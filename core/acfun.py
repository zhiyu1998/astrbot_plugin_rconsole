"""
AcFun（猴山）解析（迁移自 rconsole-plugin/apps/tools.js 的 acfun 分支 + utils/acfun.js）

流程：作品页 ajaxpipe 拿 videoInfo -> ksPlayJson 拿 m3u8 -> 解析分片 -> 并发下载 ts -> ffmpeg concat 成 mp4
"""

import asyncio
import json
import os
import re
import shutil
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .common import (
    build_temp_path,
    concat_ts,
    delete_file,
    ensure_dir,
    fetch_text,
    get_message_text,
    release_video,
    video_components,
)

ACFUN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ACFUN_HEADERS = {"User-Agent": ACFUN_UA, "Referer": "https://www.acfun.cn/"}

ACFUN_URL_REGEX = r"(?:https?://)?(?:www\.|m\.)?acfun\.cn/[^\s]*|ac\d{6,10}"


def _escape_special_chars(raw: str) -> str:
    """对应 JS 的 escapeSpecialChars"""
    return raw.replace('\\\\"', '\\"').replace('\\"', '"')


def normalize_acfun_url(msg: str) -> Optional[str]:
    match = re.search(ACFUN_URL_REGEX, msg)
    if not match:
        return None
    link = match.group(0)
    if link.startswith("ac") and re.fullmatch(r"ac\d{6,10}", link):
        return f"https://www.acfun.cn/v/{link}"
    if not link.startswith("http"):
        link = "https://" + link
    if "m.acfun.cn" in link:
        ac_match = re.search(r"ac=([^&?]*)", link)
        if ac_match:
            return f"https://www.acfun.cn/v/ac{ac_match.group(1)}"
        return None
    return link


async def fetch_video_meta(url: str) -> Optional[Dict[str, Any]]:
    """拿到 videoInfo 与 m3u8 地址列表"""
    page = url + "?quickViewId=videoInfo_new&ajaxpipe=1"
    raw = await fetch_text(page, headers=ACFUN_HEADERS, timeout=20)
    if not raw:
        return None

    marker = "window.pageInfo = window.videoInfo ="
    if marker not in raw:
        logger.warning("[R插件][AcFun] 页面结构变化，找不到 videoInfo")
        return None
    body = raw.split(marker, 1)[1].split("</script>", 1)[0]
    try:
        video_info = json.loads(_escape_special_chars(body))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][AcFun] videoInfo 解析失败: {exc}")
        return None

    current = video_info.get("currentVideoInfo") or {}
    ks_play_raw = current.get("ksPlayJson") or "{}"
    try:
        ks_play = json.loads(ks_play_raw)
    except Exception:  # noqa: BLE001
        ks_play = {}

    m3u8_urls: List[str] = []
    for adaptation in ks_play.get("adaptationSet") or []:
        for rep in (adaptation or {}).get("representation") or []:
            if (rep or {}).get("url"):
                m3u8_urls.append(rep["url"])

    user = video_info.get("user") or {}
    return {
        "title": video_info.get("title") or "",
        "user": user.get("name") or "",
        "douga_id": video_info.get("dougaId") or "",
        "m3u8_urls": m3u8_urls,
    }


async def parse_m3u8(m3u8_url: str) -> List[str]:
    """返回分片的完整地址列表"""
    text = await fetch_text(m3u8_url, headers=ACFUN_HEADERS, timeout=20)
    if not text:
        return []
    prefix = m3u8_url.rsplit("/", 1)[0]
    segments: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("http"):
            segments.append(line)
        else:
            segments.append(f"{prefix}/{line}")
    return segments


async def download_segments(segments: List[str], work_dir: str, concurrency: int = 8) -> List[str]:
    """并发下载 ts 分片，返回按顺序排列的本地路径"""
    ensure_dir(work_dir)
    semaphore = asyncio.Semaphore(concurrency)
    paths: List[str] = [os.path.join(work_dir, "0.ts")] * len(segments)

    async def _one(index: int, url: str) -> bool:
        path = os.path.join(work_dir, f"{index}.ts")
        async with semaphore:
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                    async with client.stream("GET", url, headers=ACFUN_HEADERS) as resp:
                        if resp.status_code >= 400:
                            return False
                        with open(path, "wb") as fp:
                            async for chunk in resp.aiter_bytes(65536):
                                fp.write(chunk)
                paths[index] = path
                return os.path.getsize(path) > 0
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件][AcFun] 分片 {index} 下载失败: {exc}")
                return False

    results = await asyncio.gather(*[_one(i, u) for i, u in enumerate(segments)])
    if not all(results):
        logger.warning(f"[R插件][AcFun] 部分分片下载失败 {sum(1 for r in results if not r)}/{len(results)}")
    return paths


async def process_acfun_url(
    event: AstrMessageEvent,
    identify_prefix: str = "识别：",
    video_size_limit_mb: int = 0,
) -> AsyncGenerator[Any, None]:
    msg = get_message_text(event)
    if not msg:
        return

    url = normalize_acfun_url(msg)
    if not url:
        return
    logger.info(f"[R插件][AcFun] 规范化链接: {url}")

    meta = await fetch_video_meta(url)
    if not meta:
        yield event.plain_result("AcFun 解析失败：拿不到视频信息，可能是链接失效或页面改版")
        return

    title = meta.get("title") or "AcFun 视频"
    author = meta.get("user") or ""
    yield event.plain_result(f"{identify_prefix}猴山（AcFun）\n📝{title}\n👤{author}".rstrip())

    m3u8_urls: List[str] = meta.get("m3u8_urls") or []
    if not m3u8_urls:
        yield event.plain_result("AcFun 解析失败：没有拿到 m3u8 地址（可能需要大会员或者该视频受限）")
        return

    # 和原实现一致，取最后一个（通常是最高画质）
    segments = await parse_m3u8(m3u8_urls[-1])
    if not segments:
        yield event.plain_result("AcFun 解析失败：m3u8 分片列表为空")
        return

    work_dir = build_temp_path("acfun_ts", ".dir")
    ensure_dir(work_dir)
    try:
        local_parts = await download_segments(segments, work_dir)
        if not local_parts or not os.path.exists(local_parts[0]):
            yield event.plain_result("AcFun 视频分片下载失败")
            return

        list_file = os.path.join(work_dir, "file.txt")
        with open(list_file, "w", encoding="utf-8") as fp:
            for path in local_parts:
                fp.write(f"file '{os.path.abspath(path).replace(chr(92), '/')}'\n")

        output = build_temp_path("acfun", ".mp4")
        merged = await concat_ts(list_file, output)
        if not merged:
            yield event.plain_result("AcFun 视频合并失败，可能是服务器缺少 ffmpeg")
            return

        components = video_components(merged, f"{meta.get('title') or 'acfun'}.mp4", video_size_limit_mb)
        if components:
            yield event.chain_result(components)
        else:
            yield event.plain_result(
                f"AcFun 视频已合并但超过 {video_size_limit_mb}MB 发送上限\n原链接：{url}"
            )
        release_video(merged)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)