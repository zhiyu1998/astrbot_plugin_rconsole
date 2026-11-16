"""
抖音解析（迁移自 rconsole-plugin/apps/tools.js 的 douyin 分支）

解析顺序：
1. 短链先跟一次跳转拿到真实地址与 aweme_id
2. 分享页 SSR（www.iesdouyin.com/share/{video,note}/{id}/ 里的 window._ROUTER_DATA），免 Cookie
3. 配了 Cookie 时走 web 接口 aweme/detail（带 a_bogus 签名，execjs 不可用就跳过）
4. 都失败再落到第三方接口

只负责「解析 + 发送」，评论、直播录制、背景音乐、SSR Cookie 注册等重逻辑不进第一版。
"""

import datetime
import json
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.douyin import (
    DOUYIN_HEADER,
    DOUYIN_ID_REGEXES,
    DOUYIN_ITEM_INFO,
    DOUYIN_LIVE_ENTER_API,
    DOUYIN_LIVE_PAGE,
    DOUYIN_LIVE_QUALITY_ORDER,
    DOUYIN_LIVE_REFLOW_API,
    DOUYIN_LIVE_STATUS_LIVING,
    DOUYIN_RATIO_MAP,
    DOUYIN_SHARE_NOTE_PAGE,
    DOUYIN_SHARE_VIDEO_PAGE,
    DOUYIN_TARGET_HEIGHT,
    DOUYIN_THIRD_PARTY,
    DOUYIN_TOUTIAO_API,
    DOUYIN_URL_REGEX,
    DOUYIN_VIDEO_API,
)
from .common import (
    build_temp_path,
    convert_flv_to_mp4,
    cover_text_result,
    delete_file,
    download_file,
    download_images,
    download_stream_segment,
    fetch_json,
    fetch_text,
    get_message_text,
    image_send_results,
    limit_notice_precise,
    release_file,
    release_video,
    resolve_redirect,
    truncate_str,
    video_components,
)

# 尝试导入 execjs，失败则跳过 a_bogus 签名（SSR 兜底不需要签名）
try:
    import urllib.parse

    import execjs

    HAS_EXECJS = True
except ImportError:  # pragma: no cover - 取决于运行环境
    import urllib.parse

    HAS_EXECJS = False
    logger.warning("[R插件] 未安装 PyExecJS，抖音 Cookie 接口将跳过 a_bogus 签名")


def _abogus_file_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "a-bogus.js")


def generate_a_bogus(query: str, user_agent: str) -> str:
    """生成抖音 a_bogus 参数，失败返回空串"""
    if not HAS_EXECJS:
        return ""
    path = _abogus_file_path()
    if not os.path.exists(path):
        logger.warning(f"[R插件] 找不到 a-bogus.js: {path}")
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            js = fp.read()
        return execjs.compile(js).call("generate_a_bogus", query, user_agent)
    except Exception as exc:  # noqa: BLE001
        # 签名失败不影响主流程（分享页 SSR 不需要签名），降级成 warning 免得刷错误
        logger.warning(f"[R插件] 生成 a_bogus 失败（回退到无签名/SSR 链路）: {exc}")
        return ""


def _looks_like_media(url: Any) -> bool:
    """判断是不是真正的媒体直链（排除作品页地址）"""
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host:
        return False
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return False
    return True


def _ratio_for(resolution: str) -> str:
    """把配置里的档位名转成 aweme/v1/play 的 ratio 值（空串=直链默认）"""
    key = str(resolution or "1080P").strip().upper()
    if key not in DOUYIN_RATIO_MAP:
        key = "1080P"
    return DOUYIN_RATIO_MAP[key]


def _target_height(resolution: str) -> int:
    key = str(resolution or "1080P").strip().upper()
    return DOUYIN_TARGET_HEIGHT.get(key, 1080)


def _toutiao_url(uri: str, ratio: str) -> str:
    url = DOUYIN_TOUTIAO_API.format(uri)
    return url.replace("ratio=1080p", f"ratio={ratio}")


