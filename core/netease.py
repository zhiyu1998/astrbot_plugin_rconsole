"""
网易云音乐链接解析

来源：Yunzai 版 rconsole-plugin 的 `apps/songRequest.js` + `utils/music-platform/netease.js`
及其依赖的自建 NeteaseCloudMusicApi。原版把「编排、HTTP、业务、渲染、上传」揉在一个
1118 行的类里，还带模块级可变状态和四处逐字复制的卡片解析；这里只保留解析这一条链路：

    端点常量   constants/netease.py
    传输层     NeteaseClient           统一超时 / 重试 / 错误归一
    纯函数层   parse_* build_* format_* 无 IO，可单测
    业务层     resolve_audio / fetch_song_detail / fetch_wiki_tags / temp_api_resolve
    编排层     process_netease_url

相对原版修掉的关键问题（都是实测出来的，不是猜的）：

1. **Cookie 活性必须缓存**。原版每次解析都打一次 `/login/status`，同一个群连续发几个
   链接就放大成 N 次探测；这里按 TTL 缓存并对并发去重。
2. **「接口返回了 url」≠「拿到了想要的音质」**。实测 Cookie 失效时 `/song/url/v1?level=jymaster`
   会**静默降级**成 exhigh/mp3 且 `payed=0`，而不是返回空 url。原版只看 url 是否为空，
   于是「ck 失效 → 其实已经拿到 exhigh → 又白跑一遍临时接口」。这里回读响应里的
   `level` 判断真实结果，只有真的拿不到才兜底。
3. **音质回退链 + 体积上限**。母带单曲实测 208MB，直接下载会拖垮群文件与带宽。
   拿不到目标档位或体积超限时逐级下降，而不是整体失败。
4. **播客**：搜索接口给的是 UI 结构，没有原版假设的 `baseInfo.mainSong`；
   这里先用 `/dj/program/detail` 换 `mainSong.id` 再取音源。
5. **试听片段**：`freeTrialInfo` 非空的 30 秒片段不能当完整歌曲发出去，原版没有判断。
6. **不留模块级可变状态**。原版 `let FileSuffix = 'flac'` 是全局可变的，多人同时解析会
   互相覆盖后缀；这里后缀只跟着单次解析结果走。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.netease import (
    AUDIO_EXTENSIONS,
    BROKEN_COVER_MARKERS,
    DEFAULT_COVER,
    DEFAULT_QUALITY,
    EP_DJ_PROGRAM_DETAIL,
    EP_LOGIN_STATUS,
    EP_SONG_DETAIL,
    EP_SONG_URL_V1,
    EP_SONG_WIKI_SUMMARY,
    GUEST_MAX_QUALITY,
    QUALITY_FROM_LABEL,
    QUALITY_LABELS,
    PROGRAM_PAGE,
    QUALITY_ORDER,
    SONG_PAGE,
    SPATIAL_QUALITIES,
    TEMP_AUDIO_APIS,
)
from ..constants.tools import COMMON_USER_AGENT
from .common import (
    build_temp_path,
    convert_audio_to_mp3,
    cover_text_result,
    download_file,
    get_message_text,
    release_file,
    resolve_redirect,
    safe_filename,
)

# ------------------------------------------------------------------ 常量

COOKIE_CACHE_TTL = 600.0

# 音质回退链最多试几档，避免 VIP 掉权时把整条链打满
MAX_QUALITY_ATTEMPTS = 5

# 网易云 CDN 一般不校验 Referer，但带上更稳
NETEASE_REFERER = "https://music.163.com/"

# 文件消息走 base64（NapCat 跑在容器里，读不到宿主机绝对路径），
# 超过这个体积改成提示，避免把几十 MB 的 base64 塞进协议端
FILE_BASE64_MAX_MB = 20

# 非 OneBot 平台（Telegram 等）发语音的体积上限。
# Telegram 的 sendVoice 对体积/时长限制很严，实测 5.4MB 的 mp3 直接被 413
# （NetworkError: Request Entity Too Large）拒掉，而且 AstrBot 的 fallback
# 只处理「隐私设置不让收语音」那一类错误，不会退回文档。
VOICE_MAX_MB_NON_ONEBOT = 1.0

_URL_RE = re.compile(r"https?://[^\s\"'<>（）()【】\[\]]+", re.IGNORECASE)
_SHORT_HOST_RE = re.compile(r"(?:^|//)163cn\.tv/([A-Za-z0-9]+)", re.IGNORECASE)
_ID_IN_QUERY_RE = re.compile(r"[?&#]id=(\d+)")
_ID_IN_PATH_RE = re.compile(r"/(?:song|program|dj)/(\d+)")
_PROGRAM_PATH_RE = re.compile(r"/(?:dj|program)(?:/|\?|#|$)", re.IGNORECASE)
# 只有 song / program / dj 页面才当目标。歌单(playlist)、专辑(album)、歌手(artist)、
# 榜单(toplist) 的 id 是**另一套号段**，拿去 /song/detail 可能正好命中某首不相干的歌，
# 于是「发的是歌单，收到的是一首别人的曲子」且毫无提示。
_SINGLE_PATH_RE = re.compile(r"(?:^|/|#/)(?:song|program|dj)(?:/|\?|&|$)", re.IGNORECASE)
_AUDIO_FILE_RE = re.compile(r"\.(" + "|".join(AUDIO_EXTENSIONS) + r")$", re.IGNORECASE)


class NeteaseError(Exception):
    """网易云接口 / 配置错误"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str = "",
        status: int = 0,
        code: Any = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.code = code


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class SongTarget:
    """从文本里解析出来的目标"""

    kind: str  # song / program / short
    target_id: str
    raw: str = ""


