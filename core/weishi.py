"""
微视解析（迁移自 rconsole-plugin/apps/tools.js 的 weishi 分支）

短链先 HEAD 跟一次跳转，从最终地址里取 id，再请求 H5 播放页接口拿视频直链。
"""

import re
from typing import Any, AsyncGenerator

import httpx
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.tools import COMMON_USER_AGENT, WEISHI_VIDEO_INFO
from .common import (
    build_temp_path,
    cover_text_result,
    delete_file,
    download_file,
    fetch_json,
    get_message_text,
    release_video,
    truncate_str,
    video_components,
)

WEISHI_HEADERS = {
    "User-Agent": COMMON_USER_AGENT,
    "Referer": "https://video.weishi.qq.com/",
}


async def _final_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.head(url, headers=WEISHI_HEADERS)
        return str(resp.url) or url
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][微视] 短链跳转失败: {exc}")
        return url


async def process_weishi_url(
    event: AstrMessageEvent,
    identify_prefix: str = "识别：",
    video_size_limit_mb: int = 0,
) -> AsyncGenerator[Any, None]:
    msg = get_message_text(event)
    if not msg:
        return

    match = re.search(r"https?://video\.weishi\.qq\.com/\S+", msg)
    if not match:
        return
    url = match.group(0).strip()
    logger.info(f"[R插件][微视] 收到链接: {url}")

    final_url = await _final_url(url)
    id_match = re.search(r"id=([^&]+)&spid", final_url) or re.search(r"[?&]id=([^&]+)", final_url)
    if not id_match:
        yield event.plain_result("微视解析失败：无法从链接里拿到视频 ID")
        return

    feed_id = id_match.group(1)
    payload = await fetch_json(WEISHI_VIDEO_INFO.format(feed_id), headers=WEISHI_HEADERS, timeout=20)
    if not isinstance(payload, dict):
        yield event.plain_result("微视解析失败：接口没有返回数据")
        return

    feeds = (((payload.get("data") or {}).get("feeds")) or [])
    if not feeds:
        yield event.plain_result("微视解析失败：作品不存在或已删除")
        return

    feed = feeds[0] or {}
    title = truncate_str(feed.get("feed_desc") or "微视视频", 120)
    cover = ((feed.get("images") or [{}])[0] or {}).get("url")
    video_url = feed.get("video_url")

    header_text = f"{identify_prefix}微视，{title}"
    yield await cover_text_result(
        event, cover, header_text, referer="https://video.weishi.qq.com/", prefix="ws_cover"
    )

    if not video_url:
        yield event.plain_result("微视解析失败：没有拿到视频直链")
        return

    path = await download_file(
        video_url,
        build_temp_path("weishi", ".mp4"),
        headers=WEISHI_HEADERS,
        referer="https://video.weishi.qq.com/",
        timeout=240,
    )
    if not path:
        yield event.plain_result("微视视频下载失败")
        return
    components = video_components(path, "weishi.mp4", video_size_limit_mb)
    if components:
        yield event.chain_result(components)
    else:
        yield event.plain_result(f"微视视频超过 {video_size_limit_mb}MB 发送上限\n原链接：{url}")
    release_video(path)