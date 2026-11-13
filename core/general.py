"""
通用短视频解析（迁移自 rconsole-plugin/apps/tools.js 的 general 分支 + utils/general-link-adapter.js）

覆盖：快手、西瓜视频、皮皮虾、皮皮搞笑、QQ小世界、即刻

快手优先走原生网页解析（免第三方），失败后和其余平台一起落到第三方解析接口列表。
第三方接口本身是外挂式服务，随时可能失效，所以接口列表做成了可配置项。
"""

import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import quote

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.tools import (
    COMMON_USER_AGENT,
    GENERAL_PARSE_APIS,
    KUAISHOU_MOBILE_PAGE,
    KUAISHOU_REFERER,
    KUAISHOU_V_MOBILE_PAGE,
    KUAISHOU_VIDEO_PAGE,
)
from .common import (
    build_temp_path,
    cover_text_result,
    delete_file,
    download_file,
    download_images,
    fetch_json,
    fetch_text,
    get_message_text,
    image_send_results,
    probe_media_url,
    release_file,
    release_video,
    resolve_redirect,
    truncate_str,
    video_components,
)

UA_HEADERS = {"User-Agent": COMMON_USER_AGENT}
KUAISHOU_HEADERS = {"User-Agent": COMMON_USER_AGENT, "Referer": KUAISHOU_REFERER}

PLATFORM_RULES = [
    (r"(?:www\.|v\.)?kuaishou\.com|chenzhongtech\.com", "快手"),
    (r"(?:www\.|v\.|m\.)?ixigua\.com", "西瓜视频"),
    (r"h5\.pipix\.com", "皮皮虾"),
    (r"h5\.pipigx\.com", "皮皮搞笑"),
    (r"s\.xsj\.qq\.com", "QQ小世界"),
    (r"m\.okjike\.com", "即刻"),
]


def detect_platform(msg: str) -> str:
    for pattern, name in PLATFORM_RULES:
        if re.search(pattern, msg):
            return name
    return ""


def _find_url(msg: str) -> str:
    match = re.search(r"(?:https?://)?[^\s]*?(?:kuaishou\.com|chenzhongtech\.com|ixigua\.com|pipix\.com|pipigx\.com|xsj\.qq\.com|okjike\.com)[^\s]*", msg)
    return match.group(0) if match else ""


# ---------------------------------------------------------------- 快手原生解析


def _parse_embedded_state(html_text: str) -> Optional[Any]:
    for marker in ("window.__APOLLO_STATE__", "window.INIT_STATE", "__APOLLO_STATE__", "window.__INITIAL_STATE__"):
        index = html_text.find(marker)
        if index < 0:
            continue
        brace = html_text.find("{", index)
        if brace < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(html_text[brace:])
        except Exception:  # noqa: BLE001
            continue
        return obj
    return None