def _pick_from_bit_rates(video: Dict[str, Any], target_height: int) -> Optional[str]:
    """接口给了 bit_rate 多档时，按高度挑不高于目标的最高码率那档"""
    best: Optional[Tuple[int, int, str]] = None
    for rate in video.get("bit_rate") or []:
        if not isinstance(rate, dict):
            continue
        addr = rate.get("play_addr") or {}
        if not isinstance(addr, dict):
            continue
        urls = [u for u in addr.get("url_list") or [] if u and _looks_like_media(u)]
        if not urls:
            continue
        try:
            height = int(addr.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        if target_height and height and height > target_height:
            continue
        try:
            bit_rate = int(rate.get("bit_rate") or 0)
        except (TypeError, ValueError):
            bit_rate = 0
        score = (height, bit_rate)
        if best is None or score > best[0]:
            best = (score, bit_rate, urls[0].replace("playwm", "play"))
    return best[2] if best else None


def _pick_video_url(video: Dict[str, Any], resolution: str = "1080P") -> Optional[str]:
    """按分辨率档位挑播放直链

    优先级：
    1. bit_rate 多档列表（web 接口会给，带 height/bit_rate，最准）
    2. play_addr.uri → 走 aweme/v1/play 接口，用 ratio 参数指定 1080p/720p/540p
    3. play_addr.url_list 直链（服务端默认档，通常 720p）
    """
    if not isinstance(video, dict):
        return None
    ratio = _ratio_for(resolution)
    target_height = _target_height(resolution)

    from_rates = _pick_from_bit_rates(video, target_height)
    if from_rates:
        return from_rates

    for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
        addr = video.get(key) or {}
        if not isinstance(addr, dict):
            continue
        uri = addr.get("uri")
        # uri 正常是 video_id（v0d00fg10000...），但偶尔会给背景音乐直链，那种不能当 video_id 用
        if isinstance(uri, str) and uri and not uri.startswith("http"):
            return _toutiao_url(uri, ratio)
        for url in addr.get("url_list") or []:
            if url and _looks_like_media(url):
                return url.replace("playwm", "play")
    return None


def _pick_cover(item: Dict[str, Any], images: List[str]) -> Optional[str]:
    video = item.get("video") or {}
    for key in ("cover", "origin_cover", "dynamic_cover"):
        node = video.get(key) or {}
        if isinstance(node, dict):
            for url in node.get("url_list") or []:
                if url:
                    return url
    if images:
        return images[0]
    return None


def _collect_images(item: Dict[str, Any]) -> List[str]:
    images: List[str] = []
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        urls = img.get("url_list") or []
        if urls:
            images.append(urls[0])
    return images


def _extract_aweme_id(url: str) -> str:
    for pattern in DOUYIN_ID_REGEXES:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def _is_usable(item: Any) -> bool:
    """详情是否真的能用来发送（有播放直链或有图集）"""
    if not isinstance(item, dict):
        return False
    return bool(_pick_video_url(item.get("video") or {}) or _collect_images(item))


# 实测只有「移动端 UA + region=CN」的分享页才带 play_addr，
# 裸链接返回的是「请打开 App」的空壳（有 _ROUTER_DATA 但没有 item_list）
SSR_MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}

SSR_TEMPLATES = [
    "https://www.iesdouyin.com/share/video/{id}/?region=CN&mid=0",
    "https://www.iesdouyin.com/share/video/{id}/",
    "https://www.iesdouyin.com/share/note/{id}/?region=CN&mid=0",
    "https://www.iesdouyin.com/share/note/{id}/",
]


async def _fetch_detail_by_ssr(aweme_id: str, prefer_note: bool = False) -> Optional[Dict[str, Any]]:
    """分享页 SSR：页面内嵌 window._ROUTER_DATA，里面就有 aweme 详情"""
    templates = list(SSR_TEMPLATES)
    if prefer_note:
        templates = [SSR_TEMPLATES[2], SSR_TEMPLATES[3], SSR_TEMPLATES[0], SSR_TEMPLATES[1]]

    for tpl in templates:
        page = tpl.format(id=aweme_id)
        html_text = await fetch_text(page, headers=SSR_MOBILE_HEADERS, timeout=20)
        if not html_text or "_ROUTER_DATA" not in html_text:
            continue
        item = _parse_router_data(html_text)
        if _is_usable(item):
            logger.info(f"[R插件][抖音] SSR 兜底命中: {page}")
            return item
        logger.debug(f"[R插件][抖音] SSR 页面没有可用数据（空壳页）: {page}")
    return None


def _parse_router_data(html_text: str) -> Optional[Dict[str, Any]]:
    marker = "window._ROUTER_DATA"
    index = html_text.find(marker)
    if index < 0:
        return None
    brace = html_text.find("{", index)
    if brace < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(html_text[brace:])
    except Exception:  # noqa: BLE001
        return None

    loader = payload.get("loaderData") or {}
    if not isinstance(loader, dict):
        return None
    for value in loader.values():
        if not isinstance(value, dict):
            continue
        info = value.get("videoInfoRes") or {}
        items = info.get("item_list") or []
        for item in items:
            if isinstance(item, dict) and (item.get("video") or item.get("images")):
                return item
    return None


