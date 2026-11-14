"""
抖音解析（迁移自 rconsole-plugin/apps/tools.js 的 douyin 分支）

解析顺序：
1. 短链先跟一次跳转拿到真实地址与 aweme_id
2. 分享页 SSR（www.iesdouyin.com/share/{video,note}/{id}/ 里的 window._ROUTER_DATA），免 Cookie
3. 配了 Cookie 时走 web 接口 aweme/detail（带 a_bogus 签名，execjs 不可用就跳过）
4. 都失败再落到第三方接口

只负责「解析 + 发送」，评论、直播录制、背景音乐、SSR Cookie 注册等重逻辑不进第一版。
"""

import json
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.douyin import (
    DOUYIN_HEADER,
    DOUYIN_ID_REGEXES,
    DOUYIN_ITEM_INFO,
    DOUYIN_SHARE_NOTE_PAGE,
    DOUYIN_SHARE_VIDEO_PAGE,
    DOUYIN_THIRD_PARTY,
    DOUYIN_TOUTIAO_API,
    DOUYIN_URL_REGEX,
    DOUYIN_VIDEO_API,
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


def _pick_video_url(video: Dict[str, Any]) -> Optional[str]:
    if not isinstance(video, dict):
        return None
    for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
        addr = video.get(key) or {}
        if not isinstance(addr, dict):
            continue
        for url in addr.get("url_list") or []:
            if url and _looks_like_media(url):
                return url.replace("playwm", "play")
        uri = addr.get("uri")
        if uri:
            return DOUYIN_TOUTIAO_API.format(uri)
    # 部分接口把码率列表放在 bit_rate 里
    for rate in video.get("bit_rate") or []:
        play_addr = (rate or {}).get("play_addr") or {}
        for url in play_addr.get("url_list") or []:
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


def _describe(item: Dict[str, Any]) -> Tuple[str, str]:
    author = ((item.get("author") or {}).get("nickname")) or "抖音用户"
    desc = item.get("desc") or "无简介"
    return author, truncate_str(desc, 200)


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
) -> AsyncGenerator[Any, None]:
    """抖音链接解析入口"""
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

    aweme_id = _extract_aweme_id(dou_url)
    if not aweme_id:
        yield event.plain_result("抖音链接识别失败，请确认是有效的作品链接")
        return

    if "live.douyin.com" in dou_url or "/live/" in dou_url:
        yield event.plain_result(f"{identify_prefix}抖音直播\n第一版暂不支持直播流解析，可以先发作品链接～")
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
    play_url = _pick_video_url(video)
    if not play_url:
        yield event.plain_result("抖音解析失败：没有拿到视频直链")
        return

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