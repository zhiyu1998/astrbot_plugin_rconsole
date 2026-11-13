import re
import os
import json
import httpx
import asyncio
import aiohttp
import time
import random
from typing import Any, Dict, List, AsyncGenerator, Optional
from urllib.parse import urlparse, parse_qs

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .common import (
    cache_dir,
    create_forward_message,
    delete_boring_characters,
    local_image,
    single_result,
    truncate_str,
    image_send_results,
    release_file,
    release_video,
    remove_files,
    send_forward_message,
    video_components,
)

# Constants
XHS_REQ_LINK = "https://www.xiaohongshu.com/explore/"
XHS_DESC_LEN_LIMIT = 78
COMMON_HEADER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# 缓存目录跟随全局暂存目录（common.set_temp_dir），不再写死
def _cache_dir() -> str:
    return cache_dir("xhs_cache")

async def download_img(url: str, path: str, session: Optional[aiohttp.ClientSession] = None) -> str:
    """
    下载图片到指定路径
    
    :param url: 图片链接
    :param path: 保存路径
    :param session: 可选的 aiohttp 会话
    :return: 保存的文件路径
    """
    if session:
        async with session.get(url) as response:
            with open(path, 'wb') as fd:
                async for chunk in response.content.iter_chunked(1024):
                    fd.write(chunk)
    else:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                with open(path, 'wb') as fd:
                    async for chunk in response.content.iter_chunked(1024):
                        fd.write(chunk)
    return path

async def download_video(url: str) -> str:
    """
    下载视频到缓存路径
    
    :param url: 视频链接
    :return: 保存的文件路径
    """
    # 确保缓存目录存在
    os.makedirs(_cache_dir(), exist_ok=True)
    
    # 生成唯一文件名
    filename = f"xhs_video_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
    path = os.path.join(_cache_dir(), filename)
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            with open(path, 'wb') as fd:
                async for chunk in response.content.iter_chunked(1024):
                    fd.write(chunk)
    return path

def save_to_cache(note_id: str, data: Dict[str, Any]) -> None:
    """
    将小红书笔记数据保存到缓存
    
    :param note_id: 笔记ID
    :param data: 笔记数据
    """
    cache_path = os.path.join(_cache_dir(), f"{note_id}.json")
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        
        # 添加缓存时间戳
        cache_data = {
            "timestamp": int(time.time()),
            "data": data
        }
        
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"小红书数据已缓存: {note_id}")
    except Exception as e:
        logger.error(f"保存小红书缓存失败: {e}")

def get_from_cache(note_id: str, max_age: int = 86400) -> Optional[Dict[str, Any]]:
    """
    从缓存获取小红书笔记数据
    
    :param note_id: 笔记ID
    :param max_age: 最大缓存时间（秒），默认1天
    :return: 笔记数据或None
    """
    cache_path = os.path.join(_cache_dir(), f"{note_id}.json")
    
    if not os.path.exists(cache_path):
        return None
    
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        # 检查缓存是否过期
        timestamp = cache_data.get("timestamp", 0)
        current_time = int(time.time())
        
        if current_time - timestamp > max_age:
            logger.info(f"小红书缓存已过期: {note_id}")
            return None
        
        return cache_data.get("data")
    except Exception as e:
        logger.error(f"读取小红书缓存失败: {e}")
        return None