async def _fetch_detail_by_api(aweme_id: str, cookie: str) -> Optional[Dict[str, Any]]:
    if not cookie:
        return None
    headers = {
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "User-Agent": DOUYIN_HEADER["User-Agent"],
        "Origin": "https://open.douyin.com",
        "Referer": "https://www.douyin.com/",
        "cookie": cookie,
    }
    api = DOUYIN_VIDEO_API.format(aweme_id)
    query = urllib.parse.urlparse(api).query
    abogus = generate_a_bogus(query, headers["User-Agent"])
    if abogus:
        api = f"{api}&a_bogus={abogus}"
    data = await fetch_json(api, headers=headers, timeout=20)
    if isinstance(data, dict) and data.get("aweme_detail"):
        return data["aweme_detail"]
    return None


async def _fetch_detail_by_item_info(aweme_id: str) -> Optional[Dict[str, Any]]:
    data = await fetch_json(DOUYIN_ITEM_INFO.format(aweme_id), headers=DOUYIN_HEADER, timeout=15)
    if isinstance(data, dict):
        items = data.get("item_list") or []
        if items:
            return items[0]
    return None


async def _fetch_detail_by_third_party(url: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    第三方接口返回结构五花八门，这里统一成 aweme 形状。

    以前直接把 content["item"] 原样返回，遇到只有壳的对象就会「解析成功但没直链」，
    现在拼成 desc / author / video / images 再交出去，并用 _is_usable 兜底校验。
    """
    data = await fetch_json(
        DOUYIN_THIRD_PARTY.format(urllib.parse.quote(url, safe="")),
        headers={"User-Agent": DOUYIN_HEADER["User-Agent"]},
        timeout=20,
    )
    if not isinstance(data, dict):
        return None, []

    content = data.get("data") or {}
    if not isinstance(content, dict):
        return None, []

    item = content.get("item") or {}
    if not isinstance(item, dict):
        item = {}

    # xingzhige 这类接口：item.url / item.cover / item.title 才是真东西，
    # jx.url 是作品页地址，content.url 通常不存在
    video_url = item.get("url") or content.get("url") or ""
    if not _looks_like_media(video_url):
        video_url = ""
    cover = item.get("cover") or content.get("cover") or item.get("cover_gif") or ""
    title = item.get("title") or content.get("title") or content.get("desc") or ""

    author = content.get("author") or ""
    if isinstance(author, dict):
        author = author.get("name") or author.get("nickname") or ""

    # 图片可能是纯 url 列表，也可能是 aweme 的 [{url_list:[...]}]
    raw_images = content.get("images") or item.get("images") or []
    images: List[str] = []
    for img in raw_images:
        if isinstance(img, str) and img.startswith("http"):
            images.append(img)
        elif isinstance(img, dict):
            urls = img.get("url_list") or []
            if urls:
                images.append(urls[0])

    normalized: Dict[str, Any] = {
        "desc": title or "",
        "author": {"nickname": author or "抖音用户"},
        "images": [{"url_list": [u]} for u in images],
        "aweme_type": 2 if images else 4,
        "duration": item.get("duration") or 0,
        "video": item.get("video") if isinstance(item.get("video"), dict) else {},
    }
    if cover and _looks_like_media(cover):
        normalized["video"].setdefault("cover", {"url_list": [cover]})
    if video_url and not _pick_video_url(normalized["video"]):
        normalized["video"]["play_addr"] = {"url_list": [video_url]}

    logger.debug(f"[R插件][抖音] 第三方字段: video={video_url[:60]!r} cover={str(cover)[:50]!r} title={title[:30]!r}")
    if not _is_usable(normalized):
        return None, []
    return normalized, images


async def _resolve_detail(url: str, aweme_id: str, cookie: str, enable_ssr: bool = True) -> Optional[Dict[str, Any]]:
    """按可靠性依次尝试，任何一个来源拿到可用数据就返回"""
    candidates = []
    if enable_ssr:
        candidates.append(("SSR", lambda: _fetch_detail_by_ssr(aweme_id, prefer_note="note" in url)))
    candidates.extend([
        ("Cookie接口", lambda: _fetch_detail_by_api(aweme_id, cookie)),
        ("iteminfo", lambda: _fetch_detail_by_item_info(aweme_id)),
    ])
    for name, loader in candidates:
        try:
            detail = await loader()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][抖音] {name} 取详情异常: {exc}")
            continue
        if _is_usable(detail):
            logger.info(f"[R插件][抖音] 详情来自: {name}")
            return detail
        if detail:
            logger.warning(f"[R插件][抖音] {name} 返回的数据不可用（没有直链/图集），继续换来源")

    detail, _images = await _fetch_detail_by_third_party(url)
    if _is_usable(detail):
        logger.info("[R插件][抖音] 详情来自: 第三方接口")
        return detail
    return None


# ---------------------------------------------------------------- 直播

LIVE_PAGE_HEADERS = {
    "User-Agent": DOUYIN_HEADER["User-Agent"],
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}


def _merge_cookie(*cookie_texts: str) -> str:
    """把多段 Cookie 字符串合并成一段（后面的同名键覆盖前面的）"""
    jar: Dict[str, str] = {}
    for raw in cookie_texts:
        for part in str(raw or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            jar[key.strip()] = value.strip()
    return "; ".join(f"{k}={v}" for k, v in jar.items() if k)


def _extract_live_web_rid(url: str) -> str:
    match = re.search(r"live\.douyin\.com/(\d+)", url)
    return match.group(1) if match else ""


def _extract_live_room_id(url: str) -> str:
    match = re.search(r"reflow/(\d+)", url)
    return match.group(1) if match else ""


async def _live_cookie_with_ttwid(web_rid: str, cookie: str) -> str:
    """
    先访问一次直播间页面，把服务端下发的 ttwid 合并进 Cookie。

    实测：不带 Cookie 直接请求进入房间接口，HTTP 200 但 body 是空的（长度 0），
    带上页面给的 ttwid 才有 data.data[0] —— 这就是 R 插件里「必须填 Cookie」的真正原因，
    其实只需要一个游客 ttwid，配置里的登录 Cookie 只是加分项。
    """
    page_cookie = ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            await client.get(DOUYIN_LIVE_PAGE.format(web_rid), headers=LIVE_PAGE_HEADERS)
            page_cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][抖音直播] 取 ttwid 失败（继续用配置 Cookie）: {exc}")
    return _merge_cookie(cookie, page_cookie)


def _pick_live_room(payload: Any) -> Optional[Dict[str, Any]]:
    """从进入房间接口的响应里抠出 room 对象"""
    if not isinstance(payload, dict):
        return None
    rooms = ((payload.get("data") or {}).get("data")) or []
    if isinstance(rooms, dict):
        rooms = [rooms]
    for room in rooms:
        if isinstance(room, dict) and (room.get("stream_url") or room.get("title")):
            return room
    return None


async def _fetch_live_room_by_web(
    web_rid: str, cookie: str
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    web 端进入房间（对应 R 插件的 DY_LIVE_INFO，第一类直播链接）

    返回 (room, data)：data 是响应的 data 段，带着 room 对象里没有的字段
    —— enter_room_id（真实 room_id）和 partition_road_map（分区/游戏分类）。
    """
    headers = {
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "User-Agent": DOUYIN_HEADER["User-Agent"],
        "Referer": DOUYIN_LIVE_PAGE.format(web_rid),
        "cookie": await _live_cookie_with_ttwid(web_rid, cookie),
    }
    api = DOUYIN_LIVE_ENTER_API.format(web_rid, "")
    abogus = generate_a_bogus(urllib.parse.urlparse(api).query, headers["User-Agent"])
    if abogus:
        api = f"{api}&a_bogus={urllib.parse.quote(abogus, safe='')}"
    data = await fetch_json(api, headers=headers, timeout=20)
    room = _pick_live_room(data)
    if room:
        inner = data.get("data") if isinstance(data, dict) else None
        return room, (inner if isinstance(inner, dict) else {})
    logger.warning("[R插件][抖音直播] 进入房间接口返回空（通常是缺 ttwid 或被风控）")
    return None


async def _fetch_live_room_by_reflow(room_id: str, cookie: str) -> Optional[Dict[str, Any]]:
    """客户端 reflow 接口（对应 R 插件的 DY_LIVE_INFO_2，第二类直播链接）"""
    headers = {
        "User-Agent": DOUYIN_HEADER["User-Agent"],
        "Accept-Language": "zh-CN,zh;q=0.9",
        "cookie": cookie,
    }
    data = await fetch_json(DOUYIN_LIVE_REFLOW_API.format(room_id), headers=headers, timeout=20)
    if isinstance(data, dict):
        room = (data.get("data") or {}).get("room")
        if isinstance(room, dict) and (room.get("stream_url") or room.get("title")):
            return room
    return None


def _pick_live_stream(room: Dict[str, Any], resolution: str = "AUTO") -> Optional[str]:
    """
    挑直播流地址。

    优先级：FLV（R 插件走的就是 flv，录片段最省事）→ HLS 兜底。
    清晰度按 DOUYIN_LIVE_QUALITY_ORDER 依次尝试，缺档自动降级。
    """
    stream_url = room.get("stream_url") or {}
    if not isinstance(stream_url, dict):
        return None
    flv_map = stream_url.get("flv_pull_url") or {}
    hls_map = stream_url.get("hls_pull_url_map") or {}
    if not isinstance(flv_map, dict):
        flv_map = {}
    if not isinstance(hls_map, dict):
        hls_map = {}
    # 老结构是 hls_pull_url 单条直链
    hls_single = stream_url.get("hls_pull_url")

    order = DOUYIN_LIVE_QUALITY_ORDER.get(
        str(resolution or "AUTO").strip().upper(), DOUYIN_LIVE_QUALITY_ORDER["AUTO"]
    )
    for quality in order:
        url = flv_map.get(quality)
        if isinstance(url, str) and url.startswith("http"):
            logger.info(f"[R插件][抖音直播] 使用 FLV {quality} 档")
            return url
    for quality in order:
        url = hls_map.get(quality)
        if isinstance(url, str) and url.startswith("http"):
            logger.info(f"[R插件][抖音直播] 使用 HLS {quality} 档")
            return url
    if isinstance(hls_single, str) and hls_single.startswith("http"):
        logger.info("[R插件][抖音直播] 使用 HLS 单直链")
        return hls_single
    return None


def _human_count(value: Any) -> str:
    """数字转「1234 / 1.2万 / 1.2亿」，转不出来给空串（宁可少一行也不要 0）"""
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    if num >= 100000000:
        text = f"{num / 100000000:.1f}亿"
    elif num >= 10000:
        text = f"{num / 10000:.1f}万"
    else:
        return str(int(num))
    return text.replace(".0万", "万").replace(".0亿", "亿")


def _live_online_text(room: Dict[str, Any]) -> str:
    """
    在线人数。

    web 端有两套口径：room.user_count_str（"4w+"，接口算好的口语值）和
    room.room_view_stats（display_short / display_value 精确值）。
    优先用 room_view_stats 的精确值，拿不到才回退接口的原话。
    """
    stats = room.get("room_view_stats")
    if isinstance(stats, dict) and not stats.get("is_hidden"):
        for key in ("display_short_anchor", "display_short"):
            value = str(stats.get(key) or "").strip()
            if value and value != "0":
                return value
        precise = _human_count(stats.get("display_value"))
        if precise:
            return precise
    return str(room.get("user_count_str") or "").strip() or "未知"


def _live_anchor_tag(room: Dict[str, Any], detail: Dict[str, Any]) -> str:
    """主播名 + 抖音号 + 认证，抖音号/认证只有 reflow 接口才有"""
    owner = room.get("owner") if isinstance(room.get("owner"), dict) else {}
    deep = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
    nickname = str(owner.get("nickname") or deep.get("nickname") or "").strip()
    extra: List[str] = []
    display_id = str(deep.get("display_id") or "").strip()
    if display_id:
        extra.append(f"@{display_id}")
    if deep.get("verified"):
        extra.append("已认证")
    if nickname and extra:
        return f"{nickname}（{'，'.join(extra)}）"
    return nickname


def _live_partition_text(room: Dict[str, Any], enter_data: Dict[str, Any]) -> str:
    """分区 + 游戏名：主分类在响应的 partition_road_map，游戏名在 room.game_data"""
    parts: List[str] = []
    road = enter_data.get("partition_road_map") if isinstance(enter_data, dict) else None
    if isinstance(road, dict):
        main = str((road.get("partition") or {}).get("title") or "").strip()
        if main:
            parts.append(main)
    game = room.get("game_data") if isinstance(room.get("game_data"), dict) else {}
    tag = str((game.get("game_tag_info") or {}).get("game_tag_name") or "").strip()
    if tag:
        parts.append(tag)
    return " · ".join(dict.fromkeys(p for p in parts if p))


def _live_started_text(detail: Dict[str, Any]) -> str:
    """开播时间 + 已播时长。create_time 只有 reflow 接口给，web 端没有"""
    try:
        started = int(detail.get("create_time") or 0)
    except (TypeError, ValueError):
        started = 0
    if started <= 0:
        return ""
    try:
        clock = datetime.datetime.fromtimestamp(started).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return ""
    elapsed = int((datetime.datetime.now().timestamp() - started) / 60)
    if elapsed < 1:
        span = "刚开始"
    elif elapsed < 60:
        span = f"{elapsed} 分钟"
    elif elapsed % 60:
        span = f"{elapsed // 60} 小时 {elapsed % 60} 分"
    else:
        span = f"{elapsed // 60} 小时"
    return f"{clock}（已播 {span}）"


def _live_stats_text(room: Dict[str, Any], detail: Dict[str, Any]) -> str:
    """累计观看 + 本场涨粉：reflow 端是精确值（实测比 web 端的 "10万+" 更新），web 端只做兜底"""
    web_stats = room.get("stats") if isinstance(room.get("stats"), dict) else {}
    deep_stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
    watched = _human_count(deep_stats.get("total_user")) or str(
        web_stats.get("total_user_str") or ""
    ).strip()
    bits: List[str] = []
    if watched:
        bits.append(f"累计观看：{watched}")
    follows = _human_count(deep_stats.get("follow_count"))
    if follows:
        bits.append(f"本场涨粉：{follows}")
    return " · ".join(bits)


def _live_like_text(room: Dict[str, Any], detail: Dict[str, Any]) -> str:
    """点赞数：web 端 room.like_count（1266093），reflow 端同名字段"""
    for source in (room, detail):
        if not isinstance(source, dict):
            continue
        try:
            num = int(source.get("like_count") or 0)
        except (TypeError, ValueError):
            num = 0
        if num > 0:
            return _human_count(num)
    return ""


def _live_shop_text(room: Dict[str, Any]) -> str:
    """带货小黄车：has_commerce_goods 为真才算，ecom_data 只有电商配置不代表挂了商品"""
    return "主播挂着小黄车" if room.get("has_commerce_goods") else ""


def _live_cover(room: Dict[str, Any], detail: Optional[Dict[str, Any]] = None) -> str:
    """
    直播封面：web 端 cover.url_list → reflow 端 cover.url_list → 主播头像兜底。

    两个接口的封面签名链接都能直接用（Referer 要是 live.douyin.com），
    web 端缺封面（个别主播没设）时就退回头像，至少别让文案光秃秃的。
    """
    sources: List[Any] = [room.get("cover")]
    if isinstance(detail, dict):
        sources.append(detail.get("cover"))
    for cover in sources:
        if isinstance(cover, dict):
            urls = [u for u in cover.get("url_list") or [] if u]
            if urls:
                return urls[0]
        elif isinstance(cover, str) and cover.startswith("http"):
            return cover

    for source in (room, detail if isinstance(detail, dict) else {}):
        thumb = (source.get("owner") or {}).get("avatar_thumb") or {}
        if isinstance(thumb, dict):
            urls = [u for u in thumb.get("url_list") or [] if u]
            if urls:
                return urls[0]
    return ""


async def _process_douyin_live(
    event: AstrMessageEvent,
    dou_url: str,
    *,
    douyin_ck: str,
    identify_prefix: str,
    stream_duration: int,
    stream_compat: bool,
    video_size_limit_mb: int,
    video_resolution: str,
    display_detail: bool = True,
    live_cover: bool = True,
) -> AsyncGenerator[Any, None]:
    """抖音直播：房间信息 → 封面文字 → 录 N 秒流 → 转 mp4 → 发视频"""
    room: Optional[Dict[str, Any]] = None
    enter_data: Dict[str, Any] = {}
    detail: Dict[str, Any] = {}

    web_rid = _extract_live_web_rid(dou_url)
    room_id = _extract_live_room_id(dou_url)

    if web_rid:
        got = await _fetch_live_room_by_web(web_rid, douyin_ck)
        if got:
            room, enter_data = got
            # enter_room_id 就是真实 room_id，比从页面正则抠更靠谱
            room_id = room_id or str(enter_data.get("enter_room_id") or "")
    if not room and room_id:
        room = await _fetch_live_room_by_reflow(room_id, douyin_ck)
        detail = room or {}
    if not room and web_rid:
        # 万一 webcast 短链跳转后 still 是 reflow 结构
        room = await _fetch_live_room_by_reflow(web_rid, douyin_ck)
        detail = room or {}

    if not room:
        yield event.plain_result("抖音直播解析失败，可能需要配置 Cookie，或接口风控了，稍后再试试")
        return

    # 开播时间 / 抖音号 / 认证 / 本场涨粉只有客户端 reflow 接口给，多一次请求换更完整的信息
    if display_detail and room_id and not detail:
        detail = await _fetch_live_room_by_reflow(room_id, douyin_ck) or {}

    title = truncate_str(str(room.get("title") or "抖音直播"), 100)
    lines = [f"{identify_prefix}抖音直播，{title}"]
    anchor = _live_anchor_tag(room, detail)
    if anchor:
        lines.append(f"🎙️ 主播：{anchor}")
    partition = _live_partition_text(room, enter_data)
    if partition:
        lines.append(f"🎮 分区：{partition}")
    started = _live_started_text(detail)
    if started:
        lines.append(f"⏰ 开播：{started}")

    try:
        status = int(room.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status != DOUYIN_LIVE_STATUS_LIVING:
        tip = "已结束" if status == 4 else "暂未开播"
        yield await cover_text_result(
            event,
            _live_cover(room, detail) if live_cover else "",
            "\n".join([*lines, f"💤 该直播{tip}，等主播开播再来试试～"]),
            referer="https://live.douyin.com/",
            prefix="dy_live_cover",
        )
        return

    lines.append(f"🏄‍♂️ 在线人数：{_live_online_text(room)}人正在观看")
    stats_line = _live_stats_text(room, detail)
    if stats_line:
        lines.append(f"👀 {stats_line}")
    likes = _live_like_text(room, detail)
    if likes:
        lines.append(f"👍 点赞：{likes}")
    shop = _live_shop_text(room)
    if shop:
        lines.append(f"🛒 带货：{shop}")
    if web_rid:
        lines.append(f"🔗 直播间：{DOUYIN_LIVE_PAGE.format(web_rid)}")

    yield await cover_text_result(
        event,
        _live_cover(room, detail) if live_cover else "",
        "\n".join(lines),
        referer="https://live.douyin.com/",
        prefix="dy_live_cover",
    )

    stream_url = _pick_live_stream(room, video_resolution)
    if not stream_url:
        yield event.plain_result("抖音直播没有拿到直播流地址（可能刚下播），稍后再试试")
        return

    seconds = max(3, min(int(stream_duration or 10), 120))
    logger.info(f"[R插件][抖音直播] 开始录 {seconds} 秒直播流")
    path = await download_stream_segment(
        stream_url,
        build_temp_path("douyin_live", ".flv"),
        seconds=seconds,
        headers={"User-Agent": DOUYIN_HEADER["User-Agent"]},
        referer="https://live.douyin.com/",
    )
    if not path:
        yield event.plain_result("抖音直播录制失败，可能是直播流地址过期了，稍后再试试")
        return

    # 录到的是 flv：多数客户端（QQ / Telegram）不认，转成 mp4 再发
    if stream_compat:
        mp4_path = await convert_flv_to_mp4(path)
        if mp4_path:
            delete_file(path)
            path = mp4_path
        else:
            logger.warning("[R插件][抖音直播] flv 转 mp4 失败（没装 ffmpeg？），直接发 flv")

    components = video_components(path, "douyin_live.mp4", video_size_limit_mb)
    if components:
        yield event.chain_result(components)
        release_video(path)
    else:
        delete_file(path)
        yield event.plain_result(
            f"直播片段已录好但超过 {video_size_limit_mb}MB 发送上限，已跳过\n原链接：{dou_url}"
        )


def _describe(item: Dict[str, Any]) -> Tuple[str, str]:
    author = ((item.get("author") or {}).get("nickname")) or "抖音用户"
    desc = item.get("desc") or "无简介"
    return author, truncate_str(desc, 200)


def _is_live_url(url: str) -> bool:
    return (
        "live.douyin.com" in url
        or "/live/" in url
        or "webcast.amemv.com" in url
        or "reflow/" in url
    )


async def process_douyin_url(
    event: AstrMessageEvent,
    douyin_ck: str = "",
    video_duration_maximum: int = 480,
    identify_prefix: str = "识别：",
    display_cover: bool = True,
    image_batch_threshold: int = 50,
    video_size_limit_mb: int = 0,
    image_use_forward: bool = True,
    enable_ssr: bool = True,
    video_resolution: str = "1080P",
    stream_duration: int = 10,
    stream_compat: bool = True,
    live_detail: bool = True,
    live_cover: bool = True,
) -> AsyncGenerator[Any, None]:
    """抖音链接解析入口（作品/图集/直播）"""
    msg = get_message_text(event)
    if not msg:
        return

    url_match = re.search(DOUYIN_URL_REGEX, msg, re.I)
    if not url_match:
        return
    dou_url = url_match.group(0)
    if not dou_url.startswith("http"):
        dou_url = "https://" + dou_url
    logger.info(f"[R插件][抖音] 收到链接: {dou_url}")

    # 短链先跟一次跳转
    if "v.douyin.com" in dou_url or "webcast.amemv.com" in dou_url:
        dou_url = await resolve_redirect(dou_url, headers={"User-Agent": DOUYIN_HEADER["User-Agent"]})
        logger.info(f"[R插件][抖音] 跳转后: {dou_url}")

    # 直播：先走直播链路，别掉进作品解析
    if _is_live_url(dou_url):
        async for result in _process_douyin_live(
            event,
            dou_url,
            douyin_ck=douyin_ck,
            identify_prefix=identify_prefix,
            stream_duration=stream_duration,
            stream_compat=stream_compat,
            video_size_limit_mb=video_size_limit_mb,
            video_resolution=video_resolution,
            display_detail=live_detail,
            live_cover=live_cover,
        ):
            yield result
        return

    aweme_id = _extract_aweme_id(dou_url)
    if not aweme_id:
        yield event.plain_result("抖音链接识别失败，请确认是有效的作品链接")
        return

    try:
        item = await _resolve_detail(dou_url, aweme_id, douyin_ck, enable_ssr=enable_ssr)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][抖音] 解析异常: {exc}")
        item = None

    if not item:
        yield event.plain_result("抖音解析失败，可能需要配置 Cookie，或接口风控了，稍后再试试")
        return

    author, desc = _describe(item)
    images = _collect_images(item)
    cover = _pick_cover(item, images)

    if images:
        header_text = f"{identify_prefix}抖音图集，作者：{author}\n📝 简介：{desc}\n🖼️ 共 {len(images)} 张"
        yield await cover_text_result(
            event,
            cover if display_cover else "",
            header_text,
            referer="https://www.douyin.com/",
            prefix="dy_cover",
        )

        local_images = await download_images(
            images, prefix="douyin", referer="https://www.douyin.com/", limit=60
        )
        if not local_images:
            yield event.plain_result("图片下载失败，可能是链接过期了")
            return

        components: List[Any] = []
        for path in local_images:
            components.append(Comp.Image.fromFileSystem(path))
        for result in image_send_results(
            event, components, use_forward=image_use_forward, batch=image_batch_threshold
        ):
            yield result
        # 转发消息是在真正发送时才把节点里的图转 base64，所以延后删
        for path in local_images:
            release_file(path)
        return

    video = item.get("video") or {}
    play_url = _pick_video_url(video, video_resolution)
    if not play_url:
        yield event.plain_result("抖音解析失败：没有拿到视频直链")
        return
    logger.info(f"[R插件][抖音] 画质档位：{video_resolution}（ratio={_ratio_for(video_resolution) or '默认'}）")

    # 有的来源给毫秒，有的给秒，>1000 才当毫秒处理
    raw_duration = video.get("duration") or item.get("duration") or 0
    try:
        raw_duration = float(raw_duration)
    except (TypeError, ValueError):
        raw_duration = 0.0
    duration_s = int(raw_duration / 1000) if raw_duration > 1000 else int(raw_duration)
    # 排版对齐原插件：识别：抖音，作者 + 简介（时长只在超限提示里出现）
    header_text = f"{identify_prefix}抖音，{author}\n📝 简介：{desc}"

    if video_duration_maximum and duration_s > video_duration_maximum:
        limit_text = header_text + "\n" + limit_notice_precise(
            duration_s, video_duration_maximum
        )
        yield await cover_text_result(
            event,
            cover if display_cover else "",
            limit_text,
            referer="https://www.douyin.com/",
            prefix="dy_cover",
        )
        return

    yield await cover_text_result(
        event,
        cover if display_cover else "",
        header_text,
        referer="https://www.douyin.com/",
        prefix="dy_cover",
    )

    path = await download_file(
        play_url,
        build_temp_path("douyin", ".mp4"),
        headers={"User-Agent": DOUYIN_HEADER["User-Agent"]},
        referer="https://www.douyin.com/",
    )
    if not path:
        yield event.plain_result("抖音视频下载失败，可能是链接过期或被限流")
        return
    components = video_components(path, "douyin.mp4", video_size_limit_mb)
    if components:
        yield event.chain_result(components)
    else:
        yield event.plain_result(
            f"视频已下载但超过 {video_size_limit_mb}MB 发送上限，已跳过\n原链接：{dou_url}"
        )
    release_video(path)