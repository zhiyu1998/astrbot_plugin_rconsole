import os
import random
import re
import time
import asyncio
from typing import List, Tuple, Optional, AsyncGenerator

import astrbot.api.message_components as Comp
import httpx
import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..constants.douyin import DOUYIN_HEADER, DOUYIN_VIDEO_API, DOUYIN_TOUTIAO_API, URL_TYPE_CODE_DICT
from .common import create_forward_message, send_forward_message

# 尝试导入execjs，但即使导入失败也不影响基本功能
try:
    import execjs
    import urllib.parse

    HAS_EXECJS = True
except ImportError:
    logger.warning("execjs not installed, X-Bogus signature generation will be skipped")
    HAS_EXECJS = False
    import urllib.parse

# 临时目录设置 - 使用标准化的路径格式
DATA_DIR = os.path.join(os.getcwd(), "data")
CACHE_DIR = os.path.join(DATA_DIR, "douyin_cache")

# 确保缓存目录存在
try:
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"确保抖音缓存目录存在: {CACHE_DIR}")
except Exception as e:
    logger.error(f"创建缓存目录失败: {e}")


def generate_random_str(randomlength=16):
    """
    根据传入长度产生随机字符串
    param :randomlength
    return:random_str
    """
    random_str = ''
    base_str = 'ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789='
    length = len(base_str) - 1
    for _ in range(randomlength):
        random_str += base_str[random.randint(0, length)]
    return random_str


def generate_x_bogus_url(url, headers):
    """
    生成抖音A-Bogus签名 (如果有execjs)
    :param url: 视频链接
    :param headers: 请求头
    :return: 包含X-Bogus签名的URL
    """
    if not HAS_EXECJS:
        # 如果没有execjs，返回原始URL
        return url

    try:
        # 获取查询部分
        query = urllib.parse.urlparse(url).query
        # A-Bogus JS文件路径
        abogus_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core',
                                        'a-bogus.js')

        # 检查文件是否存在
        if not os.path.exists(abogus_file_path):
            logger.warning(f"A-Bogus JS file not found at {abogus_file_path}")
            return url

        # 读取JS文件并执行
        with open(abogus_file_path, 'r', encoding='utf-8') as abogus_file:
            abogus_js = abogus_file.read()

        abogus = execjs.compile(abogus_js).call('generate_a_bogus', query, headers['User-Agent'])
        logger.debug(f'生成的A-Bogus签名为: {abogus}')
        return url + "&a_bogus=" + abogus
    except Exception as e:
        logger.error(f"生成A-Bogus签名失败: {e}")
        return url


async def get_douyin_slide_info(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], List[str]]:
    """
    获取抖音图集信息，尝试使用第三方API
    :param url: 抖音短链
    :return: (封面URL, 作者, 标题, 图片URL列表)
    """
    try:
        # 尝试使用第三方API解析
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.xingzhige.com/API/douyin/?url={url}")
            data = resp.json()

        data_content = data.get("data", { })
        item_id = data_content.get("jx", { }).get("item_id")
        item_type = data_content.get("jx", { }).get("type")

        if not item_id or not item_type:
            logger.debug("第三方API未返回item_id或type")
            return await _get_douyin_slide_info_fallback(url)

        # 备用API成功解析图集，直接处理
        if item_type == "图集":
            item = data_content.get("item", { })
            cover = item.get("cover", "")
            images = item.get("images", [])
            # 只有在有图片的情况下才发送
            if images:
                author = data_content.get("author", { }).get("name", "")
                title = data_content.get("item", { }).get("title", "")
                return cover, author, title, images

        # 如果不是图集或解析失败，使用备用方法
        return await _get_douyin_slide_info_fallback(url)
    except Exception as e:
        logger.error(f"获取抖音图集信息失败: {e}")
        return await _get_douyin_slide_info_fallback(url)


async def _get_douyin_slide_info_fallback(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], List[str]]:
    """
    获取抖音图集信息的备用方法
    :param url: 抖音短链
    :return: (封面URL, 作者, 标题, 图片URL列表)
    """
    try:
        # 获取重定向后的URL
        real_url = await get_redirect_url(url)
        if not real_url:
            return None, None, None, []

        # 提取图集ID
        slide_id_match = re.search(r"share\/slides\/(\d+)", real_url)
        if not slide_id_match:
            return None, None, None, []

        slide_id = slide_id_match.group(1)
        api_url = DOUYIN_VIDEO_API.format(slide_id)

        async with httpx.AsyncClient(headers=DOUYIN_HEADER) as client:
            resp = await client.get(api_url)
            data = resp.json()

        if not data.get('item_list'):
            return None, None, None, []

        item = data['item_list'][0]
        author = item.get('author', { }).get('nickname', '未知作者')
        title = item.get('desc', '无标题')
        cover = item.get('video', { }).get('cover', { }).get('url_list', [None])[0]

        images = []
        for img in item.get('images', []):
            if img.get('url_list'):
                images.append(img['url_list'][0])

        return cover, author, title, images
    except Exception as e:
        logger.error(f"获取抖音图集信息失败 (备用方法): {e}")
        return None, None, None, []


