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

from .common import delete_boring_characters, remove_files, send_forward_message

# Constants
XHS_REQ_LINK = "https://www.xiaohongshu.com/explore/"
COMMON_HEADER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# 数据目录和缓存目录
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CACHE_DIR = os.path.join(DATA_DIR, "xhs_cache")
TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp")

# 确保缓存目录存在
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    logger.info(f"确保小红书缓存目录存在: {CACHE_DIR}")
    logger.info(f"确保临时目录存在: {TEMP_DIR}")
except Exception as e:
    logger.error(f"创建目录失败: {e}")

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
    下载视频到临时路径
    
    :param url: 视频链接
    :return: 保存的文件路径
    """
    # 确保临时目录存在
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    # 生成唯一文件名
    filename = f"xhs_video_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
    path = os.path.join(TEMP_DIR, filename)
    
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
    cache_path = os.path.join(CACHE_DIR, f"{note_id}.json")
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
    cache_path = os.path.join(CACHE_DIR, f"{note_id}.json")
    
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

def extract_note_info(note_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    从笔记数据中提取关键信息
    
    :param note_data: 原始笔记数据
    :return: 提取后的关键信息
    """
    # 从原始数据中提取基本信息
    liked = note_data.get('liked', False)
    like_count = note_data.get('likeCount', 0)
    collected = note_data.get('collected', False)
    collect_count = note_data.get('collectCount', 0)
    comment_count = note_data.get('commentCount', 0)
    share_count = note_data.get('shareCount', 0)
    
    # 尝试从shareInfo中获取更多信息
    share_info = note_data.get('shareInfo', {})
    note_id = share_info.get('noteId', '')
    note_type = share_info.get('type', 'normal')
    title = share_info.get('title', '无标题')
    location = share_info.get('location', '')
    time_stamp = share_info.get('time', 0)
    
    # 提取用户信息
    user_info = share_info.get('user', {})
    user_id = user_info.get('userId', '')
    nickname = user_info.get('nickname', '未知作者')
    avatar = user_info.get('avatar', '')
    
    # 提取图片列表 (提取urlDefault作为高质量图片链接)
    image_list = []
    for img in note_data.get('imageList', []):
        if 'urlDefault' in img and img['urlDefault']:
            image_list.append({
                'url': img['urlDefault'],
                'width': img.get('width', 0),
                'height': img.get('height', 0)
            })
    
    # 提取视频信息
    video_info = {}
    if note_type == 'video' and 'video' in note_data:
        video = note_data.get('video', {})
        video_info = {
            'url': video.get('url', ''),
            'cover': video.get('cover', {}).get('url', '')
        }
    
    # 构建结果
    result = {
        'note_id': note_id,
        'type': note_type,
        'title': title,
        'desc': note_data.get('desc', ''),
        'location': location,
        'time': time_stamp,
        'user': {
            'user_id': user_id,
            'nickname': nickname,
            'avatar': avatar
        },
        'stats': {
            'liked': liked,
            'like_count': like_count, 
            'collected': collected,
            'collect_count': collect_count,
            'comment_count': comment_count,
            'share_count': share_count
        },
        'images': image_list,
        'video': video_info
    }
    
    return result

