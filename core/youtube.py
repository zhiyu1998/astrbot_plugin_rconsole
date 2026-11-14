"""YouTube 解析（yt-dlp）

YouTube 在多数国内服务器上不可直连，因此需要 HTTP/SOCKS 代理：
配置 `PROXY_ENABLED` + `PROXY_ADDR` + `PROXY_PORT`（默认 127.0.0.1:7890），
并把 `youtube` 放进 `PROXY_PLATFORMS`（默认已包含）。
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
from typing import Any, Dict, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .common import (
    cache_dir,
    limit_notice_precise,
    local_image,
    release_video,
    video_components,
)

YOUTUBE_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*|shorts/[\w-]{6,}|live/[\w-]{6,}|embed/[\w-]{6,})|youtu\.be/[\w-]{6,})",
    re.IGNORECASE,
)
_VIDEO_ID = re.compile(r"(?:v=|/shorts/|/live/|/embed/|youtu\.be/)([\w-]{11})")

# 画质上限（yt-dlp format filter 用）
MAX_HEIGHT_MAP = {
    "2160P": 2160,
    "1440P": 1440,
    "1080P": 1080,
    "720P": 720,
    "480P": 480,
    "360P": 360,
}


def extract_youtube_url(text: str) -> str:
    """从一段文本里抠出 YouTube 链接（卡片解析后的文本也能命中）"""
    if not text:
        return ""
    match = YOUTUBE_URL_REGEX.search(text.replace("\\", ""))
    if not match:
        return ""
    url = match.group(0)
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def video_id_of(url: str) -> str:
    match = _VIDEO_ID.search(url)
    return match.group(1) if match else ""


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _fmt_count(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    return str(number)


def _extract_info(url: str, proxy: Optional[str]) -> Dict[str, Any]:
    """同步调用 yt-dlp 取元数据（放到线程里跑）"""
    import yt_dlp

    options: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 25,
        "nocheckcertificate": True,
        "ignoreerrors": False,
    }
    if proxy:
        options["proxy"] = proxy
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _download(url: str, target_dir: str, name: str, proxy: Optional[str], max_height: int) -> str:
    """同步下载到 target_dir，返回最终文件路径"""
    import yt_dlp

    height = max_height or 1080
    fmt = (
        f"bv*[ext=mp4][height<={height}]+ba[ext=m4a]/"
        f"b[ext=mp4][height<={height}]/"
        f"bv*[height<={height}]+ba/b"
    )
    outtmpl = os.path.join(target_dir, f"{name}.%(ext)s")
    options: Dict[str, Any] = {
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
    }
    if proxy:
        options["proxy"] = proxy
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    for candidate in (os.path.join(target_dir, f"{name}.mp4"),) + tuple(
        glob.glob(os.path.join(target_dir, f"{name}.*"))
    ):
        if candidate.lower().endswith(".mp4") and os.path.exists(candidate):
            return candidate
    found = glob.glob(os.path.join(target_dir, f"{name}.*"))
    if found:
        return found[0]
    raise FileNotFoundError("yt-dlp 下载完成但没找到输出文件")


async def process_youtube_url(
    event,
    video_duration_maximum: int = 480,
    video_size_limit_mb: int = 0,
    *,
    proxy: Optional[str] = None,
    display_cover: bool = True,
    display_info: bool = True,
    display_desc: bool = True,
    desc_len_limit: int = 0,
    max_resolution: str = "1080P",
):
    text = (getattr(event, "message_str", "") or "").strip()
    url = extract_youtube_url(text)
    if not url:
        yield event.plain_result("没识别出 YouTube 链接")
        return

    vid = video_id_of(url) or "youtube"
    logger.info(f"[R插件][YouTube] 解析 {url} 代理={proxy or '直连'}")

    try:
        info = await asyncio.to_thread(_extract_info, url, proxy)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][YouTube] 取信息失败: {exc}")
        hint = "（YouTube 通常需要代理，检查 PROXY_ENABLED / PROXY_ADDR / PROXY_PORT）" if not proxy else ""
        yield event.plain_result(f"YouTube 解析失败：{exc}{hint}")
        return

    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0] or {}

    title = (info.get("title") or "").strip() or "YouTube 视频"
    uploader = (info.get("uploader") or info.get("channel") or "").strip()
    duration = info.get("duration") or 0
    description = (info.get("description") or "").strip().replace("\r", "")
    thumbnail = info.get("thumbnail") or ""
    if not thumbnail:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumbnail = thumbs[-1].get("url", "")

    lines = [f"\n识别：YouTube，{title}"]
    if display_info:
        stats = []
        if uploader:
            stats.append(f"👤 {uploader}")
        if info.get("view_count"):
            stats.append(f"▶️ {_fmt_count(info.get('view_count'))}")
        if info.get("like_count"):
            stats.append(f"👍 {_fmt_count(info.get('like_count'))}")
        if duration:
            stats.append(f"⏱️ {_fmt_duration(duration)}")
        if stats:
            lines.append(" | ".join(stats))
    if display_desc and description:
        desc = description
        if desc_len_limit and desc_len_limit > 0 and len(desc) > desc_len_limit:
            desc = desc[:desc_len_limit] + "…"
        lines.append(f"📝 简介：{desc.splitlines()[0][:200] if not desc_len_limit else desc}")

    prefix: list = []
    if display_cover and thumbnail:
        cover = await local_image(
            thumbnail, referer="https://www.youtube.com/", prefix="yt_cover", proxy=proxy
        )
        if cover is not None:
            prefix.append(cover)

    header = "\n".join(lines)
    if duration and duration > video_duration_maximum:
        yield event.chain_result(prefix + [
            Comp.Plain(f"{header}\n{limit_notice_precise(duration, video_duration_maximum)}")
        ])
        return

    yield event.chain_result(prefix + [Comp.Plain(header)])

    work_dir = cache_dir("youtube_cache")
    max_height = MAX_HEIGHT_MAP.get((max_resolution or "1080P").upper(), 1080)
    try:
        path = await asyncio.to_thread(_download, url, work_dir, vid, proxy, max_height)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][YouTube] 下载失败: {exc}")
        yield event.plain_result(f"YouTube 视频下载失败：{exc}")
        return

    logger.info(f"[R插件][YouTube] 下载完成 {path}")
    components = video_components(path, f"{vid}.mp4", video_size_limit_mb)
    if components:
        yield event.chain_result(components)
    else:
        yield event.plain_result(
            f"视频已下载但无法发送：可能超过大小上限 {video_size_limit_mb}MB。\n原视频：{url}"
        )
    release_video(path)