async def get_redirect_url(url: str) -> Optional[str]:
    """
    获取重定向后的URL，直接从headers获取location而不是follow redirects
    :param url: 原始URL
    :return: 重定向后的URL或None
    """
    try:
        response = httpx.get(url, headers=DOUYIN_HEADER, follow_redirects=False)
        if 'location' in response.headers:
            return response.headers['location']
        return str(response.url) if response.status_code == 200 else None
    except Exception as e:
        logger.error(f"获取重定向URL失败: {e}")
        return None


async def download_image(url: str, session: Optional[aiohttp.ClientSession] = None) -> Optional[str]:
    """
    下载图片到缓存目录
    
    :param url: 图片URL
    :param session: 可选的 aiohttp 会话
    :return: 本地文件路径或None
    """
    try:
        # 生成唯一文件名
        filename = f"img_{int(time.time())}_{random.randint(1000, 9999)}.jpg"
        filepath = os.path.join(CACHE_DIR, filename)
        
        if session:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(f"下载图片失败，状态码: {response.status}")
                    return None
                    
                with open(filepath, 'wb') as fd:
                    async for chunk in response.content.iter_chunked(1024):
                        fd.write(chunk)
        else:
            async with aiohttp.ClientSession() as new_session:
                async with new_session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"下载图片失败，状态码: {response.status}")
                        return None
                        
                    with open(filepath, 'wb') as fd:
                        async for chunk in response.content.iter_chunked(1024):
                            fd.write(chunk)
                            
        return filepath
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return None


