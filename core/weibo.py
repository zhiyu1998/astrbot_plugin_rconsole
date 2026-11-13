"""
微博解析（迁移自 rconsole-plugin/apps/tools.js 的 weibo 分支 + utils/weibo.js）

支持：
* m.weibo.cn/detail|status/{id}
* weibo.com/{uid}/{mid}、weibo.com/tv/show?...mid=（mid 转 base62 id）
* 正文、九宫格图片、视频（page_info）、热门评论、投票贴图片兜底
"""

import json
import math
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.tools import (
    COMMON_USER_AGENT,
    WEIBO_AJAX_STATUS,
    WEIBO_COMMENTS,
    WEIBO_DESKTOP_PAGE,
    WEIBO_DETAIL_PAGE,
    WEIBO_SINGLE_INFO,
)
from .common import (
    build_temp_path,
    chunked,
    create_forward_message,
    delete_file,
    download_file,
    download_images,
    fetch_json,
    fetch_text,
    get_message_text,
    html_to_text,
    image_send_results,
    release_file,
    truncate_str,
    video_components,
)

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

WEIBO_HEADERS = {
    "User-Agent": COMMON_USER_AGENT,
    "Referer": "https://weibo.com/",
}


def base62_encode(number: int) -> str:
    if number == 0:
        return "0"
    result = ""
    while number > 0:
        result = ALPHABET[number % 62] + result
        number //= 62
    return result


def mid2id(mid: str) -> str:
    """微博 mid -> base62 id，移植自 utils/weibo.js"""
    mid = str(mid)[::-1]
    size = math.ceil(len(mid) / 7)
    chunks: List[str] = []
    for i in range(size):
        part = mid[i * 7:(i + 1) * 7][::-1]
        try:
            encoded = base62_encode(int(part))
        except ValueError:
            return ""
        if i < size - 1 and len(encoded) < 4:
            encoded = "0" * (4 - len(encoded)) + encoded
        chunks.append(encoded)
    chunks.reverse()
    return "".join(chunks)