def _pick_str(source, *keys) -> str:
    for key in keys:
        value = (source or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_image_url(img) -> str:
    """新版页面图片地址在 urlDefault / infoList 里，老版在 url"""
    if not isinstance(img, dict):
        return ""
    for key in ("urlDefault", "urlPre", "url"):
        url = img.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    for info in img.get("infoList") or []:
        if isinstance(info, dict):
            url = info.get("url")
            if isinstance(url, str) and url.startswith("http"):
                return url
    return ""


def _pick_video_url(note) -> str:
    video = note.get("video") or {}
    if not isinstance(video, dict):
        return ""
    direct = video.get("url")
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    stream = ((video.get("media") or {}).get("stream")) or {}
    for codec in ("h264", "h265", "av1", "EF5", "EF4"):
        for item in stream.get(codec) or []:
            if not isinstance(item, dict):
                continue
            for key in ("masterUrl", "backupUrls"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
                if isinstance(value, list):
                    for u in value:
                        if isinstance(u, str) and u.startswith("http"):
                            return u
    return ""


def extract_note_info(note_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    从笔记数据里提取关键信息。

    新版网页把字段放在 note 顶层（title/desc/user/interactInfo/imageList），
    老版放在 shareInfo 里，这里两个都兼容。
    """
    share_info = note_data.get("shareInfo") or {}
    if not isinstance(share_info, dict):
        share_info = {}

    note_id = _pick_str(note_data, "noteId") or _pick_str(share_info, "noteId")
    note_type = _pick_str(note_data, "type") or _pick_str(share_info, "type") or "normal"
    title = _pick_str(note_data, "title") or _pick_str(share_info, "title")
    desc = _pick_str(note_data, "desc") or _pick_str(share_info, "desc")
    if not title and desc:
        # 小红书很多笔记没有标题，用正文首行顶上，免得全显示「无标题」
        title = desc.split("\n", 1)[0][:40]
    location = _pick_str(note_data, "ipLocation") or _pick_str(share_info, "location")
    time_stamp = note_data.get("time") or share_info.get("time") or 0

    user = note_data.get("user") or share_info.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    nickname = _pick_str(user, "nickname", "nickName", "nick_name") or "未知作者"
    user_id = _pick_str(user, "userId", "user_id", "id")
    avatar = _pick_str(user, "avatar", "image")

    interact = note_data.get("interactInfo") or {}
    if not isinstance(interact, dict):
        interact = {}

    def _num(*keys) -> int:
        for key in keys:
            for source in (interact, note_data):
                value = source.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.isdigit():
                    return int(value)
        return 0

    image_list = []
    for img in note_data.get("imageList") or []:
        url = _pick_image_url(img)
        if url:
            image_list.append({
                "url": url,
                "width": (img or {}).get("width", 0),
                "height": (img or {}).get("height", 0),
            })

    video_info = {"url": "", "cover": ""}
    if note_type == "video":
        video_info["url"] = _pick_video_url(note_data)
        video_node = note_data.get("video") or {}
        video_info["cover"] = _pick_image_url(video_node.get("image") or {}) or _pick_str(
            (video_node.get("consumer") or {}), "coverUrl"
        )

    return {
        "note_id": note_id,
        "type": note_type,
        "title": title,
        "desc": desc,
        "location": location,
        "time": time_stamp,
        "user": {"user_id": user_id, "nickname": nickname, "avatar": avatar},
        "stats": {
            "liked": bool(interact.get("liked")),
            "like_count": _num("likedCount", "likeCount"),
            "collected": bool(interact.get("collected")),
            "collect_count": _num("collectedCount", "collectCount"),
            "comment_count": _num("commentCount"),
            "share_count": _num("shareCount"),
        },
        "images": image_list,
        "video": video_info,
    }


def normalize_xhs_state_text(text: str) -> str:
    """
    小红书把 JS 对象字面量直接塞进了 window.__INITIAL_STATE__，
    严格 JSON 解析会炸在 `new Map([])` 这种地方（Expecting value: line 1 column xxxxx）。

    这里把常见的 JS-only 写法归一成 JSON：
    * undefined -> null
    * new Map(...) / new WeakMap(...) -> {}
    * new Set(...) / new WeakSet(...) -> []
    * new Date(...) -> null
    """
    text = text.replace("undefined", "null")
    text = re.sub(r"new\s+(?:Weak)?Map\s*\([^()]*\)", "{}", text)
    text = re.sub(r"new\s+(?:Weak)?Set\s*\([^()]*\)", "[]", text)
    text = re.sub(r"new\s+Date\s*\([^()]*\)", "null", text)
    return text


def parse_xhs_initial_state(html_text: str) -> Optional[Dict[str, Any]]:
    """从页面里抠出 window.__INITIAL_STATE__ 并解析，失败返回 None"""
    marker = "window.__INITIAL_STATE__="
    index = html_text.find(marker)
    if index < 0:
        return None
    brace = html_text.find("{", index)
    if brace < 0:
        return None

    body = html_text[brace:].split("</script>", 1)[0]
    # new Map(new Map()) 这类嵌套要替换几轮
    for _ in range(4):
        patched = normalize_xhs_state_text(body)
        if patched == body:
            break
        body = patched

    try:
        state, _ = json.JSONDecoder().raw_decode(body)
        return state
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][小红书] __INITIAL_STATE__ 解析失败: {exc}")
        return None


def pick_note_from_state(state: Dict[str, Any], xhs_id: str) -> Optional[Dict[str, Any]]:
    """从 state 里取笔记，兼容 noteDetailMap 的键不是 note id 的情况"""
    note_map = ((state.get("note") or {}).get("noteDetailMap")) or {}
    if not isinstance(note_map, dict):
        return None
    entry = note_map.get(xhs_id)
    if entry is None and note_map:
        entry = next(iter(note_map.values()))
    if isinstance(entry, dict):
        note = entry.get("note")
        return note if isinstance(note, dict) else entry
    return None


async def process_xiaohongshu_url(
    event: AstrMessageEvent,
    xhs_ck: str,
    image_use_forward: bool = True,
    image_batch_threshold: int = 0,
    video_size_limit_mb: int = 30,
    identify_prefix: str = "识别：",
    desc_len_limit: int = 78,
) -> AsyncGenerator[Any, None]:
    """
    处理小红书链接
    
    :param event: 消息事件
    :param xhs_ck: 小红书cookie
    :return: 异步生成结果
    """
    if not xhs_ck:
        yield event.plain_result("无法获取到小红书Cookie，请在配置中设置XHS_CK")
        return
    
    # 提取URL - 匹配形如 https://www.xiaohongshu.com/explore/6841430e000000002300f126 的链接
    message_str = event.message_str.strip()
    msg_url_match = re.search(r"(https?:\/\/)?(?:www\.)?(xhslink\.com|xiaohongshu\.com)\/[A-Za-z\d._?%&+\-=\/#@]*", message_str)
    
    if not msg_url_match:
        return
    
    msg_url = msg_url_match.group(0)
    
    # 请求头
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'accept-language': 'zh-CN,zh;q=0.9',
        'cookie': xhs_ck,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    
    # 如果是短链接，获取完整链接
    if "xhslink" in msg_url:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(msg_url, headers=headers, follow_redirects=True)
                msg_url = str(response.url)
        except Exception as e:
            yield event.plain_result(f"解析小红书链接失败: {str(e)}")
            return
    
    # 提取小红书ID
    xhs_id_match = re.search(r'/explore/(\w+)', msg_url)
    if not xhs_id_match:
        xhs_id_match = re.search(r'/discovery/item/(\w+)', msg_url)
    if not xhs_id_match:
        xhs_id_match = re.search(r'source=note&noteId=(\w+)', msg_url)
    
    if not xhs_id_match:
        yield event.plain_result(f"无法从链接中提取小红书ID")
        return
    
    xhs_id = xhs_id_match.group(1)
    
    # 首先尝试从缓存获取数据
    cached_data = get_from_cache(xhs_id)
    note_data = None
    
    if cached_data:
        logger.info(f"使用缓存的小红书数据: {xhs_id}")
        note_data = cached_data
    else:
        # 解析URL参数
        parsed_url = urlparse(msg_url)
        params = parse_qs(parsed_url.query)
        
        # 提取参数
        xsec_source = params.get('xsec_source', ['pc_feed'])[0]
        xsec_token = params.get('xsec_token', [None])[0]
        
        # 请求小红书内容（必须跟随跳转：小红书经常 302 到规范化地址，
        # 不跟随的话拿到的只是一个空跳转页，__INITIAL_STATE__ 自然找不到）
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
                response = await client.get(
                    f'{XHS_REQ_LINK}{xhs_id}?xsec_source={xsec_source}&xsec_token={xsec_token}',
                    headers=headers
                )
                html = response.text
        except Exception as e:
            yield event.plain_result(f"请求小红书内容失败: {str(e)}")
            return
        
        # 解析页面里的 __INITIAL_STATE__（注意：它不是严格 JSON）
        state = parse_xhs_initial_state(html)
        if not state:
            yield event.plain_result("无法解析小红书内容，Cookie 可能已失效或页面改版了")
            return

        raw_note_data = pick_note_from_state(state, xhs_id)
        if not raw_note_data:
            yield event.plain_result("小红书页面里没有找到笔记数据（可能需要登录 Cookie，或该笔记仅 App 可见）")
            return

        try:
            # 提取并缓存数据
            note_data = extract_note_info(raw_note_data)
            save_to_cache(xhs_id, note_data)
        except Exception as e:
            yield event.plain_result(f"解析小红书内容失败: {str(e)}")
            return
    
    if not note_data:
        yield event.plain_result("无法获取小红书内容")
        return
        
    # 提取帖子信息
    content_type = note_data.get("type", "")
    note_title = note_data.get("title") or "无标题"
    note_desc = note_data.get("desc", "") or ""

    # 和 R 插件的 sendXhsDescPreview 保持一致的排版：
    #   识别：小红书, <标题>
    #   <描述前 78 字…>
    # 描述超长时，再补一条合并转发放全文，避免正文被截断后看不到
    header = f"{identify_prefix}小红书, {note_title}"
    # desc_len_limit <= 0 表示不折叠，直接发全文
    limit = XHS_DESC_LEN_LIMIT if desc_len_limit is None else int(desc_len_limit)
    if limit <= 0:
        preview_desc = note_desc
    else:
        preview_desc = truncate_str(note_desc, limit)
    preview_text = f"{header}\n{preview_desc}" if preview_desc else header

    # 视频笔记用首图当封面，顺带验证视觉上不那么空
    cover_comp = None
    if content_type == "video" and note_data.get("images"):
        cover_comp = await local_image(
            note_data["images"][0].get("url", ""),
            referer="https://www.xiaohongshu.com/",
            prefix="xhs_cover",
        )

    if cover_comp is not None:
        yield single_result(event, cover_comp, Comp.Plain("\n" + preview_text))
    else:
        yield event.plain_result(preview_text)

    if limit > 0 and len(note_desc) > limit:
        uid = event.get_sender_id()
        uname = event.get_sender_name()
        node = create_forward_message(
            [
                {"uin": uid, "name": uname, "content": [Comp.Plain(header)]},
                {"uin": uid, "name": uname, "content": [Comp.Plain(note_desc)]},
            ],
            uname,
            uid,
        )
        yield event.chain_result([node])
    
    # 根据内容类型处理
    if content_type == "video":
        video_url = (note_data.get("video") or {}).get("url", "")
        if not video_url:
            yield event.plain_result("无法获取小红书视频直链")
            return
        video_path = await download_video(video_url)
        if not video_path:
            yield event.plain_result("小红书视频下载失败")
            return
        components = video_components(video_path, f"{xhs_id}.mp4", video_size_limit_mb)
        if components:
            yield event.chain_result(components)
        else:
            yield event.plain_result(f"视频超过 {video_size_limit_mb}MB 发送上限，已跳过发送")
        release_video(video_path)
        return

    # 图文笔记
    image_list = note_data.get("images", [])
    if not image_list:
        yield event.plain_result("未找到图片内容")
        return

    os.makedirs(_cache_dir(), exist_ok=True)
    image_paths = []
    async with aiohttp.ClientSession() as session:
        download_tasks = []
        for index, item in enumerate(image_list):
            image_url = item.get("url", "")
            if not image_url:
                continue
            path = os.path.join(_cache_dir(), f"xhs_{xhs_id}_{index}.jpg")
            download_tasks.append(asyncio.create_task(download_img(image_url, path, session=session)))
        if download_tasks:
            image_paths = [p for p in await asyncio.gather(*download_tasks) if p]

    if not image_paths:
        yield event.plain_result("图片下载失败")
        return

    components = [Comp.Image.fromFileSystem(p) for p in image_paths]
    for result in image_send_results(
        event, components, use_forward=image_use_forward, batch=image_batch_threshold
    ):
        yield result
    # 转发消息发送时才读文件，延后删除
    for path in image_paths:
        release_file(path)