@dataclass
class Song:
    """统一歌曲结构"""

    id: str
    name: str = ""
    artist: str = ""
    duration_ms: int = 0
    cover: str = ""
    kind: str = "song"  # song / podcast
    program_id: str = ""

    def display_title(self) -> str:
        if self.artist and self.name:
            return f"{self.artist} - {self.name}"
        return self.name or self.artist or "未知歌曲"

    def page_url(self) -> str:
        """只在「音乐卡片」的 url 字段里用，不要拼进文字消息（会引发跨机器人循环）"""
        if self.kind == "podcast" and self.program_id:
            return PROGRAM_PAGE.format(self.program_id)
        return SONG_PAGE.format(self.id)


@dataclass
class AudioSource:
    """一次解析拿到的音源"""

    url: str = ""
    ext: str = "mp3"
    size_bytes: int = 0
    level: str = ""
    source: str = "api"  # api / temp
    oversize: bool = False
    warnings: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ 纯函数


def normalize_base_url(base: str) -> str:
    """把配置里的 API 地址归一化：补协议、去尾斜杠"""
    value = (base or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "http://" + value
    return value.rstrip("/")


def _https(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("http://"):
        return "https://" + raw[len("http://") :]
    return raw


def parse_netease_target(text: str) -> Optional[SongTarget]:
    """
    从一段文本里找出网易云的目标。

    支持：
      * https://music.163.com/song?id=xxx
      * https://music.163.com/#/song?id=xxx
      * https://music.163.com/m/song?id=xxx
      * https://y.music.163.com/m/song?id=xxx&uct2=...
      * https://music.163.com/dj?id=xxx（播客） / /program?id=xxx
      * https://163cn.tv/xxxxxx（短链，需要再跟一次跳转）

    QQ 分享的网易云音乐卡片经过 card 补丁注入后会带 `jumpUrl`，
    同样是 music.163.com 链接，所以卡片和纯链接走同一条路径。
    """
    if not text:
        return None

    for raw in _URL_RE.findall(text):
        url = raw.rstrip("，。、,)）]】")
        host_match = re.search(r"^https?://([^/?#]+)", url, re.IGNORECASE)
        host = (host_match.group(1) if host_match else "").lower()
        if not host:
            continue
        if "163cn.tv" in host:
            return SongTarget("short", url, url)
        if "music.163.com" not in host:
            continue
        if not _SINGLE_PATH_RE.search(url):
            continue
        id_match = _ID_IN_QUERY_RE.search(url) or _ID_IN_PATH_RE.search(url)
        if not id_match:
            continue
        kind = "program" if _PROGRAM_PATH_RE.search(url) else "song"
        return SongTarget(kind, id_match.group(1), url)

    short = _SHORT_HOST_RE.search(text)
    if short:
        return SongTarget("short", f"https://163cn.tv/{short.group(1)}", short.group(0))
    return None


def quality_label(level: str) -> str:
    """英文档位转中文"""
    value = (level or "").strip().lower()
    return QUALITY_LABELS.get(value, value)


def send_mode_from_setting(value: Any) -> str:
    """
    面板取值 -> 发送方式：card / voice / file。

    兼容历史形态：早先是 bool（True=语音，False=文件），后来是「语音 / 文件」下拉，
    现在扩展成「卡片 / 语音 / 文件」三选一。
    """
    if isinstance(value, bool):
        return "voice" if value else "file"
    text = str(value or "").strip().lower()
    if text in ("卡片", "card", "music"):
        return "card"
    if text in ("语音", "voice", "true", "1", "开"):
        return "voice"
    return "file"


def quality_from_setting(value: str) -> str:
    """面板里存的是中文标签，也可能是历史遗留的英文档位"""
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_QUALITY
    if raw.lower() in QUALITY_LABELS:
        return raw.lower()
    if raw in QUALITY_FROM_LABEL:
        return QUALITY_FROM_LABEL[raw]
    return DEFAULT_QUALITY


def build_quality_chain(
    preferred: str,
    *,
    cookie_alive: bool = True,
    max_attempts: int = MAX_QUALITY_ATTEMPTS,
) -> List[str]:
    """
    生成音质回退链。

    * 未登录 / Cookie 失效时先从首选档位夹到 exhigh —— 请求母带只会被静默降级，白花一次往返
    * 空间音频（杜比/环绕）没有逐级降级的概念，直接回落到无损 → 极高 → 标准
    """
    level = quality_from_setting(preferred or DEFAULT_QUALITY)
    if not cookie_alive:
        guest_index = QUALITY_ORDER.index(GUEST_MAX_QUALITY)
        if level in QUALITY_ORDER and QUALITY_ORDER.index(level) < guest_index:
            level = GUEST_MAX_QUALITY
        elif level not in QUALITY_ORDER:
            level = GUEST_MAX_QUALITY

    if level in SPATIAL_QUALITIES:
        chain = [level, "lossless", "exhigh", "standard"]
    else:
        start = QUALITY_ORDER.index(level) if level in QUALITY_ORDER else 0
        chain = list(QUALITY_ORDER[start:])

    deduped: List[str] = []
    for item in chain:
        if item not in deduped:
            deduped.append(item)

    limit = max(1, int(max_attempts or 1))
    if len(deduped) > limit:
        # QUALITY_ORDER 是从高到低，直接 [:limit] 砍的是**链尾**。
        # 选「超清母带」时 standard 会被砍掉，于是「只有标准音源」的老歌/冷门歌
        # 全档失败，用户看到的是「没有拿到可用音源（无版权/需会员）」这个假原因。
        # 所以末尾始终留一档兜底。
        deduped = deduped[: limit - 1] + [deduped[-1]]
    return deduped


def _as_int(value: Any, default: int = 0) -> int:
    """
    容错转 int。

    接口里 `dt` / `duration` / `size` 偶尔是 `"123.4"` 或 `"3m20s"`；
    直接 `int()` 抛 ValueError 会把整条 detail 丢掉（标题、歌手、封面全没了），
    播客那边还会误报「声音不存在或已下架」。
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def format_size(size_bytes: int) -> str:
    """字节转 MB 字符串；无效值返回空串"""
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    return f"{size / 1024 / 1024:.2f} MB"


def format_duration(duration_ms: int) -> str:
    """毫秒转 mm:ss；超过一小时转成 h:mm:ss（播客常见）"""
    total = _as_int(duration_ms) // 1000
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def normalize_cover(url: str) -> str:
    """封面归一化：占位图 / 空值 → 默认封面，http → https"""
    raw = (url or "").strip()
    if not raw or raw == "def":
        return DEFAULT_COVER
    if any(marker in raw for marker in BROKEN_COVER_MARKERS):
        return DEFAULT_COVER
    return _https(raw)


def is_trial_audio(free_trial_info: Any) -> bool:
    """
    `freeTrialInfo` 是否表示「只有试听片段」。

    实测有两种形态：`{"start": 0, "end": 30}` 和 `[{"start": ...}]`；
    另外 `{"start": null, "end": null}` 是**无**试听限制，不能当试听。
    只看 key 存在会把「key 在但值为 null」的整首好歌误丢掉。
    """
    if isinstance(free_trial_info, (list, tuple)):
        return any(is_trial_audio(item) for item in free_trial_info)
    if not isinstance(free_trial_info, dict):
        return False
    for key in ("start", "end", "trialStart", "trialEnd"):
        if key not in free_trial_info:
            continue
        try:
            int(free_trial_info[key])
        except (TypeError, ValueError):
            continue
        return True
    return False


def _is_trial_free(free_trial_info: Any) -> bool:
    """有些接口返回空字典 {}，那是「无试听限制」而不是试听"""
    return bool(free_trial_info) and is_trial_audio(free_trial_info)


def parse_wiki_tags(payload: Any, limit: int = 5) -> List[str]:
    """
    解析 `/song/wiki/summary` 的标签。

    原版在字段缺失时也照样 push，结果 `tags.push(undefined)`；
    后面靠 `tags.pop()` 去掉末尾音质标签，一旦百科没返回标签就误删了真实标签。
    这里只收集真正有值的字符串，调用方按需再拼音质。
    """
    tags: List[str] = []

    def push(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in tags:
            tags.append(text)

    blocks = ((payload or {}).get("data") or {}).get("blocks")
    if not isinstance(blocks, list):
        return tags
    if len(blocks) < 2:
        return tags
    creatives = blocks[1].get("creatives") if isinstance(blocks[1], dict) else None
    if not isinstance(creatives, list) or not creatives:
        return tags

    first = creatives[0] if isinstance(creatives[0], dict) else {}
    resources = first.get("resources")
    if isinstance(resources, list) and resources:
        push(((resources[0] or {}).get("uiElement") or {}).get("mainTitle", {}).get("title"))
    else:
        push((first.get("uiElement") or {}).get("mainTitle", {}).get("title"))

    rec = creatives[1] if len(creatives) > 1 and isinstance(creatives[1], dict) else {}
    rec_resources = rec.get("resources")
    if isinstance(rec_resources, list) and rec_resources:
        for resource in rec_resources[:3]:
            push(((resource or {}).get("uiElement") or {}).get("mainTitle", {}).get("title"))
    else:
        links = (rec.get("uiElement") or {}).get("textLinks") or []
        if links:
            push((links[0] or {}).get("text"))

    third = creatives[2] if len(creatives) > 2 and isinstance(creatives[2], dict) else {}
    third_title = (third.get("uiElement") or {}).get("mainTitle", {}).get("title")
    third_links = (third.get("uiElement") or {}).get("textLinks") or []
    if third_title == "BPM":
        if third_links:
            bpm = (third_links[0] or {}).get("text")
            if bpm:
                push(f"BPM {bpm}")
    elif third_links:
        push((third_links[0] or {}).get("text"))

    return tags[: max(1, int(limit or 5))]


def _extract_temp_audio(payload: Any) -> Optional[AudioSource]:
    """从第三方临时接口的响应里挖出音频直链（各家字段名都不一样）"""
    if not isinstance(payload, dict):
        return None

    candidates: List[Any] = [
        payload.get("music_url"),
        payload.get("url"),
        payload.get("link"),
        payload.get("data") if isinstance(payload.get("data"), str) else None,
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("music_url"), data.get("url"), data.get("link")])

    for candidate in candidates:
        # 这里**不做** http→https 升级：第三方源（kilz 之类）返回的直链很多只有 http，
        # 强行升 https 会 TLS 失败，白白把兜底弄废
        url = str(candidate or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            continue
        if not re.search(r"\.(mp3|flac|m4a|wav|ape|ogg|aac|mp4)(\?|$)", url, re.IGNORECASE):
            # 有些接口给的是不带后缀的 CDN 直链，含 music.126.net 也当音频处理
            if "music.126.net" not in url:
                continue
        match = _AUDIO_FILE_RE.search(url.split("?")[0])
        ext = match.group(1).lower() if match else "mp3"
        size = 0
        for key in ("size", "filesize", "file_size"):
            raw_size = payload.get(key) or (data.get(key) if isinstance(data, dict) else None)
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                continue
            if size > 0:
                break
        return AudioSource(url=url, ext=ext, size_bytes=size, level="", source="temp")
    return None


# ------------------------------------------------------------------ 传输层


def _assert_api_ok(payload: Any, endpoint: str) -> Any:
    """
    接口层错误归一：`code != 200` 一律抛 NeteaseError。

    非 dict 响应（反代返回 `[]`、撞到别的服务、兼容实现换了结构）也在这里抛，
    否则调用方的 `payload.get(...)` 会变成 AttributeError 冒到 handler，
    用户端一个回复都收不到。
    """
    if not isinstance(payload, dict):
        raise NeteaseError(
            f"网易云接口 {endpoint} 返回的不是 JSON 对象（{type(payload).__name__}）",
            endpoint=endpoint,
        )
    code = payload.get("code")
    if code is None:
        return payload
    try:
        ok = int(code) == 200
    except (TypeError, ValueError):
        ok = False
    if not ok:
        detail = payload.get("message") or payload.get("msg") or ""
        raise NeteaseError(
            f"网易云接口 {endpoint} 返回 code={code}{'：' + str(detail) if detail else ''}",
            endpoint=endpoint,
            code=code,
        )
    return payload


class NeteaseClient:
    """
    自建 API 的最小客户端。

    * 统一超时（默认 15s）与重试（默认 2 次，仅对网络错误和 5xx 重试）
    * 4xx / 业务 code 直接抛错，不重试
    * 每个请求独立 httpx client（与插件其它模块保持一致的用法）
    """

    def __init__(
        self,
        base_url: str,
        cookie: str = "",
        *,
        timeout: float = 15.0,
        retries: int = 2,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.cookie = (cookie or "").strip()
        self.timeout = float(timeout or 15.0)
        self.retries = max(0, int(retries))

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        suffix = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{suffix}"
        pairs = []
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            pairs.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
        if pairs:
            url += "?" + "&".join(pairs)
        return url

    def _headers(self, cookie: Optional[str]) -> Dict[str, str]:
        use_cookie = self.cookie if cookie is None else cookie
        headers = {"User-Agent": COMMON_USER_AGENT}
        if use_cookie:
            headers["Cookie"] = use_cookie
        return headers

    async def get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        cookie: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> Any:
        if not self.configured:
            raise NeteaseError(
                "未配置网易云 API 地址（NETEASE_API_SERVER），请先自建 NeteaseCloudMusicApi"
            )

        url = self._url(path, params)
        headers = self._headers(cookie)
        attempts = self.retries if retries is None else max(0, int(retries))
        last_error: Optional[NeteaseError] = None

        for attempt in range(attempts + 1):
            retryable = False
            try:
                async with httpx.AsyncClient(
                    timeout=timeout or self.timeout, follow_redirects=True
                ) as client:
                    resp = await client.get(url, headers=headers)
                if resp.status_code >= 500:
                    retryable = True
                    last_error = NeteaseError(
                        f"网易云接口 {path} 返回 {resp.status_code}",
                        endpoint=path,
                        status=resp.status_code,
                    )
                elif resp.status_code >= 400:
                    raise NeteaseError(
                        f"网易云接口 {path} 返回 {resp.status_code}",
                        endpoint=path,
                        status=resp.status_code,
                    )
                else:
                    try:
                        payload = resp.json()
                    except Exception as exc:  # noqa: BLE001
                        raise NeteaseError(
                            f"网易云接口 {path} 返回的不是 JSON：{exc}",
                            endpoint=path,
                            status=resp.status_code,
                        ) from exc
                    return _assert_api_ok(payload, path)
            except NeteaseError:
                raise
            except Exception as exc:  # noqa: BLE001
                retryable = True
                last_error = NeteaseError(f"网易云接口 {path} 请求失败：{exc}", endpoint=path)

            if not retryable or attempt >= attempts:
                break
            await asyncio.sleep(0.4 * (attempt + 1))

        raise last_error or NeteaseError(f"网易云接口 {path} 请求失败", endpoint=path)


# ------------------------------------------------------------------ Cookie 活性

_COOKIE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_COOKIE_LOCKS: Dict[str, asyncio.Lock] = {}


def _cookie_key(cookie: str) -> str:
    return hashlib.sha256((cookie or "").encode("utf-8", "ignore")).hexdigest()[:16]


async def cookie_status(
    client: NeteaseClient,
    cookie: Optional[str] = None,
    *,
    ttl: float = COOKIE_CACHE_TTL,
    force: bool = False,
) -> Dict[str, Any]:
    """
    检测 Cookie 是否有效，带 TTL 缓存 + 并发去重。

    注意：**探测请求失败不等于 Cookie 失效**。网络抖一下就把音质打回最低
    是最难排查的那类「有时候没无损」的问题，所以失败时沿用上一次的结论。
    """
    raw = (client.cookie if cookie is None else cookie) or ""
    if not raw.strip():
        return {"alive": False, "user_id": "", "nickname": "", "reason": "未配置 Cookie"}

    key = _cookie_key(raw)
    cached = _COOKIE_CACHE.get(key)
    if cached and not force and time.monotonic() - cached[0] < ttl:
        return cached[1]

    lock = _COOKIE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _COOKIE_CACHE.get(key)
        if cached and not force and time.monotonic() - cached[0] < ttl:
            return cached[1]
        try:
            payload = await client.get_json(EP_LOGIN_STATUS, cookie=raw, retries=0)
            profile = ((payload or {}).get("data") or {}).get("profile") or {}
            if profile:
                status = {
                    "alive": True,
                    "user_id": str(profile.get("userId") or ""),
                    "nickname": str(profile.get("nickname") or ""),
                    "reason": "",
                }
                logger.info(
                    f"[R插件][网易云] Cookie 有效（{status['nickname'] or status['user_id']}），"
                    "可解析 VIP 音质"
                )
            else:
                status = {"alive": False, "user_id": "", "nickname": "", "reason": "Cookie 已失效"}
                logger.info("[R插件][网易云] Cookie 失效，音质将被限制在极高，必要时走临时接口")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][网易云] Cookie 检测失败：{exc}")
            if cached:
                return cached[1]
            # 探测失败**不等于**失效。这里给 alive=True 让回退链从首选档开始：
            # 接口自己会静默降级，最多多花一次往返；反过来（当失效处理）
            # 会让网络抖一下就把音质永久压在「极高」，用户以为是插件不识别无损。
            return {
                "alive": True,
                "user_id": "",
                "nickname": "",
                "reason": f"探测失败（按有效处理）：{exc}",
            }
        _COOKIE_CACHE[key] = (time.monotonic(), status)
        return status


def reset_caches() -> None:
    """清空模块级缓存（测试 / 改配置后强制刷新用）"""
    _COOKIE_CACHE.clear()


# ------------------------------------------------------------------ 业务层


async def fetch_song_detail(
    client: NeteaseClient,
    ids: Iterable[Any],
    *,
    cookie: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """批量取歌曲详情，返回 `{id: {name, artists, cover, duration_ms}}`"""
    wanted = [str(item) for item in ids if str(item or "").strip()]
    if not wanted:
        return {}
    payload = await client.get_json(EP_SONG_DETAIL, {"ids": ",".join(wanted)}, cookie=cookie)
    detail: Dict[str, Dict[str, Any]] = {}
    for item in (payload or {}).get("songs") or []:
        if not isinstance(item, dict):
            continue
        song_id = str(item.get("id") or "")
        if not song_id:
            continue
        album = item.get("al") if isinstance(item.get("al"), dict) else {}
        artists = [
            str(a.get("name"))
            for a in (item.get("ar") or item.get("artists") or [])
            if isinstance(a, dict) and a.get("name")
        ]
        detail[song_id] = {
            "name": str(item.get("name") or ""),
            "artists": artists,
            "cover": normalize_cover(album.get("picUrl") or ""),
            "duration_ms": _as_int(item.get("dt") or item.get("duration")),
        }
    return detail


async def fetch_wiki_tags(client: NeteaseClient, song_id: str, *, limit: int = 5) -> List[str]:
    """取歌曲百科标签；失败只记 debug，不影响主链路"""
    if not song_id:
        return []
    try:
        payload = await client.get_json(EP_SONG_WIKI_SUMMARY, {"id": song_id}, retries=0)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][网易云] 百科标签获取失败（忽略）：{exc}")
        return []
    try:
        return parse_wiki_tags(payload, limit)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][网易云] 百科标签解析失败（忽略）：{exc}")
        return []


async def resolve_audio(
    client: NeteaseClient,
    song_id: str,
    *,
    cookie: Optional[str] = None,
    quality: str = DEFAULT_QUALITY,
    max_mb: int = 0,
    allow_fallback: bool = True,
    cookie_alive: Optional[bool] = None,
) -> Optional[AudioSource]:
    """
    解析音源，带回退链。

    返回 None 表示「这条歌在这个 API / Cookie 下拿不到可用音源」，
    调用方再决定是否走第三方临时接口。
    """
    if not song_id:
        return None

    if cookie_alive is None:
        status = await cookie_status(client, cookie)
        cookie_alive = bool(status.get("alive"))

    chain = build_quality_chain(
        quality,
        cookie_alive=cookie_alive,
        max_attempts=MAX_QUALITY_ATTEMPTS if allow_fallback else 1,
    )
    max_bytes = max(0, int(max_mb or 0)) * 1024 * 1024
    oversize_hit: Optional[AudioSource] = None
    reasons: List[str] = []

    for level in chain:
        try:
            payload = await client.get_json(
                EP_SONG_URL_V1, {"id": song_id, "level": level}, cookie=cookie
            )
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{level}: {exc}")
            continue

        data = (payload or {}).get("data") or []
        audio = data[0] if isinstance(data, list) and data else None
        if not isinstance(audio, dict):
            reasons.append(f"{level}: 接口没有返回音源")
            continue

        url = _https(str(audio.get("url") or ""))
        if not url:
            trial = _is_trial_free(audio.get("freeTrialInfo"))
            reasons.append(f"{level}: {'仅试听片段' if trial else '无版权或该档位不可用'}")
            continue
        if _is_trial_free(audio.get("freeTrialInfo")):
            reasons.append(f"{level}: 仅试听片段")
            continue

        actual_level = str(audio.get("level") or level)
        size_bytes = _as_int(audio.get("size"))

        # `type` 并不总是容器格式：母带/Dolby 会出现 "mp4"、某些实现给 "flac" 但实际是 mp3。
        # 依次看 URL 后缀 → type → encodeType，最后才退回 mp3（后缀错了会让语音播不出来）。
        url_ext = _AUDIO_FILE_RE.search(url.split("?")[0])
        if url_ext:
            ext = url_ext.group(1).lower()
        else:
            ext = str(audio.get("type") or audio.get("encodeType") or "").lower()
        if ext not in AUDIO_EXTENSIONS:
            ext = "mp3"

        if actual_level != level:
            # Cookie 掉权 / 会员过期时接口会静默降级，这里只提示，不打断
            logger.info(
                f"[R插件][网易云] 请求 {quality_label(level)} 实际返回 "
                f"{quality_label(actual_level)}（Cookie 权限不足或该档位不可用）"
            )

        source = AudioSource(
            url=url,
            ext=ext,
            size_bytes=size_bytes,
            level=actual_level,
            source="api",
        )
        if max_bytes and size_bytes > max_bytes:
            reasons.append(f"{level}: {format_size(size_bytes)} 超过上限 {max_mb}MB")
            if oversize_hit is None:
                source.oversize = True
                oversize_hit = source
            continue
        return source

    if reasons:
        logger.info(f"[R插件][网易云] 音源回退链全部失败：{'；'.join(reasons)}")
    return oversize_hit


async def temp_api_resolve(
    artist: str,
    name: str,
    *,
    timeout: float = 20.0,
    apis: Sequence[str] = TEMP_AUDIO_APIS,
) -> Optional[AudioSource]:
    """
    第三方临时接口兜底（多个源依次尝试）。

    这些接口本来就时灵时不灵，任何异常都只记 debug。
    """
    title = f"{artist} {name}".strip() or (name or "").strip()
    if not title:
        return None
    keyword = quote(title.replace("-", " "), safe="")

    for template in apis:
        url = template.format(keyword)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
                resp = await http.get(url, headers={"User-Agent": COMMON_USER_AGENT})
            if resp.status_code >= 400:
                continue
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][网易云] 临时接口失败 {template.split('?')[0]}：{exc}")
            continue
        source = _extract_temp_audio(payload)
        if source:
            logger.info(f"[R插件][网易云] 已使用临时音源：{template.split('?')[0]}")
            source.warnings.append("已使用第三方临时音源接口")
            return source
    return None


async def fetch_program_song(
    client: NeteaseClient,
    program_id: str,
    *,
    cookie: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """播客（声音）→ mainSong：音源接口只认歌曲 id，不认声音 id"""
    if not program_id:
        return None
    payload = await client.get_json(EP_DJ_PROGRAM_DETAIL, {"id": program_id}, cookie=cookie)
    program = (payload or {}).get("program") or {}
    if not isinstance(program, dict):
        return None
    main_song = program.get("mainSong") if isinstance(program.get("mainSong"), dict) else {}
    song_id = str(main_song.get("id") or "")
    if not song_id:
        return None
    album = main_song.get("album") if isinstance(main_song.get("album"), dict) else {}
    artists = [
        str(a.get("name"))
        for a in (main_song.get("artists") or main_song.get("ar") or [])
        if isinstance(a, dict) and a.get("name")
    ]
    return {
        "id": song_id,
        "name": str(main_song.get("name") or program.get("name") or ""),
        "artists": artists,
        "cover": normalize_cover(str(album.get("picUrl") or program.get("coverUrl") or "")),
        "duration_ms": _as_int(main_song.get("duration") or program.get("duration")),
        "program_name": str(program.get("name") or ""),
    }


# ------------------------------------------------------------------ 编排层


def build_info_text(
    song: Song,
    source: Optional[AudioSource],
    tags: Sequence[str],
    identify_prefix: str,
) -> str:
    """拼「识别：…」信息块"""
    lines: List[str] = []
    kind_label = "播客" if song.kind == "podcast" else "音乐"
    lines.append(f"{identify_prefix}网易云{kind_label}：{song.display_title()}")

    meta: List[str] = []
    duration = format_duration(song.duration_ms)
    if duration:
        meta.append(f"时长 {duration}")
    if source is not None:
        label = quality_label(source.level)
        if label:
            meta.append(label)
        size = format_size(source.size_bytes)
        if size:
            meta.append(size)
    if meta:
        lines.append(" · ".join(meta))

    if tags:
        lines.append("标签：" + " / ".join(tags))
    # 注意：这里**不能**拼页面链接。机器人发出带 music.163.com 的文本后，
    # 其它机器人会再次命中路由，形成互相识别死循环。
    return "\n".join(lines)


def _onebot_api(event: AstrMessageEvent) -> Any:
    """拿到 OneBot(aiocqhttp) 的 api 对象；其它平台返回 None。"""
    return getattr(getattr(event, "bot", None), "api", None)


async def _deliver_via_component(
    event: AstrMessageEvent,
    path: str,
    ext: str,
    display_name: str,
    send_mode: str,
) -> Any:
    """
    非 OneBot 平台（Telegram / Discord / QQ 官方等）走 AstrBot 的消息组件。

    这些适配器对 Comp.File 有自己的实现（Telegram 是 send_document），
    能直接吃本地路径，**不存在 NapCat 那种容器看不到路径的问题**。
    """
    if send_mode in ("card", "voice"):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        # 语音：只有体积足够小才走 Record，否则大音频会被协议端直接拒
        if send_mode == "voice" and size and size <= VOICE_MAX_MB_NON_ONEBOT * 1024 * 1024:
            voice_path = path
            if ext != "mp3":
                converted = await convert_audio_to_mp3(path)
                if converted:
                    voice_path = converted
            if voice_path.lower().endswith(".mp3"):
                # 转码后体积可能反而变大（flac→320k mp3），复查一次再决定发不发语音
                try:
                    voice_size = os.path.getsize(voice_path)
                except OSError:
                    voice_size = 0
                if voice_size and voice_size <= VOICE_MAX_MB_NON_ONEBOT * 1024 * 1024:
                    release_file(voice_path, 180.0)
                    return event.chain_result([Comp.Record(file=voice_path)])
                if voice_path != path:
                    release_file(voice_path, 10.0)
        logger.info(
            "[R插件][网易云] 当前平台不支持音乐卡片或语音发不了（音频"
            f"{size / 1024 / 1024:.1f}MB），改发文件"
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
    song: Song,
    source: Optional[AudioSource],
) -> Any:
    """
    发送音频。

    * 语音：aiocqhttp 适配器会把 Record 转成 base64 再发，NapCat 在容器里也能收到；
      但容器格式（flac/ape）对端不一定认，所以先转 320k mp3。
    * 文件：非 mp3 且转码失败时退回文件发送。
    """
    api = _onebot_api(event)
    if api is None:
        # Telegram 等平台没有 OneBot 接口，卡片与群文件上传都无从谈起，
        # 交给组件层：Telegram 的 Comp.File 会走 send_document
        return await _deliver_via_component(event, path, ext, display_name, send_mode)

    if send_mode == "card":
        pushed, error = await _push_card(event, song, source)
        if pushed:
            release_file(path, 10.0)
            return None
        logger.warning(f"[R插件][网易云] 卡片发送失败，回退语音：{error}")
        send_mode = "voice"

    if send_mode == "voice":
        voice_path = path
        if ext != "mp3":
            converted = await convert_audio_to_mp3(path)
            if converted:
                voice_path = converted
        if voice_path and voice_path.lower().endswith(".mp3"):
            release_file(voice_path, 180.0)
            return event.chain_result([Comp.Record(file=voice_path)])
        logger.warning(f"[R插件][网易云] 转码失败，改发文件：{path}")

    pushed, error = await _push_file(event, path, display_name)
    if pushed:
        release_file(path, 10.0)
        return None

    # 文件发不出去（体积超限 / 协议端拒绝）就退化成语音，至少能听到
    converted = await convert_audio_to_mp3(path)
    if converted:
        release_file(converted, 180.0)
        logger.info("[R插件][网易云] 文件发送失败，已回退为语音")
        return event.chain_result([Comp.Record(file=converted)])

    return _file_result(event, path, error)


async def _push_card(
    event: AstrMessageEvent,
    song: Song,
    source: Optional[AudioSource],
) -> Tuple[bool, str]:
    """
    用 OneBot 音乐卡片发出去，返回 (是否成功, 失败原因)。

    同样绕开 `Comp.Music`：BaseMessageComponent.toDict() 按字段名序列化，
    而 Music 的关键字段叫 `_type`，发出去会变成 `data._type`，
    协议端认 `data.type`，卡片会直接坏掉（和 Comp.File 的 `file_` 是同一类坑）。

    * 官方歌曲 -> `{"type": "163", "id": ...}`：QQ 原生网易云卡片，不依赖音源直链
    * 云盘 / 播客 -> `{"type": "custom", ...}`：需要 audio 直链，拿不到就只能退回语音/文件
    """
    api = getattr(getattr(event, "bot", None), "api", None)
    if api is None:
        return False, "当前平台没有 OneBot 接口"

    if song.kind == "song" and song.id:
        data: Dict[str, Any] = {"type": "163", "id": str(song.id)}
    else:
        audio = getattr(source, "url", "") or ""
        if not audio:
            return False, "没有可用音源，无法拼自定义卡片"
        data = {
            "type": "custom",
            "url": song.page_url(),
            "audio": audio,
            "title": song.name or "网易云音乐",
            "image": song.cover or DEFAULT_COVER,
            "content": song.artist or "",
        }

    message = [{"type": "music", "data": data}]
    group_id = None
    try:
        group_id = event.get_group_id()
    except Exception:  # noqa: BLE001
        group_id = None

    try:
        if group_id:
            await api.call_action("send_group_msg", group_id=int(group_id), message=message)
        else:
            await api.call_action(
                "send_private_msg", user_id=int(event.get_sender_id()), message=message
            )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][网易云] 发送音乐卡片失败：{exc}")
        return False, str(exc)


async def _push_file(event: AstrMessageEvent, path: str, display_name: str) -> Tuple[bool, str]:
    """
    用 OneBot API 直接把音频作为**文件**发出去，返回 (是否成功, 失败原因)。

    为什么不用 `Comp.File`：AstrBot 的 `BaseMessageComponent.toDict()` 按**字段名**
    序列化，而 File 的字段是 `file_`（带下划线），实际发出去是
    `{"type": "file", "data": {"file_": "base64://..."}}`，
    NapCat 找 `data.file` 找不到，直接回 `retcode=1200 文件消息缺少参数`（实测踩过）。

    也不能传宿主机绝对路径：aiocqhttp 会转成 `file://` URI，而 NapCat 跑在容器里
    只挂了两个 volume，看不到 /home/AstrBot 下的临时文件，会回 ENOENT。

    所以这里绕开组件层，直接调 OneBot 接口并自带 base64 内容。
    """
    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        return False, "音频文件已失效"

    if size_bytes > FILE_BASE64_MAX_MB * 1024 * 1024:
        return False, (
            f"音频 {size_bytes / 1024 / 1024:.1f}MB 超过文件消息上限 {FILE_BASE64_MAX_MB}MB"
        )

    if size_bytes > 12 * 1024 * 1024:
        # base64 会膨胀约 1/3，20MB 音频 → 27MB 的单条 ws 消息，协议端可能直接断连
        logger.warning(
            f"[R插件][网易云] 文件 {size_bytes / 1024 / 1024:.1f}MB 走 base64 上传，"
            "体积偏大；失败时建议改用「语音」或调低音质"
        )
    try:
        with open(path, "rb") as fp:
            payload = "base64://" + base64.b64encode(fp.read()).decode()
    except OSError as exc:
        return False, f"读取音频失败：{exc}"

    api = getattr(getattr(event, "bot", None), "api", None)
    if api is None:
        return False, "当前平台没有 OneBot 接口"

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
                file=payload,
                name=display_name,
            )
        else:
            await api.call_action(
                "upload_private_file",
                user_id=int(event.get_sender_id()),
                file=payload,
                name=display_name,
            )
        return True, ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][网易云] 上传文件失败：{exc}")
        return False, str(exc)


async def _probe_size(url: str, timeout: float = 10.0) -> int:
    """
    HEAD 拿 Content-Length。

    第三方临时源经常不带 `size`，于是「体积上限」对它们形同不存在（白下几十 MB）。
    拿不到体积时返回 0，调用方按「未知不拦」处理 —— 宁可放行，也别把能播的歌挡掉。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.head(
                url,
                headers={"Referer": NETEASE_REFERER, "User-Agent": COMMON_USER_AGENT},
            )
            return _as_int(resp.headers.get("content-length"))
    except Exception:  # noqa: BLE001
        return 0


def _file_result(event: AstrMessageEvent, path: str, error: str = "") -> Any:
    """文件没发出去时的收尾：给一句可操作的提示，别让用户以为插件坏了。"""
    release_file(path, 10.0)
    detail = f"（{error}）" if error else ""
    return event.plain_result(
        f"音频文件没发出去{detail}。\n"
        "可以在面板把「发送方式」改成「语音」，或把「解析最高音质」调低试试"
    )


async def process_netease_url(
    event: AstrMessageEvent,
    *,
    api_server: str = "",
    cookie: str = "",
    quality: str = DEFAULT_QUALITY,
    audio_max_mb: int = 30,
    send_mode: str = "file",
    temp_fallback: bool = True,
    quality_fallback: bool = True,
    identify_prefix: str = "识别：",
) -> AsyncGenerator[Any, None]:
    """网易云分享链接 / 音乐卡片 → 音频"""
    text = get_message_text(event)
    target = parse_netease_target(text)
    if not target:
        # 是网易云链接但没解析出单曲（歌单 / 专辑 / 歌手页）：给一句提示，
        # 否则用户发歌单会「什么都没发生」
        if re.search(r"music\.163\.com|163cn\.tv", text or "", re.IGNORECASE):
            yield event.plain_result(
                "暂只支持网易云的单曲与声音（播客）链接，歌单 / 专辑 / 歌手页还没做"
            )
        return
    logger.info(f"[R插件][网易云] 收到目标：{target.kind} {target.target_id}")

    if target.kind == "short":
        final_url = await resolve_redirect(
            target.target_id, headers={"User-Agent": COMMON_USER_AGENT}
        )
        parsed = parse_netease_target(final_url) if final_url else None
        if not parsed or parsed.kind == "short":
            yield event.plain_result("网易云解析失败：短链里没有拿到歌曲 ID")
            return
        target = parsed

    client = NeteaseClient(api_server, cookie)
    if not client.configured:
        yield event.plain_result(
            "网易云解析未配置：请在插件配置里填写 NETEASE_API_SERVER（自建 NeteaseCloudMusicApi 地址）"
        )
        return

    if target.kind == "program":
        try:
            info = await fetch_program_song(client, target.target_id, cookie=cookie)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][网易云] 播客详情获取失败：{exc}")
            info = None
        if not info:
            yield event.plain_result("网易云播客解析失败：声音不存在或已下架")
            return
        song = Song(
            id=str(info["id"]),
            name=str(info.get("name") or info.get("program_name") or ""),
            artist="/".join(info.get("artists") or []),
            duration_ms=_as_int(info.get("duration_ms")),
            cover=str(info.get("cover") or ""),
            kind="podcast",
            program_id=target.target_id,
        )
    else:
        song = Song(id=target.target_id)

    # 补全元信息（标题 / 歌手 / 封面 / 时长）
    if song.kind != "podcast" or not song.name:
        try:
            detail = await fetch_song_detail(client, [song.id], cookie=cookie)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][网易云] 获取歌曲详情失败：{exc}")
            detail = {}
        info = detail.get(song.id)
        if info:
            song.name = song.name or info.get("name") or ""
            song.artist = song.artist or "/".join(info.get("artists") or [])
            song.cover = song.cover or info.get("cover") or ""
            song.duration_ms = song.duration_ms or _as_int(info.get("duration_ms"))
    song.cover = normalize_cover(song.cover)

    status = await cookie_status(client, cookie)
    source = await resolve_audio(
        client,
        song.id,
        cookie=cookie,
        quality=quality,
        max_mb=audio_max_mb,
        allow_fallback=quality_fallback,
        cookie_alive=bool(status.get("alive")),
    )
    # 主链没有音源、或者拿到的体积太大，都值得再试第三方：
    # 临时接口通常只给 320k mp3，比母带小一两个数量级
    if (source is None or source.oversize) and temp_fallback:
        temp_source = await temp_api_resolve(song.artist, song.name)
        if temp_source is not None and temp_source.url:
            if source is not None:
                temp_source.warnings.append("主链音源超过体积上限，改用第三方直链")
            source = temp_source

    tags: List[str] = []
    if song.kind != "podcast":
        tags = await fetch_wiki_tags(client, song.id)

    info_text = build_info_text(song, source, tags, identify_prefix)
    yield await cover_text_result(
        event, song.cover, info_text, referer=NETEASE_REFERER, prefix="nc_cover"
    )

    for warning in source.warnings if source else []:
        logger.info(f"[R插件][网易云] {warning}")

    # 卡片不需要本地文件：歌曲 id 与音频直链就够。放在下载之前，
    # 否则每发一次卡片都要白下整首（母带 200MB）。
    api = _onebot_api(event)
    if send_mode == "card" and api is not None:
        pushed, card_error = await _push_card(event, song, source)
        if pushed:
            return
        logger.warning(f"[R插件][网易云] 卡片发送失败（{card_error}），改用文件/语音")
        send_mode = "voice" if (source is not None and source.url) else "file"

    if source is None or not source.url:
        yield event.plain_result(
            "没有拿到可用音源。常见原因：这首歌无版权 / 需要会员、自建 API 没有登录、Cookie 失效。"
        )
        return

    # 体积上限对所有来源统一判定（第三方临时源原先不带 oversize 标记，等于没上限）
    max_bytes = max(0, _as_int(audio_max_mb)) * 1024 * 1024
    size_bytes = _as_int(source.size_bytes)
    if max_bytes and size_bytes <= 0:
        size_bytes = await _probe_size(source.url)
    if max_bytes and size_bytes > max_bytes:
        yield event.plain_result(
            f"音源 {format_size(size_bytes)} 超过上限 {audio_max_mb}MB，未下载。\n"
            f"可以调低「网易云解析最高音质」，或调高「网易云音频体积上限」。"
        )
        return

    ext = source.ext or "mp3"
    path = await download_file(
        source.url,
        build_temp_path("netease", f".{ext}"),
        headers={"Referer": NETEASE_REFERER},
        referer=NETEASE_REFERER,
        timeout=300,
        threads=0,
    )
    if not path:
        yield event.plain_result("音频下载失败，请稍后重试")
        return

    display_name = safe_filename(f"{song.artist}-{song.name}", default="netease") + f".{ext}"
    result = await _send_audio(
        event,
        path,
        ext,
        send_mode=send_mode,
        display_name=display_name,
        song=song,
        source=source,
    )
    if result is not None:
        yield result
