"""
米游社解析（迁移自 rconsole-plugin/apps/tools.js 的 miyoushe 分支 + utils/mihoyo.js）

米游社网页端接口需要 DS 签名：md5("salt=...&t=...&r=...")，格式 "t,r,c"
"""

import hashlib
import json
import random
import re
import string
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.tools import COMMON_USER_AGENT, MIYOUSHE_ARTICLE, MIYOUSHE_SALT
from .common import (
    build_temp_path,
    cover_text_result,
    delete_file,
    download_file,
    download_images,
    fetch_json,
    get_message_text,
    image_send_results,
    release_file,
    release_video,
    truncate_str,
    video_components,
)


def gen_ds() -> str:
    """米游社 DS 签名"""
    timestamp = int(time.time())
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    check = hashlib.md5(f"salt={MIYOUSHE_SALT}&t={timestamp}&r={rand}".encode("utf-8")).hexdigest()
    return f"{timestamp},{rand},{check}"


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": COMMON_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-cn",
        "Connection": "keep-alive",
        "x-rpc-app_version": "2.87.0",
        "x-rpc-client_type": "4",
        "Referer": "https://www.miyoushe.com/",
        "DS": gen_ds(),
    }


def extract_post_id(url: str) -> str:
    match = re.search(r"/(\d+)(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    match = re.search(r"(\d{6,})", url)
    return match.group(0) if match else ""


def _content_describe(content: Any) -> str:
    """content 一般是 JSON 字符串，取里面结构化的 describe"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:  # noqa: BLE001
            return truncate_str(content, 500)
    if isinstance(content, dict):
        for key in ("describe", "text", "content"):
            if content.get(key):
                return truncate_str(str(content[key]), 500)
    return ""


async def process_miyoushe_url(
    event: AstrMessageEvent,
    identify_prefix: str = "识别：",
    image_batch_threshold: int = 50,
    video_size_limit_mb: int = 0,
    image_use_forward: bool = True,
) -> AsyncGenerator[Any, None]:
    msg = get_message_text(event)
    if not msg:
        return

    match = re.search(r"(?:https?://)?(?:m|www)\.miyoushe\.com/[^\s]*", msg)
    if not match:
        return
    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    logger.info(f"[R插件][米游社] 收到链接: {url}")

    post_id = extract_post_id(url)
    if not post_id:
        yield event.plain_result("米游社解析失败：链接里没有作品 ID")
        return

    payload = await fetch_json(MIYOUSHE_ARTICLE.format(post_id), headers=_headers(), timeout=20)
    if not isinstance(payload, dict) or payload.get("ret") not in (0, None):
        logger.error(f"[R插件][米游社] 接口返回异常: {str(payload)[:200]}")
        yield event.plain_result("米游社解析失败，接口可能要求登录或者风控了")
        return

    data = payload.get("data") or {}
    post_wrap = (data.get("post") or {})
    post = (post_wrap.get("post") or {})
    if not post:
        yield event.plain_result("米游社解析失败：没有拿到帖子内容")
        return

    cover = post.get("cover")
    subject = post.get("subject") or "米游社帖子"
    describe = _content_describe(post.get("content"))
    images: List[str] = [url for url in (post.get("images") or []) if url]

    header_text = f"{identify_prefix}米游社，{subject}"
    if describe:
        header_text += f"\n{describe}"

    yield await cover_text_result(
        event, cover, header_text, referer="https://www.miyoushe.com/", prefix="my_cover"
    )

    if images:
        local_images = await download_images(images, prefix="miyoushe", referer="https://www.miyoushe.com/", limit=40)
        if local_images:
            components = [Comp.Image.fromFileSystem(p) for p in local_images]
            for result in image_send_results(
                event, components, use_forward=image_use_forward, batch=image_batch_threshold
            ):
                yield result
            for path in local_images:
                release_file(path)

    vod_list = (data.get("post") or {}).get("vod_list") or post_wrap.get("vod_list") or []
    video_url: Optional[str] = None
    for vod in vod_list:
        for resolution in (vod or {}).get("resolutions") or []:
            if (resolution or {}).get("url"):
                video_url = resolution["url"]
                break
        if video_url:
            break

    if video_url:
        path = await download_file(
            video_url,
            build_temp_path("miyoushe", ".mp4"),
            referer="https://www.miyoushe.com/",
            timeout=240,
        )
        if path:
            components = video_components(path, "miyoushe.mp4", video_size_limit_mb)
            if components:
                yield event.chain_result(components)
            else:
                yield event.plain_result(f"米游社视频超过 {video_size_limit_mb}MB 发送上限\n原链接：{url}")
            release_video(path)
        else:
            yield event.plain_result("米游社视频下载失败")