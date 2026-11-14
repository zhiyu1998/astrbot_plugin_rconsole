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


# ---------------------------------------------------------------- 错误诊断

# 常见报错特征 → 人话解释
_BLOCK_SIGNS = (
    "unexpected_eof",
    "eof occurred in violation",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "ssl",
    "timed out",
    "timeout",
)
_PROXY_SIGNS = (
    "unable to connect to proxy",
    "proxyerror",
    "proxy error",
    "tunnel connection failed",
    "connection refused",
)


def diagnose_error(exc: BaseException, proxy: Optional[str]) -> str:
    """
    把 yt-dlp 的报错翻译成能直接照做的提示。

    区分三种情况：
      1. 没配代理 / 代理没生效，但目标站点被墙（SSL EOF、连接重置）
      2. 配了代理但连不上（refused / tunnel failed）
      3. 其他（视频不存在、地区限制、需要登录等）
    """
    raw = str(exc)
    low = raw.lower()

    if any(sign in low for sign in _PROXY_SIGNS):
        if proxy:
            return (
                f"代理连不上（{proxy}）。检查地址和端口是否写对、代理是否在运行、"
                "服务器能否访问到它；改好后重试。"
            )
        return "网络不通：连接被拒绝。"

    blocked = any(sign in low for sign in _BLOCK_SIGNS)
    if blocked:
        if not proxy:
            return (
                "服务器连不上 YouTube（TLS 握手被重置 / 域名被污染），这不是插件的问题。"
                "请在面板「【代理】代理地址」填上可用的代理，例如 http://127.0.0.1:7897，"
                "并确认「走代理的平台」里包含 youtube。"
            )
        return (
            f"已配置代理（{proxy}），但 YouTube 仍然连不上："
            "代理可能没生效、被限速或线路不通，换个节点/端口试试。"
        )

    if "video unavailable" in low or "not available" in low:
        return "视频不可用：可能已删除、私享，或在你所在地区被限制。"
    if "private" in low:
        return "该视频是私享视频，无法解析。"
    if "members-only" in low or "join this channel" in low:
        return "该视频仅频道会员可看，需要 Cookie 才能下载。"
    if "age" in low and "confirm" in low:
        return "该视频有年龄限制，需要登录 Cookie 才能解析。"
    if "sign in" in low:
        return "该视频需要登录才能查看（可在 yt-dlp 侧配置 Cookie）。"

    return ""


async def check_proxy(proxy: Optional[str], timeout: float = 8.0) -> Dict[str, Any]:
    """代理连通性自检：返回能否连到 YouTube"""
    import httpx

    target = "https://www.youtube.com/generate_204"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        if proxy:
            try:
                client = httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=True)
            except TypeError:
                client = httpx.AsyncClient(timeout=timeout, proxies=proxy, follow_redirects=True)
        else:
            client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        async with client:
            resp = await client.get(target, headers=headers)
        return {"ok": resp.status_code in (200, 204), "status": resp.status_code, "proxy": proxy}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160], "proxy": proxy}


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
        advice = diagnose_error(exc, proxy)
        yield event.plain_result(
            f"YouTube 解析失败：{advice or str(exc)[:200]}\n"
            f"（详细日志：{str(exc).splitlines()[0][:180]}）"
        )
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

    lines = [f"识别：YouTube，{title}"]
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
        advice = diagnose_error(exc, proxy)
        yield event.plain_result(
            f"YouTube 视频下载失败：{advice or str(exc)[:200]}\n原视频：{url}"
        )
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