import asyncio
import os
import platform
import re
import subprocess
from urllib.parse import urlparse, parse_qs

import aiofiles
import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from bilibili_api import video, Credential, live, article
from bilibili_api.opus import Opus
from bilibili_api.video import VideoDownloadURLDataDetecter

from .common import (
    delete_boring_characters,
    delete_file,
    local_image,
    release_video,
    remove_files,
    video_components,
)
from ..constants.bili23 import *

# B站 CDN 拉流很慢，默认 5 秒的 read timeout 必炸，这里给足时间
BILI_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=600.0, pool=30.0)


async def download_b_file(url, full_file_name, progress_callback=None, retries=3):
    """
        下载视频文件和音频文件（带重试，避免大文件 ReadTimeout 直接失败）

    :param url: 下载地址
    :param full_file_name: 落地文件名
    :param progress_callback: 进度回调，可为空
    :param retries: 重试次数
    :return: 落地文件名
    """
    progress_callback = progress_callback or (lambda _msg: None)
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
                timeout=BILI_DOWNLOAD_TIMEOUT,
            ) as client:
                async with client.stream("GET", url, headers=BILIBILI_HEADER) as resp:
                    if resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    total_len = int(resp.headers.get("content-length") or 0)
                    current_len = 0
                    async with aiofiles.open(full_file_name, "wb") as f:
                        async for chunk in resp.aiter_bytes(262144):
                            current_len += len(chunk)
                            await f.write(chunk)
                            if total_len:
                                progress_callback(f"下载进度：{round(current_len / total_len, 3)}")
                    # CDN 偶尔会提前断流，长度对不上就当失败重来
                    if total_len and current_len < total_len:
                        raise RuntimeError(f"下载不完整 {current_len}/{total_len}")
            return full_file_name
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(f"[R插件][B站] 媒体下载失败（第 {attempt}/{retries} 次）: {exc}")
            delete_file(full_file_name)
            if attempt < retries:
                await asyncio.sleep(2 * attempt)

    raise RuntimeError(f"B站媒体下载失败: {last_error}")


async def merge_file_to_mp4(v_full_file_name: str, a_full_file_name: str, output_file_name: str,
                            log_output: bool = False):
    """
    合并视频文件和音频文件
    :param v_full_file_name: 视频文件路径
    :param a_full_file_name: 音频文件路径
    :param output_file_name: 输出文件路径
    :param log_output: 是否显示 ffmpeg 输出日志，默认忽略
    :return:
    """
    logger.info(f'正在合并：{output_file_name}')

    # 构建 ffmpeg 命令
    command = f'ffmpeg -y -i "{v_full_file_name}" -i "{a_full_file_name}" -c copy "{output_file_name}"'
    stdout = None if log_output else subprocess.DEVNULL
    stderr = None if log_output else subprocess.DEVNULL

    if platform.system() == "Windows":
        # Windows 下使用 run_in_executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.call(command, shell=True, stdout=stdout, stderr=stderr)
        )
    else:
        # 其他平台使用 create_subprocess_shell
        process = await asyncio.create_subprocess_shell(
            command,
            shell=True,
            stdout=stdout,
            stderr=stderr
        )
        await process.communicate()


def extra_bili_info(video_info):
    """
        格式化视频信息
    """
    video_state = video_info['stat']
    video_like, video_coin, video_favorite, video_share, video_view, video_danmaku, video_reply = video_state['like'], \
        video_state['coin'], video_state['favorite'], video_state['share'], video_state['view'], video_state['danmaku'], \
        video_state['reply']

    video_data_map = {
        "点赞": video_like,
        "硬币": video_coin,
        "收藏": video_favorite,
        "分享": video_share,
        "总播放量": video_view,
        "弹幕数量": video_danmaku,
        "评论": video_reply
    }

    video_info_result = ""
    for key, value in video_data_map.items():
        if int(value) > 10000:
            formatted_value = f"{value / 10000:.1f}万"
        else:
            formatted_value = value
        video_info_result += f"{key}: {formatted_value} | "

    return video_info_result