async def process_douyin_url(event: AstrMessageEvent, douyin_ck: str = "") -> AsyncGenerator:
    """
    处理抖音链接
    :param event: AstrBot消息事件
    :param douyin_ck: 抖音Cookie，用于获取无水印内容
    :return: 生成器返回结果
    """
    # 获取消息文本
    msg: str = event.message_str.strip()
    logger.info(f"处理抖音链接: {msg}")

    # 匹配抖音短链接
    reg = r"(http:|https:)\/\/v.douyin.com\/[A-Za-z\d._?%&+\-=#]*"
    douyin_match = re.search(reg, msg, re.I)

    if not douyin_match:
        yield event.plain_result("无法识别抖音链接")
        return

    douyin_url = douyin_match.group(0)
    logger.debug(f"抖音短链接: {douyin_url}")

    try:
        # 获取重定向后的URL (使用NoneBot中的直接获取location的方法)
        dou_url_2 = await get_redirect_url(douyin_url)
        if not dou_url_2:
            yield event.plain_result("抖音短链接解析失败")
            return

        logger.debug(f"重定向后的URL: {dou_url_2}")

        # 处理图集 (如NoneBot示例中的方式)
        if "share/slides" in dou_url_2:
            cover, author, title, images = await get_douyin_slide_info(douyin_url)
            
            if author is not None and cover is not None:
                # 先发送封面和基本信息
                yield event.chain_result([
                    Comp.Image.fromURL(cover),
                    Comp.Plain(f"\n识别：抖音\n作者：{author}\n标题：{title}")
                ])
                
                # 使用转发消息发送图片集
                if images:
                    # 创建消息内容列表
                    content_list = []
                    
                    # 添加介绍消息
                    content_list.append([
                        Comp.Plain(f"抖音 | {title}\n作者: {author}\n\n图集共 {len(images)} 张图片")
                    ])
                    
                    # 下载图片
                    downloaded_images = []
                    try:
                        async with aiohttp.ClientSession() as session:
                            download_tasks = []
                            for image_url in images:
                                if image_url is not None:
                                    download_tasks.append(download_image(image_url, session))
                                    
                            if download_tasks:
                                downloaded_images = await asyncio.gather(*download_tasks)
                    except Exception as e:
                        logger.error(f"下载图片失败: {e}")
                    
                    # 添加每张图片到转发消息
                    for i, image_path in enumerate(downloaded_images):
                        if image_path is not None:
                            content_list.append([
                                Comp.Image.fromFileSystem(image_path),
                                Comp.Plain(f"\n第 {i+1}/{len(downloaded_images)} 张")
                            ])
                    
                    # 发送合并转发消息
                    if content_list:
                        try:
                            yield await send_forward_message(event, content_list)
                        except Exception as e:
                            logger.error(f"发送合并转发消息失败: {e}")
                            # 失败后单独发送每张图片
                            yield event.plain_result(f"合并转发失败，单独发送图片...")
                            for i, image_path in enumerate(downloaded_images):
                                if image_path is not None:
                                    try:
                                        yield event.chain_result([
                                            Comp.Image.fromFileSystem(image_path),
                                            Comp.Plain(f"\n第 {i+1}/{len(downloaded_images)} 张")
                                        ])
                                    except Exception as e2:
                                        logger.error(f"发送单张图片失败: {e2}")
                    
                    # 清理临时文件
                    for path in downloaded_images:
                        if path and os.path.exists(path):
                            try:
                                os.remove(path)
                            except Exception as e:
                                logger.error(f"删除临时文件失败: {e}")
            else:
                yield event.plain_result("抖音图集解析失败")
            return

        # 提取视频/笔记ID (如NoneBot示例)
        reg2 = r".*(video|note)\/(\d+)\/?.*?"
        id_match = re.search(reg2, dou_url_2, re.I)

        if not id_match:
            yield event.plain_result("无法提取抖音视频/笔记ID")
            return

        douyin_id = id_match.group(2)
        logger.debug(f"抖音ID: {douyin_id}")

        # 检查是否有Cookie
        if not douyin_ck:
            yield event.plain_result("未设置抖音Cookie，无法获取无水印内容")
            return

        # 准备请求头 (类似NoneBot示例)
        headers = {
            'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
            'referer': f'https://www.douyin.com/video/{douyin_id}',
            'cookie': douyin_ck,
            'User-Agent': DOUYIN_HEADER['User-Agent']
        }

        # 使用A-Bogus签名生成API URL (如NoneBot示例)
        api_url = DOUYIN_VIDEO_API.format(douyin_id)
        api_url = generate_x_bogus_url(api_url, headers)

        # 请求API
        async with httpx.AsyncClient() as client:
            resp = await client.get(api_url, headers=headers, timeout=10)
            data = resp.json()

            if not data or 'aweme_detail' not in data:
                yield event.plain_result("抖音解析失败，无法获取内容详情")
                return

            detail = data['aweme_detail']
            desc = detail.get('desc', '无标题')
            author = detail.get('author', { }).get('nickname', '未知作者')

            # 判断内容类型 (类似NoneBot示例)
            url_type_code = detail.get('aweme_type', 0)
            url_type = URL_TYPE_CODE_DICT.get(url_type_code, 'video')

            if url_type == 'video':
                # 处理视频 (类似NoneBot示例)
                video_info = detail.get('video', { })
                play_addr = video_info.get('play_addr', { })
                uri = play_addr.get('uri')

                if not uri:
                    yield event.plain_result("无法获取视频播放地址")
                    return

                # 获取无水印视频地址
                player_real_addr = DOUYIN_TOUTIAO_API.format(uri)
                cover_url = video_info.get('cover', { }).get('url_list', [None])[0]

                if cover_url is not None:
                    yield event.chain_result([
                        Comp.Image.fromURL(cover_url),
                        Comp.Plain(f"识别：抖音\n作者：{author}\n标题：{desc}")
                    ])

                # 直接通过URL发送视频，不需要下载
                yield event.chain_result([Comp.Video.fromURL(player_real_addr)])

            elif url_type == 'image':
                # 发送基本信息
                yield event.chain_result([
                    Comp.Plain(f"识别：抖音\n作者：{author}\n标题：{desc}")
                ])

                # 处理图片集
                images = detail.get('images', [])
                
                if not images:
                    yield event.plain_result("无法获取图片内容")
                    return
                
                # 创建消息内容列表
                content_list = []
                
                # 添加介绍消息
                content_list.append([
                    Comp.Plain(f"抖音 | {desc}\n作者: {author}\n\n图集共 {len(images)} 张图片")
                ])
                
                # 添加每张图片
                for i, img in enumerate(images):
                    url_list = img.get('url_list', [])
                    if url_list and url_list[1] is not None:
                        content_list.append([
                            Comp.Image.fromURL(url_list[1]),
                            Comp.Plain(f"\n第 {i+1}/{len(images)} 张")
                        ])
                
                # 发送合并转发消息
                if content_list:
                    yield await send_forward_message(event, content_list)

    except Exception as e:
        logger.error(f"处理抖音链接失败: {e}")
        yield event.plain_result(f"处理抖音链接失败: {e}")