async def process_xiaohongshu_url(event: AstrMessageEvent, xhs_ck: str) -> AsyncGenerator[Any, None]:
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
        'cookie': xhs_ck,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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
        
        # 请求小红书内容
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f'{XHS_REQ_LINK}{xhs_id}?xsec_source={xsec_source}&xsec_token={xsec_token}', 
                    headers=headers
                )
                html = response.text
        except Exception as e:
            yield event.plain_result(f"请求小红书内容失败: {str(e)}")
            return
        
        # 解析JSON数据
        try:
            response_json_match = re.search('window.__INITIAL_STATE__=(.*?)</script>', html)
            if not response_json_match:
                yield event.plain_result("无法解析小红书内容，Cookie可能已失效")
                return
                
            response_json_str = response_json_match.group(1).replace("undefined", "null")
            response_json = json.loads(response_json_str)
            
            raw_note_data = response_json['note']['noteDetailMap'][xhs_id]['note']
            
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
    content_type = note_data.get('type', '')
    note_title = note_data.get('title', '无标题')
    note_desc = note_data.get('desc', '')
    author_name = note_data.get('user', {}).get('nickname', '未知作者')
    location = note_data.get('location', '')
    
    # 构建回复内容
    reply_text = f"小红书解析 | {note_title}"
    if note_desc:
        reply_text += f"\n描述: {note_desc}"
    
    reply_text += f"\n作者: {author_name}"
    
    if location:
        reply_text += f"\n位置: {location}"
    
    stats = note_data.get('stats', {})
    liked_icon = "❤️" if stats.get('liked', False) else "👍"
    collected_icon = "⭐" if stats.get('collected', False) else "⭐"
    
    reply_text += f"\n{liked_icon} {stats.get('like_count', 0)} | 💬 {stats.get('comment_count', 0)} | {collected_icon} {stats.get('collect_count', 0)}"
    
    if 'time' in note_data and note_data['time']:
        # 转换时间戳为可读时间
        time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(note_data['time']/1000))
        reply_text += f"\n发布时间: {time_str}"
    
    # 发送初始信息
    yield event.plain_result(reply_text)
    
    # 根据内容类型处理
    if content_type == 'normal':
        # 图片帖子
        image_list = note_data.get('images', [])
        if not image_list:
            yield event.plain_result("未找到图片内容")
            return
        
        # 创建临时目录
        os.makedirs(TEMP_DIR, exist_ok=True)
        
        # 下载图片
        image_paths = []
        async with aiohttp.ClientSession() as session:
            download_tasks = []
            for index, item in enumerate(image_list):
                image_url = item.get('url', '')
                if not image_url:
                    continue
                path = os.path.join(TEMP_DIR, f"xhs_{xhs_id}_{index}.jpg")
                download_tasks.append(asyncio.create_task(
                    download_img(image_url, path, session=session)))
            
            if download_tasks:
                image_paths = await asyncio.gather(*download_tasks)
        
        if not image_paths:
            yield event.plain_result("图片下载失败")
            return
        
        # 创建转发消息内容
        content_list = []
        
        # 添加标题和描述
        content_list.append([
            Comp.Plain(f"小红书 | {note_title}\n作者: {author_name}")
        ])
        
        if note_desc:
            content_list.append([
                Comp.Plain(f"描述: {note_desc}")
            ])
        
        # 添加位置信息
        if location:
            content_list.append([
                Comp.Plain(f"位置: {location}")
            ])
            
        # 添加统计信息
        content_list.append([
            Comp.Plain(f"{liked_icon} {stats.get('like_count', 0)} | 💬 {stats.get('comment_count', 0)} | {collected_icon} {stats.get('collect_count', 0)}")
        ])
        
        # 添加时间信息
        if 'time' in note_data and note_data['time']:
            time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(note_data['time']/1000))
            content_list.append([
                Comp.Plain(f"发布时间: {time_str}")
            ])
        
        # 添加图片
        for i, path in enumerate(image_paths):
            content_list.append([
                Comp.Plain(f"第 {i+1}/{len(image_paths)} 张"),
                Comp.Image.fromFileSystem(path)
            ])
        
        # 发送合并转发消息
        yield await send_forward_message(event, content_list)
        
        # 清理临时文件
        remove_files(image_paths)
        
    elif content_type == 'video':
        # 视频帖子
        video_info = note_data.get('video', {})
        video_url = video_info.get('url', '')
        
        if not video_url:
            yield event.plain_result("无法获取视频链接")
            return
        
        # 下载视频
        try:
            video_path = await download_video(video_url)
            
            # 发送视频
            yield event.chain_result([
                Comp.Video.fromFileSystem(video_path)
            ])
            
            # 清理临时文件
            remove_files([video_path])
        except Exception as e:
            yield event.plain_result(f"视频处理失败: {str(e)}")
    else:
        yield event.plain_result(f"不支持的内容类型: {content_type}") 