def _headers(cookie: str = "") -> Dict[str, str]:
    headers = dict(WEIBO_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def extract_weibo_id(url: str) -> str:
    """从各种微博链接里抽出 id（base62）"""
    if "m.weibo.cn" in url:
        for pattern in (r"detail/([A-Za-z0-9]+)", r"status/([A-Za-z0-9]+)", r"m\.weibo\.cn/([A-Za-z0-9]+)/([A-Za-z0-9]+)"):
            match = re.search(pattern, url)
            if match:
                groups = [g for g in match.groups() if g]
                return groups[-1] if groups else ""
        return ""
    if "weibo.com/tv/show" in url and "mid=" in url:
        match = re.search(r"mid=([A-Za-z0-9]+)", url)
        return mid2id(match.group(1)) if match else ""
    if "weibo.com" in url:
        match = re.search(r"weibo\.com/([A-Za-z0-9]+)/([A-Za-z0-9]+)", url)
        if match:
            return match.group(2)
        match = re.search(r"weibo\.com/detail/([A-Za-z0-9]+)", url)
        if match:
            return match.group(1)
    return ""


async def get_weibo_data(weibo_id: str, cookie: str = "") -> Optional[Dict[str, Any]]:
    """单条微博数据：接口优先，失败退回详情页内嵌 JSON"""
    headers = _headers(cookie)
    api_headers = {**headers, "X-Requested-With": "XMLHttpRequest"}

    payload = await fetch_json(WEIBO_SINGLE_INFO.format(weibo_id), headers=api_headers, timeout=12)
    if isinstance(payload, dict) and payload.get("data"):
        return payload["data"]

    html_text = await fetch_text(WEIBO_DETAIL_PAGE.format(weibo_id), headers=headers, timeout=12)
    if not html_text:
        return None

    for marker in ('"status":', "$render_data"):
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
        if isinstance(obj, list) and obj:
            obj = obj[0]
        if isinstance(obj, dict):
            if "status" in obj:
                obj = obj["status"]
            if obj.get("text") or obj.get("pics"):
                return obj
    return None


async def get_weibo_comments(weibo_id: str, cookie: str = "", max_count: int = 20) -> List[Dict[str, Any]]:
    headers = {**_headers(cookie), "X-Requested-With": "XMLHttpRequest"}
    payload = await fetch_json(
        WEIBO_COMMENTS.format(mid=weibo_id), headers=headers, timeout=12
    )
    if not isinstance(payload, dict):
        return []
    rows = ((payload.get("data") or {}).get("data")) or []
    results = []
    for row in rows[:max_count]:
        if not isinstance(row, dict):
            continue
        results.append({
            "user": (row.get("user") or {}).get("screen_name", ""),
            "text": html_to_text(row.get("text", "")),
            "like": row.get("like_count") or 0,
            "time": row.get("created_at", ""),
            "source": row.get("source", ""),
        })
    return results


async def get_weibo_vote_images(uid: str, weibo_id: str, cookie: str = "") -> List[str]:
    """投票贴正文里没有 pics 时，从桌面版页面里捞图"""
    if not uid:
        return []
    headers = {**_headers(cookie), "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    html_text = await fetch_text(WEIBO_DESKTOP_PAGE.format(uid=uid, id=weibo_id), headers=headers, timeout=15)
    images: List[str] = []
    if html_text:
        for match in re.findall(r"https?://wx\d\.sinaimg\.cn/[a-z0-9]+/[^\"'\s]+?\.jpg", html_text):
            if not any(flag in match for flag in ("bmiddle", "large", "mw2000", "orj")):
                continue
            url = re.sub(r"/(bmiddle|mw2000|orj\d+)/", "/large/", match)
            if url not in images:
                images.append(url)
    if images:
        return images

    payload = await fetch_json(WEIBO_AJAX_STATUS.format(weibo_id), headers=headers, timeout=12)
    if not isinstance(payload, dict):
        return []
    text = json.dumps(payload, ensure_ascii=False)
    for match in re.findall(r"https?://[^\"]+sinaimg[^\"]+", text):
        url = match.replace("\\/", "/")
        if any(flag in url for flag in ("bmiddle", "large")) and url not in images:
            images.append(url)
    return images


def _render_text(status: Dict[str, Any], identify_prefix: str) -> str:
    parts = [f"{identify_prefix}微博"]
    text = truncate_str(html_to_text(status.get("text", "")), 800)
    if text:
        parts.append(text)
    status_title = status.get("status_title")
    if status_title:
        parts.append(status_title)
    source = status.get("source") or ""
    region = status.get("region_name") or ""
    if source or region:
        parts.append(f"{source}{('  ' + region) if region else ''}".strip())
    retweeted = status.get("retweeted_status")
    if isinstance(retweeted, dict):
        forward_text = truncate_str(html_to_text(retweeted.get("text", "")), 400)
        if forward_text:
            parts.append(f"// 转发自 {(retweeted.get('user') or {}).get('screen_name', '')}：{forward_text}")
    user = status.get("user") or {}
    if user.get("screen_name"):
        parts.append(f"👤 {user.get('screen_name')}")
    return "\n".join(parts)


def _collect_pics(status: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for pic in status.get("pics") or []:
        if not isinstance(pic, dict):
            continue
        url = ((pic.get("large") or {}).get("url")) or pic.get("url")
        if url:
            urls.append(url)
    return urls


def _collect_video(status: Dict[str, Any]) -> Optional[str]:
    page = status.get("page_info") or {}
    urls = page.get("urls") or {}
    for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4", "mp4_sd_mp4"):
        if urls.get(key):
            return urls[key]
    if page.get("media_info"):
        media = page["media_info"]
        for key in ("stream_url_hd", "stream_url"):
            if media.get(key):
                return media[key]
    return None


async def process_weibo_url(
    event: AstrMessageEvent,
    cookie: str = "",
    identify_prefix: str = "识别：",
    enable_comments: bool = True,
    image_batch_threshold: int = 50,
    video_size_limit_mb: int = 0,
    image_use_forward: bool = True,
) -> AsyncGenerator[Any, None]:
    msg = get_message_text(event)
    if not msg:
        return

    match = re.search(r"(?:https?://)?(?:m\.weibo\.cn|weibo\.com|weibo\.cn)/[^\s]*", msg)
    if not match:
        return
    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    logger.info(f"[R插件][微博] 收到链接: {url}")

    weibo_id = extract_weibo_id(url)
    if not weibo_id:
        yield event.plain_result("微博解析失败：无法获取到微博ID")
        return

    status_data = await get_weibo_data(weibo_id, cookie)
    if not status_data:
        yield event.plain_result(
            "微博解析失败：拿不到数据" + ("" if cookie else "，可以尝试在配置里填 WEIBO_CK")
        )
        return

    yield event.plain_result(_render_text(status_data, identify_prefix))

    pics = _collect_pics(status_data)
    video_url = _collect_video(status_data)

    if pics:
        local_images = await download_images(pics, headers=WEIBO_HEADERS, prefix="weibo", limit=18)
        if local_images:
            components = [Comp.Image.fromFileSystem(p) for p in local_images]
            for result in image_send_results(
                event, components, use_forward=image_use_forward, batch=image_batch_threshold
            ):
                yield result
            for path in local_images:
                release_file(path)
    elif not video_url:
        uid = (status_data.get("user") or {}).get("id") or (status_data.get("user") or {}).get("idstr") or ""
        vote_images = await get_weibo_vote_images(str(uid), weibo_id, cookie)
        if vote_images:
            local_images = await download_images(
                vote_images[:10], headers=WEIBO_HEADERS, prefix="weibo_vote", limit=10
            )
            if local_images:
                yield event.chain_result([Comp.Image.fromFileSystem(p) for p in local_images])
                for path in local_images:
                    delete_file(path)

    if video_url:
        path = await download_file(
            video_url,
            build_temp_path("weibo", ".mp4"),
            headers=WEIBO_HEADERS,
            referer="https://weibo.com/",
            timeout=240,
        )
        if path:
            components = video_components(path, "weibo.mp4", video_size_limit_mb)
            if components:
                yield event.chain_result(components)
            delete_file(path)
        else:
            yield event.plain_result("微博视频下载失败")

    if enable_comments:
        try:
            comments = await get_weibo_comments(weibo_id, cookie)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][微博] 评论获取失败: {exc}")
            comments = []
        if comments:
            blocks: List[Dict[str, Any]] = []
            for item in comments:
                content = f"{item['text']}\n{item['like']}👍 · {item['time']}{(' ' + item['source']) if item['source'] else ''}"
                blocks.append({
                    "name": item["user"] or "微博用户",
                    "uin": event.get_sender_id(),
                    "content": [Comp.Plain(content)],
                })
            try:
                nodes = create_forward_message(blocks, event.get_sender_name(), event.get_sender_id())
                yield event.chain_result([nodes])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件][微博] 评论转发失败，改为逐条发送: {exc}")
                for item in comments:
                    yield event.plain_result(f"{item['user']}：{item['text']}")