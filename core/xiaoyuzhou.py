"""
小宇宙（Xiaoyuzhou FM）链接解析

支持：
* 单集链接：xiaoyuzhoufm.com/episode/<24位hex>
* 播客主页：xiaoyuzhoufm.com/podcast/<24位hex>（展示信息 + 最新 5 期目录）
* 分享短链：xyzfm.link/...（自动跟随跳转后解析）
* QQ 小程序/分享卡片注入链接

解析策略：
* 主链路免登录抓网页 `__NEXT_DATA__`（缺失时用 OpenGraph `<meta>` 兜底）
* 付费单集 / 网页解析失败时，若配置了 `XIAOYUZHOU_ACCESS_TOKEN` / `REFRESH_TOKEN`，
  走官方 App API（支持 401 自动刷新 Token）
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.tools import COMMON_USER_AGENT
from ..constants.xiaoyuzhou import (
    API_BASE,
    APP_BUILDNO,
    APP_BUNDLE_ID,
    APP_USER_AGENT,
    APP_VERSION,
    AUDIO_EXTENSIONS,
    DEFAULT_COVER,
    EP_EPISODE_GET,
    EP_PODCAST_GET,
    EP_PRIVATE_MEDIA_GET,
    EP_TOKEN_REFRESH,
    EPISODE_PAGE,
    FILE_BASE64_MAX_MB,
    MIME_TO_EXT,
    PODCAST_PAGE,
    VOICE_BASE64_MAX_MB,
    VOICE_BITRATE,
    VOICE_MAX_MB_NON_ONEBOT,
    XIAOYUZHOU_REFERER,
)
from .common import (
    build_temp_path,
    convert_audio_to_mp3,
    cover_text_result,
    delete_file,
    download_file,
    fetch_text,
    get_message_text,
    html_to_text,
    limit_notice,
    qq_official_limits,
    register_file_url,
    release_file,
    release_video,
    resolve_redirect,
    safe_filename,
    strip_route_links,
    truncate_str,
)

_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:www|m)\.)?(?:xiaoyuzhoufm\.com|xyzfm\.link)[^\s\"'<>（）()【】\[\]]*",
    re.IGNORECASE,
)
_EPISODE_PATH_RE = re.compile(r"/episodes?/([0-9a-f]{24})(?:[/?#&]|$)", re.IGNORECASE)
_PODCAST_PATH_RE = re.compile(r"/podcasts?/([0-9a-f]{24})(?:[/?#&]|$)", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_META_TAG_RE = re.compile(r"<meta\s+([^>]+?)/?>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z0-9_:-]+)\s*=\s*(["\'])(.*?)\2', re.DOTALL)
_AUDIO_EXT_RE = re.compile(r"\.(" + "|".join(AUDIO_EXTENSIONS) + r")(?:$|\?)", re.IGNORECASE)

_QQ_OFFICIAL_PLATFORMS = ("qq_official", "qq_official_webhook")
_CN_TZ = timezone(timedelta(hours=8))

# Token 刷新缓存（按 (refresh_token, device_id) 键控，内存级复用，避免并发重复刷新）
_TOKEN_CACHE: Dict[Tuple[str, str], Tuple[str, str]] = {}
_TOKEN_LOCK: Optional[asyncio.Lock] = None


def _token_lock() -> asyncio.Lock:
    global _TOKEN_LOCK
    if _TOKEN_LOCK is None:
        _TOKEN_LOCK = asyncio.Lock()
    return _TOKEN_LOCK


def reset_caches() -> None:
    """清空模块级 Token 缓存（供测试或热重载使用）"""
    _TOKEN_CACHE.clear()


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class XyzTarget:
    """从文本里提取到的小宇宙目标"""

    kind: str  # episode / podcast / short
    target_id: str  # eid / pid / 原始短链 URL
    raw: str = ""


@dataclass
class EpisodeInfo:
    """小宇宙单集标准化结构"""

    eid: str
    pid: str = ""
    title: str = ""
    podcast_title: str = ""
    author: str = ""
    duration: int = 0
    size_bytes: int = 0
    mime_type: str = ""
    audio_url: str = ""
    cover: str = ""
    intro: str = ""
    pub_date: str = ""
    play_count: int = 0
    comment_count: int = 0
    favorite_count: int = 0
    clap_count: int = 0
    is_private: bool = False
    pay_type: str = ""

    def page_url(self) -> str:
        """仅用于卡片 `data.url`，绝不拼进文本消息"""
        return EPISODE_PAGE.format(self.eid)


@dataclass
class PodcastInfo:
    """小宇宙播客主页标准化结构"""

    pid: str
    title: str = ""
    author: str = ""
    brief: str = ""
    description: str = ""
    cover: str = ""
    subscription_count: int = 0
    episode_count: int = 0
    episodes: List[Dict[str, Any]] = field(default_factory=list)

    def page_url(self) -> str:
        return PODCAST_PAGE.format(self.pid)


# ------------------------------------------------------------------ 纯函数


def send_mode_from_setting(value: Any) -> str:
    """把配置项映射成内部模式标识：voice / file / card"""
    if isinstance(value, bool):
        return "voice" if value else "file"
    raw = str(value or "").strip().lower()
    if raw in ("card", "卡片", "音乐卡片"):
        return "card"
    if raw in ("file", "文件", "群文件"):
        return "file"
    return "voice"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_xiaoyuzhou_target(text: str) -> Optional[XyzTarget]:
    """从消息文本里抠出小宇宙单集 / 播客 / 短链目标"""
    if not text:
        return None
    for match in _URL_RE.finditer(text):
        raw = match.group(0).rstrip(".,;!?，。；！？")
        url = raw if raw.lower().startswith(("http://", "https://")) else f"https://{raw}"
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""

        ep_match = _EPISODE_PATH_RE.search(path)
        if ep_match:
            return XyzTarget(kind="episode", target_id=ep_match.group(1).lower(), raw=url)

        pod_match = _PODCAST_PATH_RE.search(path)
        if pod_match:
            return XyzTarget(kind="podcast", target_id=pod_match.group(1).lower(), raw=url)

        if "xyzfm.link" in host or ("xiaoyuzhoufm.com" in host and path.strip("/")):
            return XyzTarget(kind="short", target_id=url, raw=url)
    return None


def extract_next_data(html: str) -> Optional[Dict[str, Any]]:
    """提取网页里的 `__NEXT_DATA__` JSON"""
    if not html:
        return None
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _extract_meta_tags(html: str) -> Dict[str, str]:
    """提取 OpenGraph / description 等 `<meta>` 标签，作 SSR 缺失时的兜底"""
    tags: Dict[str, str] = {}
    if not html:
        return tags
    for m in _META_TAG_RE.finditer(html):
        attrs = {k.lower(): html_lib.unescape(v) for k, _q, v in _ATTR_RE.findall(m.group(1))}
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if key and content and key not in tags:
            tags[key] = content
    return tags


def _pick_image_url(image: Any) -> str:
    """从小宇宙 image 结构里挑一张最适合做封面的图"""
    if isinstance(image, str):
        return image.strip()
    if not isinstance(image, dict):
        return ""
    for key in ("largePicUrl", "middlePicUrl", "picUrl", "smallPicUrl", "thumbnailUrl"):
        val = str(image.get(key) or "").strip()
        if val:
            return val
    return ""


def _infer_audio_ext(url: str, mime_type: str = "") -> str:
    """根据 URL 或 MIME 推断音频扩展名，未知时默认 m4a"""
    if url:
        m = _AUDIO_EXT_RE.search(urlparse(url).path or "")
        if m:
            return m.group(1).lower()
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime in MIME_TO_EXT:
        return MIME_TO_EXT[mime]
    if url and ".mp3" in url.lower():
        return "mp3"
    return "m4a"


def _clean_intro(description: str, shownotes: str) -> str:
    """优先使用纯文本 description，缺省时从 shownotes HTML 压出纯文本"""
    desc = (description or "").strip()
    if desc:
        lines = [line.strip() for line in desc.splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned
    return html_to_text(shownotes or "")


def parse_episode_dict(raw: Dict[str, Any], fallback_eid: str = "") -> EpisodeInfo:
    """把小宇宙单集 JSON（网页 __NEXT_DATA__ 或 App API data）归一为 EpisodeInfo"""
    podcast = raw.get("podcast") if isinstance(raw.get("podcast"), dict) else {}
    media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
    media_source = media.get("source") if isinstance(media.get("source"), dict) else {}
    enclosure = raw.get("enclosure") if isinstance(raw.get("enclosure"), dict) else {}

    audio_url = (
        str(enclosure.get("url") or "").strip() or str(media_source.get("url") or "").strip()
    )
    cover = _pick_image_url(raw.get("image")) or _pick_image_url(podcast.get("image"))

    podcasters = podcast.get("podcasters") if isinstance(podcast.get("podcasters"), list) else []
    names = [
        str(p.get("nickname") or "").strip()
        for p in podcasters
        if isinstance(p, dict) and str(p.get("nickname") or "").strip()
    ]
    author = str(podcast.get("author") or "").strip() or " / ".join(names)

    return EpisodeInfo(
        eid=str(raw.get("eid") or fallback_eid or "").strip(),
        pid=str(raw.get("pid") or podcast.get("pid") or "").strip(),
        title=str(raw.get("title") or "").strip(),
        podcast_title=str(podcast.get("title") or "").strip(),
        author=author,
        duration=_as_int(raw.get("duration")),
        size_bytes=_as_int(media.get("size")),
        mime_type=str(media.get("mimeType") or "").strip(),
        audio_url=audio_url,
        cover=cover,
        intro=_clean_intro(str(raw.get("description") or ""), str(raw.get("shownotes") or "")),
        pub_date=str(raw.get("pubDate") or "").strip(),
        play_count=_as_int(raw.get("playCount")),
        comment_count=_as_int(raw.get("commentCount")),
        favorite_count=_as_int(raw.get("favoriteCount")),
        clap_count=_as_int(raw.get("clapCount")),
        is_private=bool(raw.get("isPrivateMedia", False)),
        pay_type=str(raw.get("payType") or "").strip().upper(),
    )


def extract_episode_from_html(html: str, eid: str = "") -> Optional[EpisodeInfo]:
    """从单集页面 HTML 提取 EpisodeInfo（__NEXT_DATA__ 优先，OG meta 兜底）"""
    next_data = extract_next_data(html)
    if next_data:
        ep = next_data.get("props", {}).get("pageProps", {}).get("episode")
        if isinstance(ep, dict) and ep:
            return parse_episode_dict(ep, fallback_eid=eid)

    meta = _extract_meta_tags(html)
    audio = meta.get("og:audio") or ""
    title = meta.get("og:title") or meta.get("twitter:title") or ""
    if not (audio or title):
        return None
    return EpisodeInfo(
        eid=eid,
        title=title,
        cover=meta.get("og:image") or "",
        intro=meta.get("og:description") or meta.get("description") or "",
        audio_url=audio,
    )


def parse_podcast_dict(raw: Dict[str, Any], fallback_pid: str = "") -> PodcastInfo:
    """把播客 JSON 归一为 PodcastInfo"""
    podcasters = raw.get("podcasters") if isinstance(raw.get("podcasters"), list) else []
    names = [
        str(p.get("nickname") or "").strip()
        for p in podcasters
        if isinstance(p, dict) and str(p.get("nickname") or "").strip()
    ]
    author = str(raw.get("author") or "").strip() or " / ".join(names)
    raw_eps = raw.get("episodes") if isinstance(raw.get("episodes"), list) else []
    episodes = [ep for ep in raw_eps if isinstance(ep, dict)]
    return PodcastInfo(
        pid=str(raw.get("pid") or fallback_pid or "").strip(),
        title=str(raw.get("title") or "").strip(),
        author=author,
        brief=str(raw.get("brief") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        cover=_pick_image_url(raw.get("image")),
        subscription_count=_as_int(raw.get("subscriptionCount")),
        episode_count=_as_int(raw.get("episodeCount")),
        episodes=episodes,
    )


def extract_podcast_from_html(html: str, pid: str = "") -> Optional[PodcastInfo]:
    """从播客主页 HTML 提取 PodcastInfo"""
    next_data = extract_next_data(html)
    if next_data:
        pod = next_data.get("props", {}).get("pageProps", {}).get("podcast")
        if isinstance(pod, dict) and pod:
            return parse_podcast_dict(pod, fallback_pid=pid)

    meta = _extract_meta_tags(html)
    title = meta.get("og:title") or ""
    if not title:
        return None
    return PodcastInfo(
        pid=pid,
        title=title,
        cover=meta.get("og:image") or "",
        description=meta.get("og:description") or meta.get("description") or "",
    )


def format_duration(seconds: int) -> str:
    """秒 → MM:SS 或 HH:MM:SS"""
    total = _as_int(seconds)
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_count(value: int) -> str:
    """数量格式化（过万保留 1 位小数）"""
    num = _as_int(value)
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    mb = size_bytes / 1024 / 1024
    if mb >= 1:
        return f"{mb:.1f}MB"
    return f"{max(1, size_bytes // 1024)}KB"


def format_pub_date(raw: str) -> str:
    """ISO8601 发布时间转东八区 YYYY-MM-DD"""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_CN_TZ)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


def build_episode_info_text(
    ep: EpisodeInfo,
    *,
    identify_prefix: str = "识别：",
    display_info: bool = True,
    display_intro: bool = True,
    intro_len_limit: int = 120,
) -> str:
    """拼装单集展示文案，并清洗全部 URL 防互相触发路由"""
    title = ep.title or "未知单集"
    lines = [f"{identify_prefix}小宇宙，{title}"]

    pod_parts: List[str] = []
    if ep.podcast_title:
        pod_parts.append(f"🎙️ 播客：{ep.podcast_title}")
    if ep.author and ep.author != ep.podcast_title:
        pod_parts.append(f"主播：{ep.author}")
    if pod_parts:
        lines.append(" · ".join(pod_parts))

    meta_parts: List[str] = []
    dur = format_duration(ep.duration)
    if dur:
        meta_parts.append(f"⏱️ {dur}")
    size_text = format_size(ep.size_bytes)
    if size_text:
        meta_parts.append(size_text)
    pub = format_pub_date(ep.pub_date)
    if pub:
        meta_parts.append(pub)
    if ep.is_private or (ep.pay_type and ep.pay_type != "FREE"):
        meta_parts.append("付费/私密单集")
    if meta_parts:
        lines.append(" · ".join(meta_parts))

    if display_info:
        stat_parts: List[str] = []
        if ep.play_count > 0:
            stat_parts.append(f"播放 {format_count(ep.play_count)}")
        if ep.comment_count > 0:
            stat_parts.append(f"评论 {format_count(ep.comment_count)}")
        if ep.favorite_count > 0:
            stat_parts.append(f"收藏 {format_count(ep.favorite_count)}")
        if ep.clap_count > 0:
            stat_parts.append(f"点赞 {format_count(ep.clap_count)}")
        if stat_parts:
            lines.append("📊 " + " | ".join(stat_parts))

    if display_intro and ep.intro:
        intro = ep.intro
        if intro_len_limit and intro_len_limit > 0:
            intro = truncate_str(intro, intro_len_limit)
        lines.append(f"📝 简介：{intro}")

    return strip_route_links("\n".join(lines))


def build_podcast_info_text(
    pod: PodcastInfo,
    *,
    identify_prefix: str = "识别：",
    display_info: bool = True,
    display_intro: bool = True,
    intro_len_limit: int = 120,
) -> str:
    """拼装播客主页展示文案（含最新 5 期目录）"""
    title = pod.title or "未知播客"
    lines = [f"{identify_prefix}小宇宙播客，{title}"]

    if pod.author:
        lines.append(f"🎙️ 主播：{pod.author}")

    if display_info:
        stat_parts: List[str] = []
        if pod.subscription_count > 0:
            stat_parts.append(f"订阅 {format_count(pod.subscription_count)}")
        if pod.episode_count > 0:
            stat_parts.append(f"共 {pod.episode_count} 期")
        if stat_parts:
            lines.append("📊 " + " · ".join(stat_parts))

    intro_src = pod.brief or pod.description
    if display_intro and intro_src:
        intro = intro_src
        if intro_len_limit and intro_len_limit > 0:
            intro = truncate_str(intro, intro_len_limit)
        lines.append(f"📝 简介：{intro}")

    if pod.episodes:
        lines.append("🎧 最新单集：")
        for idx, item in enumerate(pod.episodes[:5], 1):
            ep_title = str(item.get("title") or "未命名单集").strip()
            ep_dur = format_duration(_as_int(item.get("duration")))
            suffix = f" ({ep_dur})" if ep_dur else ""
            lines.append(f"{idx}. {ep_title}{suffix}")

    return strip_route_links("\n".join(lines))


# ------------------------------------------------------------------ 网络与 Token


def _build_app_headers(access_token: str = "", device_id: str = "") -> Dict[str, str]:
    now = datetime.now(_CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    headers = {
        "Host": "api.xiaoyuzhoufm.com",
        "User-Agent": APP_USER_AGENT,
        "Market": "AppStore",
        "App-BuildNo": APP_BUILDNO,
        "OS": "ios",
        "Manufacturer": "Apple",
        "BundleID": APP_BUNDLE_ID,
        "Accept-Language": "zh-Hans-CN;q=1.0",
        "Model": "iPhone14,2",
        "app-permissions": "4",
        "Accept": "*/*",
        "App-Version": APP_VERSION,
        "WifiConnected": "true",
        "OS-Version": "17.4.1",
        "Local-Time": now,
        "Timezone": "Asia/Shanghai",
    }
    if access_token:
        headers["x-jike-access-token"] = access_token
    if device_id:
        headers["x-jike-device-id"] = device_id
    return headers


async def refresh_access_token(
    access_token: str,
    refresh_token: str,
    device_id: str = "",
    *,
    timeout: float = 15.0,
) -> Optional[Tuple[str, str]]:
    """用 refresh_token 换新的 (access_token, refresh_token)，带并发去重"""
    rt = (refresh_token or "").strip()
    if not rt:
        return None
    dev = (device_id or "").strip()
    cache_key = (rt, dev)

    async with _token_lock():
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached[0] and cached[0] != access_token:
            return cached

        headers = _build_app_headers(access_token=access_token, device_id=dev)
        headers["x-jike-refresh-token"] = rt
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.post(f"{API_BASE}{EP_TOKEN_REFRESH}", headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][小宇宙] 刷新 Token 请求失败：{exc}")
            return None

        if resp.status_code != 200:
            logger.warning(f"[R插件][小宇宙] 刷新 Token 返回 HTTP {resp.status_code}")
            return None

        new_at = (resp.headers.get("x-jike-access-token") or "").strip()
        new_rt = (resp.headers.get("x-jike-refresh-token") or rt).strip()
        if not new_at:
            try:
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict):
                    new_at = str(data.get("x-jike-access-token") or data.get("accessToken") or "")
                    new_rt = str(
                        data.get("x-jike-refresh-token") or data.get("refreshToken") or new_rt
                    )
            except Exception:  # noqa: BLE001
                new_at = ""

        if not new_at:
            return None

        _TOKEN_CACHE[cache_key] = (new_at, new_rt)
        return new_at, new_rt


async def _api_get_json(
    endpoint: str,
    params: Dict[str, Any],
    *,
    access_token: str = "",
    refresh_token: str = "",
    device_id: str = "",
    timeout: float = 15.0,
) -> Optional[Dict[str, Any]]:
    """调小宇宙官方 App API（支持 401 自动用 refresh_token 刷新一次）"""
    rt = (refresh_token or "").strip()
    dev = (device_id or "").strip()
    token = (access_token or "").strip()
    if rt and (rt, dev) in _TOKEN_CACHE:
        token = _TOKEN_CACHE[(rt, dev)][0]
    if not token and not rt:
        return None
    if not token and rt:
        refreshed = await refresh_access_token("", rt, dev, timeout=timeout)
        if not refreshed:
            return None
        token = refreshed[0]

    url = f"{API_BASE}{endpoint}"
    for attempt in range(2):
        headers = _build_app_headers(access_token=token, device_id=dev)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][小宇宙] API 请求失败 {endpoint}：{exc}")
            return None

        if resp.status_code == 401 and attempt == 0 and rt:
            refreshed = await refresh_access_token(token, rt, dev, timeout=timeout)
            if refreshed and refreshed[0]:
                token = refreshed[0]
                continue
            return None

        if resp.status_code >= 400:
            logger.warning(f"[R插件][小宇宙] API {endpoint} 返回状态码 {resp.status_code}")
            return None

        try:
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][小宇宙] API {endpoint} JSON 解析失败：{exc}")
            return None

    return None


async def fetch_episode_web(eid: str) -> Optional[EpisodeInfo]:
    """免登录抓取单集公开网页并解析"""
    html = await fetch_text(
        EPISODE_PAGE.format(eid),
        headers={"User-Agent": COMMON_USER_AGENT, "Referer": XIAOYUZHOU_REFERER},
        timeout=20.0,
    )
    if not html:
        return None
    return extract_episode_from_html(html, eid=eid)


async def fetch_episode_api(
    eid: str,
    *,
    access_token: str = "",
    refresh_token: str = "",
    device_id: str = "",
) -> Optional[EpisodeInfo]:
    """通过 App API 获取单集详情"""
    payload = await _api_get_json(
        EP_EPISODE_GET,
        {"eid": eid},
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=device_id,
    )
    data = (payload or {}).get("data")
    if not isinstance(data, dict) or not data:
        return None
    return parse_episode_dict(data, fallback_eid=eid)


async def fetch_private_media_url(
    eid: str,
    *,
    access_token: str = "",
    refresh_token: str = "",
    device_id: str = "",
) -> str:
    """获取已购付费单集的私有音频直链"""
    payload = await _api_get_json(
        EP_PRIVATE_MEDIA_GET,
        {"dubbing": "false", "eid": eid},
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=device_id,
    )
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("url") or "").strip()


async def fetch_podcast_web(pid: str) -> Optional[PodcastInfo]:
    """免登录抓取播客主页并解析"""
    html = await fetch_text(
        PODCAST_PAGE.format(pid),
        headers={"User-Agent": COMMON_USER_AGENT, "Referer": XIAOYUZHOU_REFERER},
        timeout=20.0,
    )
    if not html:
        return None
    return extract_podcast_from_html(html, pid=pid)


async def fetch_podcast_api(
    pid: str,
    *,
    access_token: str = "",
    refresh_token: str = "",
    device_id: str = "",
) -> Optional[PodcastInfo]:
    """通过 App API 获取播客详情"""
    payload = await _api_get_json(
        EP_PODCAST_GET,
        {"pid": pid},
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=device_id,
    )
    data = (payload or {}).get("data")
    if not isinstance(data, dict) or not data:
        return None
    return parse_podcast_dict(data, fallback_pid=pid)


async def _probe_audio_size(url: str, timeout: float = 10.0) -> int:
    """HEAD 探测音频字节大小；拿不到返回 0"""
    if not url:
        return 0
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.head(
                url,
                headers={"Referer": XIAOYUZHOU_REFERER, "User-Agent": COMMON_USER_AGENT},
            )
            return _as_int(resp.headers.get("content-length"))
    except Exception:  # noqa: BLE001
        return 0


# ------------------------------------------------------------------ 音频发送


def _onebot_api(event: AstrMessageEvent) -> Any:
    """拿到 OneBot(aiocqhttp) 的 api 对象；其它平台返回 None"""
    return getattr(getattr(event, "bot", None), "api", None)


def _platform_name(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_platform_name() or "").lower()
    except Exception:  # noqa: BLE001
        return ""


async def _send_onebot_segment(
    event: AstrMessageEvent, api: Any, segment: Dict[str, Any]
) -> Tuple[bool, str]:
    """通过 OneBot API 发送单个自定义消息段"""
    group_id = None
    try:
        group_id = event.get_group_id()
    except Exception:  # noqa: BLE001
        group_id = None

    message = [segment]
    try:
        if group_id:
            await api.call_action("send_group_msg", group_id=int(group_id), message=message)
        else:
            await api.call_action(
                "send_private_msg", user_id=int(event.get_sender_id()), message=message
            )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def _push_card(
    event: AstrMessageEvent,
    ep: EpisodeInfo,
    audio_url: str,
) -> Tuple[bool, str]:
    """发送 OneBot 自定义音乐卡片（绕开 Comp.Music 的 `_type` 字段坑）"""
    api = _onebot_api(event)
    if api is None:
        return False, "当前平台没有 OneBot 接口"
    if not audio_url:
        return False, "没有可用音频直链，无法拼自定义卡片"

    segment = {
        "type": "music",
        "data": {
            "type": "custom",
            "url": ep.page_url(),
            "audio": audio_url,
            "title": ep.title or "小宇宙播客",
            "image": ep.cover or DEFAULT_COVER,
            "content": ep.podcast_title or ep.author or "小宇宙",
        },
    }
    ok, err = await _send_onebot_segment(event, api, segment)
    if not ok:
        logger.warning(f"[R插件][小宇宙] 发送音乐卡片失败：{err}")
    return ok, err


async def _push_voice(event: AstrMessageEvent, path: str) -> Tuple[bool, str, bool]:
    """
    通过 OneBot 发送语音消息，返回 (是否成功, 失败原因, 是否走了 URL 直发)。

    优先走 `register_file_url`（配了 VIDEO_URL_BASE 时可绕开 base64 与体积膨胀）；
    未配置时在 `VOICE_BASE64_MAX_MB` 以内走 `base64://`。
    """
    api = _onebot_api(event)
    if api is None:
        return False, "当前平台没有 OneBot 接口", False

    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return False, "语音文件已失效", False

    file_url = await register_file_url(path, event=event)
    if file_url:
        ok, err = await _send_onebot_segment(
            event, api, {"type": "record", "data": {"file": file_url}}
        )
        if ok:
            logger.info(f"[R插件][小宇宙] 语音直发（文件服务 URL）：{file_url}")
            return True, "", True
        logger.warning(f"[R插件][小宇宙] 语音 URL 发送失败：{err}")
        return False, err, True

    if size_bytes > VOICE_BASE64_MAX_MB * 1024 * 1024:
        return (
            False,
            f"语音 {size_bytes / 1024 / 1024:.1f}MB 超过 base64 语音上限 {VOICE_BASE64_MAX_MB}MB",
            False,
        )

    try:
        with open(path, "rb") as fp:
            payload = "base64://" + base64.b64encode(fp.read()).decode()
    except OSError as exc:
        return False, f"读取语音失败：{exc}", False

    ok, err = await _send_onebot_segment(event, api, {"type": "record", "data": {"file": payload}})
    if not ok:
        logger.warning(f"[R插件][小宇宙] 语音发送失败：{err}")
    return ok, err, False


async def _push_file(
    event: AstrMessageEvent,
    path: str,
    display_name: str,
    *,
    source_url: str = "",
) -> Tuple[bool, str, bool]:
    """
    通过 OneBot 上传群/私聊文件，返回 (是否成功, 失败原因, 是否走了 URL 直发)。

    绕开 `Comp.File` 的 `file_` 字段问题：直接调 `upload_group_file`/`upload_private_file`。
    """
    api = _onebot_api(event)
    if api is None:
        return False, "当前平台没有 OneBot 接口", False

    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return False, "音频文件已失效", False

    file_payload = ""
    used_url = False
    file_url = await register_file_url(path, event=event)
    if file_url:
        file_payload = file_url
        used_url = True
    elif size_bytes <= FILE_BASE64_MAX_MB * 1024 * 1024:
        try:
            with open(path, "rb") as fp:
                file_payload = "base64://" + base64.b64encode(fp.read()).decode()
        except OSError as exc:
            return False, f"读取音频失败：{exc}", False
    elif source_url:
        file_payload = source_url
    else:
        return (
            False,
            f"音频 {size_bytes / 1024 / 1024:.1f}MB 超过文件消息上限 {FILE_BASE64_MAX_MB}MB",
            False,
        )

    group_id = None
    try:
        group_id = event.get_group_id()
    except Exception:  # noqa: BLE001
        group_id = None

    try:
        if group_id:
            await api.call_action(
                "upload_group_file",
                group_id=int(group_id),
                file=file_payload,
                name=display_name,
            )
        else:
            await api.call_action(
                "upload_private_file",
                user_id=int(event.get_sender_id()),
                file=file_payload,
                name=display_name,
            )
        return True, "", used_url
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][小宇宙] 上传文件失败：{exc}")
        return False, str(exc), used_url


async def _deliver_via_component(
    event: AstrMessageEvent,
    path: str,
    ext: str,
    display_name: str,
    send_mode: str,
) -> Any:
    """
    非 OneBot 平台（Telegram / QQ 官方 / Discord 等）走 AstrBot 消息组件。
    """
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        release_file(path, 10.0)
        return event.plain_result("音频文件读取失败，请稍后重试")

    size_mb = size_bytes / 1024 / 1024
    platform = _platform_name(event)

    # QQ 官方机器人：不支持 base64://，仅支持本地文件路径 Comp.File，受 5~10MB 死区与 200MB 硬限约束
    if platform in _QQ_OFFICIAL_PLATFORMS:
        limits = qq_official_limits()
        hard_mb = limits["file_hard"]
        chunk_min = limits["chunk_min"]
        base64_max = limits["base64_max"]
        if size_mb > hard_mb:
            release_file(path, 10.0)
            return event.plain_result(
                f"音频 {size_mb:.1f}MB 超过 QQ 官方机器人文件硬限 {hard_mb}MB，无法发送。"
            )
        if base64_max < size_mb <= chunk_min:
            release_file(path, 10.0)
            return event.plain_result(
                f"该音频 {size_mb:.1f}MB 处于官方机器人 {base64_max}~{chunk_min}MB 不可发送区间："
                f"适配器仅在超过 {chunk_min}MB 时分片上传，而平台 file_data 上限为 {base64_max}MB。已跳过。"
            )
        release_file(path, 300.0)
        return event.chain_result([Comp.File(name=display_name, file=os.path.abspath(path))])

    # Telegram 等：只有 ≤1MB 才走 Record，否则改发 File（send_document），防止 413 Request Entity Too Large
    if send_mode == "voice" and size_bytes <= VOICE_MAX_MB_NON_ONEBOT * 1024 * 1024:
        voice_path = path
        if ext != "mp3":
            converted = await convert_audio_to_mp3(path, bitrate=VOICE_BITRATE)
            if converted:
                voice_path = converted
        if voice_path.lower().endswith(".mp3"):
            try:
                voice_size = os.path.getsize(voice_path)
            except OSError:
                voice_size = 0
            if voice_size and voice_size <= VOICE_MAX_MB_NON_ONEBOT * 1024 * 1024:
                release_file(voice_path, 180.0)
                return event.chain_result([Comp.Record(file=voice_path)])
            # 转码后超出 1MB：原文件已被 convert_audio_to_mp3 删除，直接用转码产物改发文件
            release_file(voice_path, 300.0)
            mp3_name = os.path.splitext(display_name)[0] + ".mp3"
            return event.chain_result([Comp.File(file=voice_path, name=mp3_name)])

    if send_mode == "voice":
        logger.info(
            f"[R插件][小宇宙] 当前平台语音限制或音频较大（{size_mb:.1f}MB），改发文件：{display_name}"
        )

    release_file(path, 300.0)
    return event.chain_result([Comp.File(file=path, name=display_name)])


async def _send_audio(
    event: AstrMessageEvent,
    path: str,
    ext: str,
    *,
    send_mode: str,
    display_name: str,
    ep: EpisodeInfo,
    source_url: str,
) -> Any:
    """发送小宇宙单集音频，按平台能力分流并在失败时自动降级"""
    api = _onebot_api(event)
    if api is None:
        return await _deliver_via_component(event, path, ext, display_name, send_mode)

    current_path = path
    current_ext = ext
    current_name = display_name

    if send_mode == "card":
        pushed, card_err = await _push_card(event, ep, source_url)
        if pushed:
            release_file(current_path, 10.0)
            return None
        logger.warning(f"[R插件][小宇宙] 卡片发送失败，回退语音：{card_err}")
        send_mode = "voice"

    if send_mode == "voice":
        voice_ready = current_path
        if current_ext != "mp3":
            converted = await convert_audio_to_mp3(current_path, bitrate=VOICE_BITRATE)
            if converted:
                # convert_audio_to_mp3 成功会删掉原文件，后续降级必须用新路径与新后缀
                current_path = converted
                current_ext = "mp3"
                current_name = os.path.splitext(current_name)[0] + ".mp3"
                voice_ready = converted
            else:
                logger.warning(f"[R插件][小宇宙] 转码 mp3 失败，改发文件：{current_path}")
                voice_ready = ""

        if voice_ready:
            pushed, voice_err, used_url = await _push_voice(event, voice_ready)
            if pushed:
                if used_url:
                    release_video(voice_ready)
                else:
                    release_file(voice_ready, 10.0)
                return None
            logger.warning(f"[R插件][小宇宙] 语音发送失败（{voice_err}），自动降级为文件")

        pushed, file_err, used_url = await _push_file(
            event, current_path, current_name, source_url=source_url
        )
        if pushed:
            if used_url:
                release_video(current_path)
            else:
                release_file(current_path, 10.0)
            return None

        release_file(current_path, 10.0)
        return event.plain_result(
            f"小宇宙音频发送失败（{file_err}）。\n"
            "可以在 Web 面板调高「音频体积上限」、配置「视频直发文件服务地址（VIDEO_URL_BASE）」，或将发送方式改为「卡片」。"
        )

    # send_mode == "file"
    pushed, file_err, used_url = await _push_file(
        event, current_path, current_name, source_url=source_url
    )
    if pushed:
        if used_url:
            release_video(current_path)
        else:
            release_file(current_path, 10.0)
        return None

    # 主动选文件失败时，再试一次转码发语音
    logger.warning(f"[R插件][小宇宙] 文件上传失败（{file_err}），尝试回退语音")
    if current_ext != "mp3":
        converted = await convert_audio_to_mp3(current_path, bitrate=VOICE_BITRATE)
        if converted:
            current_path = converted
            current_ext = "mp3"
    if current_path.lower().endswith(".mp3"):
        pushed, _voice_err, used_url = await _push_voice(event, current_path)
        if pushed:
            if used_url:
                release_video(current_path)
            else:
                release_file(current_path, 10.0)
            return None

    release_file(current_path, 10.0)
    return event.plain_result(
        f"音频文件发送失败（{file_err}）。\n"
        "可以在 Web 面板把「发送方式」改成「卡片」或配置「视频直发文件服务地址（VIDEO_URL_BASE）」。"
    )


# ------------------------------------------------------------------ 编排入口


async def process_xiaoyuzhou_url(
    event: AstrMessageEvent,
    *,
    identify_prefix: str = "识别：",
    display_cover: bool = True,
    display_info: bool = True,
    display_intro: bool = True,
    intro_len_limit: int = 120,
    send_mode: str = "voice",
    duration_limit: int = 0,
    audio_max_mb: int = 50,
    access_token: str = "",
    refresh_token: str = "",
    device_id: str = "",
) -> AsyncGenerator[Any, None]:
    """小宇宙链接解析入口（单集 / 播客 / 短链）"""
    text = get_message_text(event)
    target = parse_xiaoyuzhou_target(text)
    if not target:
        return

    logger.info(f"[R插件][小宇宙] 收到目标：{target.kind} {target.target_id}")

    if target.kind == "short":
        final_url = await resolve_redirect(
            target.target_id, headers={"User-Agent": COMMON_USER_AGENT}
        )
        parsed = parse_xiaoyuzhou_target(final_url) if final_url else None
        if not parsed or parsed.kind == "short":
            yield event.plain_result("小宇宙解析失败：短链接里没有拿到单集或播客 ID")
            return
        target = parsed

    # ---------------- 播客主页 ----------------
    if target.kind == "podcast":
        pod = await fetch_podcast_web(target.target_id)
        if pod is None and (access_token or refresh_token):
            pod = await fetch_podcast_api(
                target.target_id,
                access_token=access_token,
                refresh_token=refresh_token,
                device_id=device_id,
            )
        if pod is None:
            yield event.plain_result("小宇宙播客解析失败：未获取到节目信息")
            return

        info_text = build_podcast_info_text(
            pod,
            identify_prefix=identify_prefix,
            display_info=display_info,
            display_intro=display_intro,
            intro_len_limit=intro_len_limit,
        )
        yield await cover_text_result(
            event,
            pod.cover if display_cover else "",
            info_text,
            referer=XIAOYUZHOU_REFERER,
            prefix="xyz_pod_cover",
        )
        return

    # ---------------- 单集 ----------------
    ep = await fetch_episode_web(target.target_id)
    if ep is None and (access_token or refresh_token):
        ep = await fetch_episode_api(
            target.target_id,
            access_token=access_token,
            refresh_token=refresh_token,
            device_id=device_id,
        )
    if ep is None:
        yield event.plain_result("小宇宙单集解析失败：未获取到单集信息（链接可能已失效）")
        return

    # 付费/私密单集：如没有私有直链，尝试调 /v1/private-media/get
    if ep.is_private and "private-media.xyzcdn.net" not in (ep.audio_url or ""):
        if access_token or refresh_token:
            private_url = await fetch_private_media_url(
                ep.eid,
                access_token=access_token,
                refresh_token=refresh_token,
                device_id=device_id,
            )
            if private_url:
                ep.audio_url = private_url

    # 发送模式归一化
    mode = send_mode_from_setting(send_mode)

    # 如果体积未知，提前 HEAD 探测一下，既能展示体积，也能在下载前拦截超大音频
    if ep.audio_url and ep.size_bytes <= 0:
        ep.size_bytes = await _probe_audio_size(ep.audio_url)

    info_text = build_episode_info_text(
        ep,
        identify_prefix=identify_prefix,
        display_info=display_info,
        display_intro=display_intro,
        intro_len_limit=intro_len_limit,
    )

    # 时长限制检查（0 或负数代表不限制；卡片模式不下载，直接继续发卡）
    dur_limit = _as_int(duration_limit)
    if mode != "card" and dur_limit > 0 and ep.duration > dur_limit:
        yield await cover_text_result(
            event,
            ep.cover if display_cover else "",
            info_text + limit_notice(ep.duration, dur_limit, kind="音频"),
            referer=XIAOYUZHOU_REFERER,
            prefix="xyz_cover",
        )
        return

    yield await cover_text_result(
        event,
        ep.cover if display_cover else "",
        info_text,
        referer=XIAOYUZHOU_REFERER,
        prefix="xyz_cover",
    )

    if ep.is_private and not ep.audio_url:
        yield event.plain_result(
            "该单集为付费/私密内容：如需解析音频，请在 Web 面板配置已购买账号的 XIAOYUZHOU_ACCESS_TOKEN / REFRESH_TOKEN。"
        )
        return

    if not ep.audio_url:
        yield event.plain_result("未获取到小宇宙音频直链，可能是付费单集或已下架")
        return

    # 卡片模式：无需下载整期音频，直接发送自定义音乐卡片
    api = _onebot_api(event)
    if mode == "card" and api is not None:
        pushed, card_err = await _push_card(event, ep, ep.audio_url)
        if pushed:
            return
        logger.warning(f"[R插件][小宇宙] 卡片发送失败（{card_err}），改用下载后发语音")
        mode = "voice"

    # 体积上限检查（0 或负数代表不限制）
    max_mb = _as_int(audio_max_mb)
    max_bytes = max(0, max_mb) * 1024 * 1024
    if max_bytes and ep.size_bytes > max_bytes:
        yield event.plain_result(
            f"音频 {format_size(ep.size_bytes)} 超过上限 {max_mb}MB，未下载。\n"
            "可以在 Web 面板调高「小宇宙音频体积上限」（填 0 不限制），或将「发送方式」改为「卡片」。"
        )
        return

    ext = _infer_audio_ext(ep.audio_url, ep.mime_type)
    path = await download_file(
        ep.audio_url,
        build_temp_path("xiaoyuzhou", f".{ext}"),
        headers={"Referer": XIAOYUZHOU_REFERER, "User-Agent": COMMON_USER_AGENT},
        referer=XIAOYUZHOU_REFERER,
        timeout=300,
    )
    if not path:
        yield event.plain_result("小宇宙音频下载失败，请稍后重试")
        return

    # 预检未拿到大小时，落盘后复检真实体积
    try:
        actual_size = os.path.getsize(path)
    except OSError:
        actual_size = 0
    if max_bytes and actual_size > max_bytes:
        delete_file(path)
        yield event.plain_result(
            f"音频 {format_size(actual_size)} 超过上限 {max_mb}MB，已跳过发送。\n"
            "可以在 Web 面板调高「小宇宙音频体积上限」（填 0 不限制），或将「发送方式」改为「卡片」。"
        )
        return

    base_name = safe_filename(
        f"{ep.podcast_title}-{ep.title}" if ep.podcast_title and ep.title else (ep.title or ep.eid),
        default="xiaoyuzhou",
    )
    display_name = f"{base_name}.{ext}"
    result = await _send_audio(
        event,
        path,
        ext,
        send_mode=mode,
        display_name=display_name,
        ep=ep,
        source_url=ep.audio_url,
    )
    if result is not None:
        yield result