def _cleanup_bili_cache(download_path: str, merged: str) -> None:
    """删掉合并产物与空目录，避免 data/bilibili_cache 越攒越大"""
    release_video(merged)
    try:
        if os.path.isdir(download_path) and not os.listdir(download_path):
            os.rmdir(download_path)
    except OSError:
        pass


async def process_bilibili_url(
    event: AstrMessageEvent,
    credential: Credential,
    video_duration_maximum: int,
    video_size_limit_mb: int = 0,
    display_cover: bool = True,
    display_info: bool = True,
    display_intro: bool = True,
    display_online: bool = True,
    intro_len_limit: int = 50,
):
    """
        核心代码：处理B站链接
    """
    # 1. URL 预处理
    # 当前收到的消息
    url: str = event.message_str.strip()
    url_reg = r"(http:|https:)\/\/(space|www|live|t).bilibili.com\/[A-Za-z\d._?%&+\-=\/#]*"
    b_short_rex = r"(https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z\d._?%&+\-=\/#]+)"
    # BV处理
    if re.match(r'^BV[1-9a-zA-Z]{10}$', url, re.IGNORECASE):
        url = 'https://www.bilibili.com/video/' + url
    # 处理短号、小程序问题
    if "b23.tv" in url or "bili2233.cn" in url:
        try:
            b_short_match = re.search(b_short_rex, url.replace("\\", ""))
            if not b_short_match:
                yield event.plain_result("错误：B站短链接解析失败。")
                return
            b_short_url = b_short_match.group(0)
            async with httpx.AsyncClient(headers=BILIBILI_HEADER, follow_redirects=True) as client:
                resp = await client.get(b_short_url)
                url: str = str(resp.url)
        except (TypeError, httpx.RequestError):
            yield event.plain_result("错误：B站短链接解析失败。")
            return
    # 2. 根据 URL 类型分流处理
    # ================== 动态解析 ==================
    if ('t.bilibili.com' in url or '/opus/' in url) and credential:
        try:
            if '?' in url:
                url = url[:url.index('?')]
            dynamic_match = re.search(r'[^/]+(?!.*/)', url)
            if not dynamic_match:
                yield event.plain_result("无法识别动态ID。")
                return
            dynamic_id = int(dynamic_match.group(0))
            opus = Opus(dynamic_id, credential)
            # Opus.get_content() might not be available, using get_opus_info() instead
            # Since get_opus_info() might not be available either, fallback to a more basic approach
            try:
                dynamic_info = await opus.get_info()
            except AttributeError:
                # If get_info() is not available, use a basic dictionary
                dynamic_info = { }

            # 提取内容
            desc = ""
            user_name = "未知UP主"

            # 尝试不同的数据结构格式获取内容
            if 'desc' in dynamic_info and isinstance(dynamic_info['desc'], dict) and 'text' in dynamic_info['desc']:
                desc = dynamic_info['desc']['text']
            elif 'item' in dynamic_info and isinstance(dynamic_info['item'], dict) and 'description' in dynamic_info[
                'item']:
                desc = dynamic_info['item']['description']

            if 'user' in dynamic_info and isinstance(dynamic_info['user'], dict) and 'name' in dynamic_info['user']:
                user_name = dynamic_info['user']['name']
            elif 'card' in dynamic_info and isinstance(dynamic_info['card'], dict) and 'card' in dynamic_info['card']:
                card = dynamic_info['card']['card']
                if isinstance(card, dict) and 'user' in card and isinstance(card['user'], dict) and 'name' in card[
                    'user']:
                    user_name = card['user']['name']

            # 提取图片
            pics = []
            if isinstance(dynamic_info, dict):
                # 尝试不同的数据结构格式获取图片
                if 'pictures' in dynamic_info and isinstance(dynamic_info['pictures'], list):
                    for p in dynamic_info['pictures']:
                        if isinstance(p, dict) and 'img_src' in p:
                            pics.append(p['img_src'])
                elif 'item' in dynamic_info and isinstance(dynamic_info['item'], dict) and 'pictures' in dynamic_info[
                    'item']:
                    for p in dynamic_info['item']['pictures']:
                        if isinstance(p, dict) and 'img_src' in p:
                            pics.append(p['img_src'])

            message_chain = [Comp.Plain(f"识别到B站动态 (来自: {user_name}):\n{desc}")]
            if pics:
                message_chain.append(Comp.Plain("\n附带图片如下："))
                for pic_url in pics[:9]:  # 最多发9张图
                    message_chain.append(Comp.Image.fromURL(pic_url))

            yield event.chain_result(message_chain)
            return
        except Exception as e:
            yield event.plain_result(f"B站动态解析失败: {e}")
            return
    # ================== 直播间解析 ==================
    if 'live.bilibili.com' in url:
        try:
            room_id_match = re.search(r'\/(\d+)', url.split('?')[0])
            if not room_id_match:
                yield event.plain_result("无法识别直播间ID。")
                return

            room_id = int(room_id_match.group(1))
            room = live.LiveRoom(room_display_id=room_id)
            room_info = (await room.get_room_info())['room_info']
            title, cover, keyframe = room_info['title'], room_info['cover'], room_info['keyframe']

            yield event.chain_result([
                Comp.Plain(f"识别：哔哩哔哩直播\n标题：{title}"),
                Comp.Image.fromURL(cover),
                Comp.Image.fromURL(keyframe)
            ])
            return
        except Exception as e:
            yield event.plain_result(f"B站直播间解析失败: {e}")
            return

    # ================== 专栏解析 ==================
    if '/read/cv' in url:
        try:
            cv_match = re.search(r'cv(\d+)', url)
            if not cv_match:
                yield event.plain_result("无法识别专栏ID。")
                return
            read_id = cv_match.group(1)
            ar = article.Article(int(read_id), credential=credential)
            # article.get_content() might not be available, using get_info() instead
            content = await ar.get_info()
            title = content.get('title', '未知标题')

            yield event.plain_result(f"识别：哔哩哔哩专栏《{title}》")
            return
        except Exception as e:
            yield event.plain_result(f"B站专栏解析失败: {e}")
            return

    # ================== 收藏夹解析 (需要SESSDATA) ==================
    if 'favlist' in url and credential:
        try:
            fid_match = re.search(r'fid=(\d+)', url)
            if not fid_match:
                yield event.plain_result("无法识别收藏夹ID。")
                return
            fav_id = fid_match.group(1)

            yield event.plain_result(f"识别到B站收藏夹ID: {fav_id}，但需要实现获取收藏夹内容的功能。")
            return
        except Exception as e:
            yield event.plain_result(f"B站收藏夹解析失败: {e} (可能需要登录凭据)")
            return

    # ================== 视频解析 ==================
    # 确保URL是有效的视频链接
    if 'video/av' not in url and 'video/BV' not in url:
        # 如果前面的逻辑都没匹配上，并且不是视频链接，则放弃
        search_res = re.search(url_reg, url)
        if search_res:
            yield event.plain_result(f"已识别链接，但暂不支持解析此类型：{search_res.group(0)}")
        return
    # 获取视频信息
    video_id_match = re.search(r"video\/([^\?\/ ]+)", url)
    if not video_id_match:
        yield event.plain_result("无法从链接中提取有效的视频ID。")
        return
    video_id = video_id_match.group(1)

    v = video.Video(video_id, credential=credential)
    video_info = await v.get_info()

    if video_info is None:
        yield event.plain_result(f"识别：B站，出错，无法获取数据！")
        return

    video_title, video_cover, video_desc, video_duration = video_info['title'], video_info['pic'], video_info[
        'desc'], \
        video_info['duration']
    # 校准 分p 的情况
    page_num = 0
    if 'pages' in video_info:
        # 解析URL
        parsed_url = urlparse(url)
        # 检查是否有查询字符串
        if parsed_url.query:
            # 解析查询字符串中的参数
            query_params = parse_qs(parsed_url.query)
            # 获取指定参数的值，如果参数不存在，则返回None
            page_num = int(query_params.get('p', [1])[0]) - 1
        else:
            page_num = 0
        if 'duration' in video_info['pages'][page_num]:
            video_duration = video_info['pages'][page_num].get('duration', video_info.get('duration'))
        else:
            # 如果索引超出范围，使用 video_info['duration'] 或者其他默认值
            video_duration = video_info.get('duration', 0)
    # 删除特殊字符
    video_title = delete_boring_characters(video_title)
    # 按开关拼装信息：封面 / 统计 / 简介 / 在线人数
    info_lines = [f"\n识别：B站，{video_title}"]
    if display_info:
        stats_line = extra_bili_info(video_info)
        if stats_line:
            info_lines.append(stats_line.rstrip(" |"))
    if display_intro and video_desc:
        intro = video_desc
        if intro_len_limit and intro_len_limit > 0 and len(intro) > intro_len_limit:
            intro = intro[:intro_len_limit] + "…"
        info_lines.append(f"📝 简介：{intro}")
    if display_online:
        try:
            online = await v.get_online()
            info_lines.append(f'🏄‍♂️ 总共 {online["total"]} 人在观看，{online["count"]} 人在网页端观看')
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][B站] 在线人数获取失败: {exc}")

    # 封面先下到本地：hdslb 有时也挑 Referer，而且一张图挂掉会把整条消息链丢掉（正文也没了）
    prefix_comps = []
    if display_cover:
        cover_comp = await local_image(video_cover, referer="https://www.bilibili.com/", prefix="bili_cover")
        if cover_comp is not None:
            prefix_comps.append(cover_comp)

    info_text = "\n".join(info_lines)
    if video_duration <= video_duration_maximum:
        yield event.chain_result(prefix_comps + [Comp.Plain(info_text)])
    else:
        yield event.chain_result(prefix_comps + [
            Comp.Plain(
                f"{info_text}\n---------\n⚠️ 当前视频时长 {video_duration // 60} 分钟，超过管理员设置的最长时间 {video_duration_maximum // 60} 分钟！\n已跳过下载；需要下载就调大 BILI_DURATION / VIDEO_DURATION_MAXIMUM")
        ])
        # 超时长不下载：省带宽，也避免把大文件塞进消息通道
        return

    # 获取下载链接
    download_url_data = await v.get_download_url(page_index=page_num)
    detecter = VideoDownloadURLDataDetecter(download_url_data)
    streams = detecter.detect_best_streams()
    video_url, audio_url = streams[0].url, streams[1].url
    # 下载视频和音频
    download_path = os.getcwd() + "/data/bilibili_cache/" + video_id
    os.makedirs(download_path, exist_ok=True)
    merged = f"{download_path}.mp4"
    try:
        await asyncio.gather(
            download_b_file(video_url, f"{download_path}-video.m4s", logger.debug),
            download_b_file(audio_url, f"{download_path}-audio.m4s", logger.debug))
        await merge_file_to_mp4(f"{download_path}-video.m4s", f"{download_path}-audio.m4s", merged)
    except Exception as exc:
        logger.error(f"[R插件][B站] 下载或合并失败: {exc}")
        yield event.plain_result(f"B站视频下载失败：{exc}\n原视频：https://www.bilibili.com/video/{video_id}")
        return
    finally:
        remove_res = remove_files([f"{download_path}-video.m4s", f"{download_path}-audio.m4s"])
        logger.info(remove_res)

    # 发送：NapCat 在容器里看不到宿主机路径，file:// 会 ENOENT，这里转 base64
    logger.info(merged)
    components = video_components(merged, f"{video_id}.mp4", video_size_limit_mb)
    if components:
        yield event.chain_result(components)
    else:
        yield event.plain_result(
            f"视频已下载但无法发送：可能超过大小上限 {video_size_limit_mb}MB，或读取失败。\n"
            f"原视频：https://www.bilibili.com/video/{video_id}"
        )
    _cleanup_bili_cache(download_path, merged)

    # AI 总结
    if credential:
        try:
            cid = video_info['pages'][page_num]['cid']
            ai_conclusion = await v.get_ai_conclusion(cid=cid)
            if ai_conclusion and ai_conclusion.get('summary'):
                summary_text = f"【Bilibili AI 总结】\n{ai_conclusion['summary']}"
                yield event.plain_result(summary_text)
        except Exception as e:
            # 获取AI总结失败，静默处理
            pass