def _walk_candidates(node: Any, out: List[Dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if any(key in node for key in ("photoUrl", "srcNoMark", "mainMvUrls", "photoUrlList")):
            out.append(node)
        for value in node.values():
            _walk_candidates(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_candidates(value, out)


def _pick_kuaishou_video(node: Dict[str, Any]) -> Optional[str]:
    for key in ("photoUrl", "srcNoMark", "playUrl"):
        if isinstance(node.get(key), str) and node[key].startswith("http"):
            return node[key]
    for key in ("photoUrlList", "mainMvUrls"):
        for item in node.get(key) or []:
            if isinstance(item, dict) and str(item.get("url", "")).startswith("http"):
                return item["url"]
            if isinstance(item, str) and item.startswith("http"):
                return item
    return None


def _match_kuaishou(html_text: str) -> Optional[Dict[str, Any]]:
    state = _parse_embedded_state(html_text)
    if not state:
        return None
    candidates: List[Dict[str, Any]] = []
    _walk_candidates(state, candidates)

    best: Optional[Dict[str, Any]] = None
    for node in candidates:
        video_url = _pick_kuaishou_video(node)
        if not video_url:
            continue
        caption = node.get("caption") or ""
        if best is None or (caption and not best.get("desc")):
            best = {
                "name": "快手",
                "video": video_url,
                "images": [],
                "desc": truncate_str(caption, 200),
                "author": (node.get("userName") or (node.get("user") or {}).get("name") or ""),
                "cover": node.get("coverUrl") or node.get("coverUrlList") or "",
            }
        if best.get("desc"):
            break
    return best


async def _kuaishou_native(link: str) -> tuple:
    """
    快手原生解析，返回 (结果 或 None, 跳转后的链接)。

    页面侧有好几种形态：
    * www.kuaishou.com/short-video/{id}      桌面版，部分视频仍有 __APOLLO_STATE__
    * m.chenzhongtech.com/fw/photo/{id}      移动分享页，命中率最高
    * v.m.chenzhongtech.com/fw/photo/{id}    上面那个的 v 域名别名
    短链先跟跳转，再逐个模板尝试；最后连原始短链页面本身也试一遍。
    """
    resolved = link
    if any(key in link for key in ("v.kuaishou.com", "v.m.chenzhongtech.com", "m.chenzhongtech.com")):
        resolved = await resolve_redirect(link, headers=UA_HEADERS)

    candidates = [resolved, link]
    pages: List[str] = []
    photo_ids: List[str] = []
    seen: set = set()

    for candidate in candidates:
        for pattern in (r"short-video/([^/?#]+)", r"/fw/photo/([^/?#]+)", r"/fw/long-video/([^/?#]+)"):
            match = re.search(pattern, candidate)
            if not match:
                continue
            photo_id = match.group(1)
            if photo_id in photo_ids:
                continue
            photo_ids.append(photo_id)
            for template in (KUAISHOU_VIDEO_PAGE, KUAISHOU_MOBILE_PAGE, KUAISHOU_V_MOBILE_PAGE):
                page = template.format(photo_id)
                if page not in seen:
                    seen.add(page)
                    pages.append(page)

    for candidate in candidates:
        if candidate.startswith("http") and candidate not in seen:
            seen.add(candidate)
            pages.append(candidate)

    for page in pages:
        html_text = await fetch_text(page, headers=KUAISHOU_HEADERS, timeout=20)
        if not html_text:
            continue
        result = _match_kuaishou(html_text)
        if result:
            logger.info(f"[R插件][通用解析] 快手原生解析命中: {page}")
            return result, resolved
    return None, resolved


# ---------------------------------------------------------------- 第三方接口


async def _third_party_once(link: str, api_list: List[str], encode: bool = False) -> Optional[Dict[str, Any]]:
    target = quote(link, safe="") if encode else link
    for template in api_list:
        url = template.replace("{}", target)
        payload = await fetch_json(url, headers={**UA_HEADERS, "Accept": "*/*"}, timeout=25)
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        if isinstance(data, dict):
            video = data.get("url") or data.get("playAddr") or data.get("video")
            images = data.get("images") or data.get("imageUrl") or data.get("pics") or data.get("imgurl") or []
            desc = data.get("title") or data.get("desc") or ""
            author = data.get("author") or ""
            cover = data.get("cover") or data.get("avatar") or ""
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            video = data[0].get("url")
            images = []
            desc = data[0].get("title") or ""
            author = ""
            cover = ""
        else:
            continue

        if isinstance(images, str):
            images = [images] if images.startswith("http") else []
        images = [u for u in images if isinstance(u, str) and u.startswith("http")]
        if not video and not images:
            continue
        logger.info(f"[R插件][通用解析] 第三方接口命中: {template}")
        return {
            "video": video,
            "images": images,
            "desc": truncate_str(desc, 200),
            "author": author or "",
            "cover": cover if isinstance(cover, str) else "",
        }
    return None


async def _third_party(link: str, api_list: List[str]) -> Optional[Dict[str, Any]]:
    result = await _third_party_once(link, api_list, encode=False)
    if result:
        return result
    return await _third_party_once(link, api_list, encode=True)


# ---------------------------------------------------------------- 入口


async def process_general_url(
    event: AstrMessageEvent,
    parse_apis: Optional[List[str]] = None,
    identify_prefix: str = "识别：",
    image_batch_threshold: int = 50,
    video_size_limit_mb: int = 0,
    image_use_forward: bool = True,
) -> AsyncGenerator[Any, None]:
    msg = get_message_text(event)
    if not msg:
        return

    platform = detect_platform(msg)
    link = _find_url(msg)
    if not platform or not link:
        return
    if not link.startswith("http"):
        link = "https://" + link
    logger.info(f"[R插件][{platform}] 收到链接: {link}")

    data: Optional[Dict[str, Any]] = None
    resolved = link
    if platform == "快手":
        try:
            data, resolved = await _kuaishou_native(link)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][快手] 原生解析异常: {exc}")
            data = None

    if not data:
        for candidate in dict.fromkeys([resolved, link]):
            data = await _third_party(candidate, list(parse_apis or GENERAL_PARSE_APIS))
            if data:
                break

    if not data:
        yield event.plain_result(
            f"{platform} 解析失败：原生解析没命中，第三方接口也没返回结果（接口可能已失效，可在配置里替换 GENERAL_PARSE_APIS）"
        )
        return

    header = f"{identify_prefix}{data.get('name') or platform}"
    if data.get("author"):
        header += f"\n👤{data['author']}"
    if data.get("desc"):
        header += f"\n📝{data['desc']}"

    cover = data.get("cover")
    yield await cover_text_result(
        event, cover, header, referer=KUAISHOU_REFERER if platform == "快手" else "", prefix="gen_cover"
    )

    images: List[str] = data.get("images") or []
    if images:
        local_images = await download_images(images, headers=UA_HEADERS, prefix=platform, limit=36)
        if local_images:
            components = [Comp.Image.fromFileSystem(p) for p in local_images]
            for result in image_send_results(
                event, components, use_forward=image_use_forward, batch=image_batch_threshold
            ):
                yield result
            for path in local_images:
                release_file(path)

    video_url = data.get("video")
    if video_url:
        referer = KUAISHOU_REFERER if platform == "快手" else ""
        headers = {"User-Agent": COMMON_USER_AGENT}
        if referer:
            headers["Referer"] = referer
        # 第三方接口经常返回已经过期的直链，先探一下再下载，省得白白等超时
        if not await probe_media_url(video_url, headers=headers):
            yield event.plain_result(
                f"{platform} 视频直链已失效（多半是第三方接口返回的缓存地址过期）。\n原链接：{link}"
            )
            return
        path = await download_file(video_url, build_temp_path(platform, ".mp4"), headers=headers, timeout=240)
        if not path:
            yield event.plain_result(f"{platform} 视频下载失败\n原链接：{link}")
            return
        components = video_components(path, f"{platform}.mp4", video_size_limit_mb)
        if components:
            yield event.chain_result(components)
        else:
            yield event.plain_result(f"{platform} 视频超过 {video_size_limit_mb}MB 发送上限\n原链接：{link}")
        release_video(path)