"""微信视频号分享链接解析

移植自原插件的 utils/weixin-channel.js（其本身参考 ltaoo/wx_channels_download）。

两步法：
  1. POST yuanbao.tencent.com/api/weixin/get_parse_result
     用「腾讯元宝 Web 端 Cookie」换 wx_export_id / playable_url / 封面 / 简介
  2. 从 playable_url 里取 token(generalToken) 与 eid(exportId)，
     POST channels.weixin.qq.com/finder-preview/api/feed/get_feed_info
     换视频直链、作者、互动数据

只依赖元宝 Cookie，不需要微信登录。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlparse

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger

from .common import (
    build_temp_path,
    download_file,
    local_image,
    release_video,
    video_components,
)

# ---------------------------------------------------------------- 常量

WXCHANNEL_YUANBAO_PARSE = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
WXCHANNEL_FEED_INFO = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"

WXCHANNEL_URL_REGEX = re.compile(
    r"https?://weixin\.qq\.com/sph/[A-Za-z0-9_-]+", re.IGNORECASE
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# 元宝接口请求头（与原插件 worker.js 保持一致，含设备/会话指纹）
PARSE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://yuanbao.tencent.com",
    "referer": "https://yuanbao.tencent.com/chat/naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
    "user-agent": _UA,
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "t-userid": "b9575f6b0a8c4a55a08096904a5ef20a",
    "x-agentid": "naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
    "x-commit-tag": "72282a0d",
    "x-device-id": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
    "x-hy106": "",
    "x-hy92": "e963067ffa31002b9646bd0c03000008b1951a",
    "x-hy93": "1921b001708100d7fa31002b9646bd0cc15a3e2e1f",
    "x-id": "b9575f6b0a8c4a55a08096904a5ef20a",
    "x-instance-id": "5",
    "x-language": "zh-CN",
    "x-os_version": "Mac OS(10.15.7)-Blink",
    "x-platform": "mac",
    "x-requested-with": "XMLHttpRequest",
    "x-source": "web",
    "x-web-third-source": "main",
    "x-webdriver": "0",
    "x-webversion": "2.69.0",
    "x-ybuitest": "0",
}

FEED_INFO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Origin": "https://channels.weixin.qq.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": _UA,
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


# ---------------------------------------------------------------- 工具函数

def generate_rid() -> str:
    """当前时间戳16进制 + '-' + 8位随机hex（模拟前端签名里的 _rid）"""
    stamp = format(int(time.time()), "x")
    chars = "0123456789abcdef"
    return f"{stamp}-{''.join(random.choice(chars) for _ in range(8))}"


def extract_share_url(text: str) -> str:
    """从消息文本里提取视频号分享链接"""
    if not text:
        return ""
    match = WXCHANNEL_URL_REGEX.search(text.replace("\\", ""))
    return match.group(0) if match else ""


def clean_video_url(video_url: str) -> str:
    """只保留 encfilekey 与 token，去掉其余临时参数"""
    try:
        parsed = urlparse(video_url)
        query = parse_qs(parsed.query)
        filekey = (query.get("encfilekey") or [""])[0]
        token = (query.get("token") or [""])[0]
        if filekey and token:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?encfilekey={filekey}&token={token}"
    except Exception:  # noqa: BLE001
        pass
    return video_url


# ---------------------------------------------------------------- 两步解析

async def parse_share_url(share_url: str, cookie: str) -> Dict[str, Any]:
    """Step 1：元宝接口解析分享链接"""
    payload = {"type": "video_channel_url", "url": share_url, "scene": 1}
    headers = {**PARSE_HEADERS, "cookie": cookie}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.post(WXCHANNEL_YUANBAO_PARSE, json=payload, headers=headers)
    try:
        result = resp.json()
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"元宝接口返回非 JSON（HTTP {resp.status_code}）")

    data = (result or {}).get("data") or {}
    if not data.get("wx_export_id"):
        raise RuntimeError("元宝接口未返回 wx_export_id，可能是 Cookie 失效或分享链接无效")
    logger.info(f"[R插件][视频号] step 1/2 完成，exportId={data.get('wx_export_id')}")
    return data


async def get_feed_info(export_id: str, general_token: str) -> Dict[str, Any]:
    """Step 2：视频号 feed 详情接口"""
    rid = generate_rid()
    payload = {"baseReq": {"generalToken": general_token}, "exportId": export_id}
    page_url = "https://channels.weixin.qq.com/finder-preview/pages/feed"
    api_url = f"{WXCHANNEL_FEED_INFO}?_rid={rid}&_pageUrl={quote(page_url, safe='')}"
    referer = (
        f"{page_url}?entry_card_type=48&comment_scene=39&appid=0"
        f"&token={quote(general_token, safe='')}"
        f"&entry_scene=0&eid={quote(export_id, safe='')}"
    )
    headers = {**FEED_INFO_HEADERS, "Referer": referer}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
    try:
        result = resp.json()
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"视频号接口返回非 JSON（HTTP {resp.status_code}）")
    logger.info(f"[R插件][视频号] step 2/2 完成，errCode={(result or {}).get('errCode')}")
    return result or {}


async def fetch_video_profile(share_url: str, cookie: str) -> Dict[str, Any]:
    """组合两步解析，返回标准化结果"""
    if not cookie:
        raise RuntimeError("未配置腾讯元宝 Cookie，请在面板填 WXCHANNEL_COOKIE")

    parse_data = await parse_share_url(share_url, cookie)

    # playable_url 里的 token / eid 是元宝下发的一次性预览凭证，缺一不可
    general_token = ""
    export_id = ""
    playable = parse_data.get("playable_url") or ""
    if playable:
        query = parse_qs(urlparse(playable).query)
        general_token = (query.get("token") or [""])[0]
        export_id = (query.get("eid") or [""])[0]
    if not general_token or not export_id:
        raise RuntimeError("元宝返回的 playable_url 缺少 token 或 eid，无法解析视频号详情")

    feed = await get_feed_info(export_id, general_token)

    err_code = feed.get("errCode")
    if err_code not in (None, 0):
        raise RuntimeError(f"视频号接口返回错误: {feed.get('errMsg') or '未知错误'}（errCode={err_code}）")

    data = feed.get("data") or {}
    if not data.get("feedInfo") and not data.get("authorInfo"):
        raise RuntimeError("视频号接口返回数据为空，可能是分享链接已失效或 token 过期")

    feed_info = data.get("feedInfo") or {}
    author_info = data.get("authorInfo") or {}
    scene_info = data.get("sceneInfo") or {}

    raw_video = feed_info.get("videoUrl") or ""
    h264_video = (feed_info.get("h264VideoInfo") or {}).get("videoUrl") or ""

    return {
        "video": raw_video or h264_video,
        "originVideo": clean_video_url(raw_video),
        "cover": feed_info.get("coverUrl") or parse_data.get("cover_url") or "",
        "desc": feed_info.get("description") or parse_data.get("desc") or "",
        "author": author_info.get("nickname") or parse_data.get("author") or "",
        "authorIcon": author_info.get("headImgUrl") or parse_data.get("author_icon") or "",
        "stats": {
            # 注意：接口的 likeCountFmt 实际是「点赞(红心)」，favCountFmt 是「喜欢」
            "like": feed_info.get("likeCountFmt") or "",
            "fav": feed_info.get("favCountFmt") or "",
            "forward": feed_info.get("forwardCountFmt") or "",
            "comment": feed_info.get("commentCountFmt") or "",
        },
        "createTime": feed_info.get("createtime") or 0,
        "expiredTime": scene_info.get("expiredTime") or 0,
        "mediaType": feed_info.get("mediaType") or 0,
    }


# ---------------------------------------------------------------- 消息入口

async def process_wxchannel_url(
    event,
    *,
    cookie: str = "",
    identify_prefix: str = "识别：",
    display_cover: bool = True,
    display_stats: bool = True,
    desc_len_limit: int = 200,
    video_size_limit_mb: int = 0,
):
    text = (getattr(event, "message_str", "") or "").strip()
    share_url = extract_share_url(text)
    if not share_url:
        return

    if not cookie:
        yield event.plain_result(
            f"{identify_prefix}视频号\n⚠️ 未配置腾讯元宝 Cookie，请在面板填 WXCHANNEL_COOKIE\n"
            "获取方法：浏览器登录 https://yuanbao.tencent.com 后 F12 → Network → "
            "任意请求 → Request Headers → 复制 Cookie"
        )
        return

    logger.info(f"[R插件][视频号] 开始解析：{share_url}")
    try:
        result = await fetch_video_profile(share_url, cookie)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][视频号] 解析失败: {exc}")
        message = str(exc)
        if "Cookie" in message or "wx_export_id" in message:
            hint = "：腾讯元宝 Cookie 可能已失效，请在面板更新 WXCHANNEL_COOKIE"
        else:
            hint = f"：{message}"
        yield event.plain_result(f"视频号解析失败{hint}")
        return

    lines = [f"{identify_prefix}视频号，{result.get('author') or '未知作者'}"]
    desc = (result.get("desc") or "").strip()
    if desc:
        if desc_len_limit and desc_len_limit > 0 and len(desc) > desc_len_limit:
            desc = desc[:desc_len_limit] + "..."
        lines.append(f"📝 简介：{desc}")
    if display_stats:
        stats = result.get("stats") or {}
        parts = []
        if stats.get("like"):
            parts.append(f"❤️{stats['like']}")
        if stats.get("fav"):
            parts.append(f"👍{stats['fav']}")
        if stats.get("forward"):
            parts.append(f"🔄{stats['forward']}")
        if stats.get("comment"):
            parts.append(f"💬{stats['comment']}")
        if parts:
            lines.append("📊 " + " ".join(parts))

    prefix = []
    if display_cover and result.get("cover"):
        cover = await local_image(
            result["cover"], referer="https://channels.weixin.qq.com/", prefix="wxch_cover"
        )
        if cover is not None:
            prefix.append(cover)

    yield event.chain_result(prefix + [Comp.Plain("\n".join(lines))])

    video_url = result.get("video") or ""
    if not video_url:
        logger.warning(f"[R插件][视频号] 没拿到视频直链，mediaType={result.get('mediaType')}")
        yield event.plain_result("未获取到视频地址，可能是图文动态或不支持的内容类型")
        return

    # 视频号 CDN 不支持 HEAD/分段，强制单连接直接 GET
    path = await download_file(
        video_url,
        build_temp_path("wxchannel", ".mp4"),
        headers={"User-Agent": _UA, "Referer": "https://channels.weixin.qq.com/"},
        timeout=300,
        threads=1,
    )
    if not path:
        yield event.plain_result("视频号视频下载失败，可能视频链接已过期，请重新分享链接")
        return

    components = video_components(path, "wxchannel.mp4", video_size_limit_mb)
    if components:
        yield event.chain_result(components)
    else:
        yield event.plain_result(
            f"视频已下载但无法发送：可能超过大小上限 {video_size_limit_mb}MB"
        )
    release_video(path)
