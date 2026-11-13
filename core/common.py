"""
公共工具：HTTP 请求、文件下载、ffmpeg 处理、消息组装与发送

统一的约定：
* 所有网络请求都走 httpx（AstrBot 自带依赖），失败返回 None，不抛异常给上层
* 所有临时文件都落在 data/astrbot_plugin_rconsole/temp 下，用完即删
* 需要 Referer/UA 才能下载的资源（微博、快手、小黑盒、B站）一律先下载到本地再发送
"""

import asyncio
import base64
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# ---------------------------------------------------------------- 路径

DATA_DIR = os.path.join(os.getcwd(), "data", "astrbot_plugin_rconsole")
DEFAULT_TEMP_DIR = os.path.join(DATA_DIR, "temp")
# 实际使用的暂存目录，可被配置 TEMP_PATH 覆盖（见 set_temp_dir）
TEMP_DIR = DEFAULT_TEMP_DIR


def set_temp_dir(path: str = "") -> str:
    """
    设置视频/图片的暂存目录（对应 R 插件的「视频暂存位置」）。

    留空 → 默认 data/astrbot_plugin_rconsole/temp；
    相对路径基于 AstrBot 的启动目录；绝对路径原样使用。
    """
    global TEMP_DIR
    target = (path or "").strip()
    if not target:
        TEMP_DIR = DEFAULT_TEMP_DIR
    elif os.path.isabs(target):
        TEMP_DIR = os.path.abspath(target)
    else:
        TEMP_DIR = os.path.abspath(os.path.join(os.getcwd(), target))
    ensure_dir(TEMP_DIR)
    logger.info(f"[R插件] 暂存目录：{TEMP_DIR}")
    return TEMP_DIR


def temp_dir() -> str:
    """当前暂存目录"""
    return TEMP_DIR


def cache_dir(name: str) -> str:
    """暂存目录下的子目录（按平台分），不存在会自动创建"""
    return ensure_dir(os.path.join(TEMP_DIR, name))

# ---------------------------------------------------------------- 下载并发

# 任务级并发：同时最多几个下载在跑（相当于 R 插件的 queueConcurrency）
_DOWNLOAD_CONCURRENCY = 2
# 单文件分段数：同一个文件切成几段并发拉（相当于 videoDownloadConcurrency）
_DOWNLOAD_THREADS = 4
# 小于这个大小就不折腾分段了
_DOWNLOAD_SEGMENT_MIN_MB = 5
_download_sem: Optional[asyncio.Semaphore] = None


def set_download_limits(concurrency: int = 2, threads: int = 4) -> None:
    """配置下载并发：concurrency=队列长度（同时几个任务），threads=单文件分段数"""
    global _DOWNLOAD_CONCURRENCY, _DOWNLOAD_THREADS, _download_sem
    _DOWNLOAD_CONCURRENCY = max(1, int(concurrency or 1))
    _DOWNLOAD_THREADS = max(0, int(threads or 0))
    _download_sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)


def _sem() -> asyncio.Semaphore:
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
    return _download_sem


# ---------------------------------------------------------------- aria2c

_USE_ARIA2 = False
_ARIA2C_PATH = "aria2c"
_ARIA2_MIN_MB = 5  # 小于这个就不折腾 aria2 了，起进程更慢


def set_aria2(enabled: bool, path: str = "aria2c") -> None:
    """开启内置 aria2c 下载；path 可以是命令名或绝对路径"""
    global _USE_ARIA2, _ARIA2C_PATH
    _USE_ARIA2 = bool(enabled)
    _ARIA2C_PATH = (path or "aria2c").strip()


def _resolve_aria2c() -> str:
    candidate = _ARIA2C_PATH
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which(candidate or "aria2c") or ""


async def _download_with_aria2(
    url: str,
    path: str,
    headers: Dict[str, str],
    connections: int,
    timeout: float,
) -> bool:
    """调用 aria2c 下载，成功返回 True"""
    binary = _resolve_aria2c()
    if not binary:
        return False

    work_dir = os.path.dirname(path) or TEMP_DIR
    ensure_dir(work_dir)
    conns = str(max(1, min(int(connections or 8), 16)))
    args = [
        binary, url,
        "-d", work_dir,
        "-o", os.path.basename(path),
        "-x", conns,
        "-s", conns,
        "-k", "1M",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--max-tries=3",
        "--retry-wait=2",
        "--connect-timeout=20",
        "--timeout=600",
        f"--max-connection-per-server={conns}",
    ]
    for key, value in headers.items():
        args += ["--header", f"{key}: {value}"]

    logger.debug("[R插件][aria2c] " + " ".join(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 启动 aria2c 失败: {exc}")
        return False

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning(f"[R插件] aria2c 超时 {url[:80]}")
        return False

    if proc.returncode != 0:
        tail = (stdout or b"").decode("utf-8", "ignore").strip().splitlines()[-3:]
        logger.warning(f"[R插件] aria2c 退出码 {proc.returncode}: {' / '.join(tail)}")
        return False
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        logger.warning("[R插件] aria2c 完成但文件为空")
        return False
    return True


async def _probe_range_size(url: str, headers: Dict[str, str], timeout: float) -> int:
    """探测文件总大小；服务端不支持 Range 时返回 0"""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers={**headers, "Range": "bytes=0-1"})
        if resp.status_code == 206:
            match = re.search(r"/(\d+)", resp.headers.get("content-range", ""))
            if match:
                return int(match.group(1))
        return 0
    except Exception:  # noqa: BLE001
        return 0


async def _download_segmented(
    url: str,
    path: str,
    total: int,
    headers: Dict[str, str],
    threads: int,
    timeout: float,
) -> bool:
    """Range 分段并发下载，全部成功返回 True"""
    threads = max(2, min(threads, 16))
    seg_size = total // threads
    segments = []
    for index in range(threads):
        start = index * seg_size
        end = total - 1 if index == threads - 1 else (start + seg_size - 1)
        segments.append((start, end))

    with open(path, "wb") as fp:
        fp.truncate(total)

    async def _one(start: int, end: int) -> bool:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                async with client.stream(
                    "GET", url, headers={**headers, "Range": f"bytes={start}-{end}"}
                ) as resp:
                    if resp.status_code != 206:
                        return False
                    with open(path, "r+b") as fp:
                        fp.seek(start)
                        written = 0
                        async for chunk in resp.aiter_bytes(65536):
                            fp.write(chunk)
                            written += len(chunk)
                        return written == end - start + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件] 分段下载失败 {start}-{end}: {exc}")
            return False

    results = await asyncio.gather(*[_one(s, e) for s, e in segments])
    return all(results)


# ---------------------------------------------------------------- 基础文本工具


def delete_boring_characters(sentence: str) -> str:
    """去除标题里的特殊字符，用于生成文件名（保留字母数字与中文）"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", sentence or "")


def html_to_text(raw: str) -> str:
    """把微博/小黑盒的 HTML 片段压成纯文本"""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = re.sub(r"</(p|h[1-6]|blockquote|div|li)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_str(text: str, limit: int, ellipsis: str = "…") -> str:
    text = "" if text is None else str(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))] + ellipsis


def chunked(items: Sequence[Any], size: int) -> List[List[Any]]:
    if size <= 0:
        return [list(items)]
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def get_message_text(event: AstrMessageEvent) -> str:
    """
    取消息里的可解析文本。

    优先用 event.message_str；某些适配器把卡片/小程序链接放在 Json 组件里，
    这时退化成把所有 Plain / Json / 其它带 data 的组件拼起来。
    """
    text = (getattr(event, "message_str", "") or "").strip()
    if text:
        return text
    parts: List[str] = []
    message_obj = getattr(event, "message_obj", None)
    for comp in getattr(message_obj, "message", None) or []:
        if isinstance(comp, Comp.Plain):
            parts.append(getattr(comp, "text", "") or "")
            continue
        data = getattr(comp, "data", None)
        if data:
            parts.append(json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data)
    return "".join(parts).strip()


def safe_filename(name: str, default: str = "media", max_len: int = 80) -> str:
    name = delete_boring_characters(name or "") or default
    return name[:max_len]


# ---------------------------------------------------------------- 路径工具


def ensure_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] 创建目录失败 {path}: {exc}")
    return path


def build_temp_path(prefix: str = "media", suffix: str = ".mp4") -> str:
    ensure_dir(TEMP_DIR)
    return os.path.join(TEMP_DIR, f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}")


def delete_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 删除临时文件失败 {path}: {exc}")


def remove_files(file_paths: Iterable[str]) -> Dict[str, str]:
    """批量删除，返回每个路径的处理结果"""
    results: Dict[str, str] = {}
    for file_path in file_paths:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                results[file_path] = "remove"
            except Exception as exc:  # noqa: BLE001
                results[file_path] = f"error: {exc}"
        else:
            results[file_path] = "don't exist"
    return results


# ---------------------------------------------------------------- HTTP


async def fetch_text(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 15.0,
    encoding: Optional[str] = None,
) -> Optional[str]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers=headers or {}, params=params or None)
        if resp.status_code >= 400:
            logger.warning(f"[R插件] GET {url} 状态码 {resp.status_code}")
            return None
        if encoding:
            return resp.content.decode(encoding, errors="ignore")
        return resp.text
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] GET 失败 {url}: {exc}")
        return None


async def fetch_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 15.0,
) -> Optional[Any]:
    text = await fetch_text(url, headers=headers, params=params, timeout=timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        # 有些接口会返回 jsonp 或者前后带杂字符，退化成用 JSONDecoder 抠第一个对象
        try:
            decoder = json.JSONDecoder()
            start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
            if start >= 0:
                obj, _ = decoder.raw_decode(text[start:])
                return obj
        except Exception:  # noqa: BLE001
            pass
        logger.warning(f"[R插件] 解析 JSON 失败 {url}: {text[:120]}")
        return None


async def post_json(
    url: str,
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
) -> Optional[Any]:
    """POST JSON 并解析响应（快手 graphql 之类）"""
    try:
        req_headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0", **(headers or {})}
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=req_headers)
        if resp.status_code >= 400:
            logger.warning(f"[R插件] POST {url} 状态码 {resp.status_code}")
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] POST 失败 {url}: {exc}")
        return None


async def resolve_redirect(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> str:
    """跟随短链跳转，返回最终地址；失败时原样返回"""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers=headers or {})
        return str(resp.url) or url
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 短链跳转失败 {url}: {exc}")
        return url


async def probe_media_url(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    referer: Optional[str] = None,
    timeout: float = 15.0,
) -> bool:
    """
    探测直链是否还能下载（第三方接口经常返回过期地址）。

    先 HEAD，服务端不支持 HEAD 时退化成 Range GET。
    """
    if not url:
        return False
    req_headers = {"User-Agent": "Mozilla/5.0", **(headers or {})}
    if referer:
        req_headers.setdefault("Referer", referer)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.head(url, headers=req_headers)
            if resp.status_code in (405, 403, 404, 410) or resp.status_code >= 500:
                resp = await client.get(url, headers={**req_headers, "Range": "bytes=0-1024"})
            if resp.status_code >= 400:
                return False
            length = resp.headers.get("content-length")
            if length is not None:
                try:
                    return int(length) > 0
                except ValueError:
                    return True
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 直链探测失败 {url}: {exc}")
        return False


async def download_file(
    url: str,
    path: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 180.0,
    referer: Optional[str] = None,
    threads: Optional[int] = None,
) -> Optional[str]:
    """
    流式下载到本地，返回路径；失败返回 None。

    * 全局 `_download_sem` 限制同时下载的任务数（相当于队列）
    * 文件够大且服务端支持 Range 时，按 `_DOWNLOAD_THREADS` 分段并发拉（相当于多线程下载）
    """
    if not url:
        return None
    ensure_dir(os.path.dirname(path) or TEMP_DIR)
    req_headers = {"User-Agent": "Mozilla/5.0", **(headers or {})}
    if referer:
        req_headers.setdefault("Referer", referer)

    seg_threads = _DOWNLOAD_THREADS if threads is None else max(0, int(threads))

    async with _sem():
        # 只有视频这类调用会带 seg_threads（图片会传 0，避免每次多探一个请求）
        want_aria2 = _USE_ARIA2 and seg_threads > 0
        total = 0
        if want_aria2 or (seg_threads and seg_threads > 1):
            total = await _probe_range_size(url, req_headers, timeout)

        # aria2c 优先（多连接 + 断点重试都由它负责）
        if want_aria2 and (not total or total >= _ARIA2_MIN_MB * 1024 * 1024):
            if await _download_with_aria2(url, path, req_headers, seg_threads * 4, timeout):
                return path
            logger.warning("[R插件] aria2c 下载失败，回退内置下载")
            delete_file(path)
            delete_file(path + ".aria2")

        # 没有 aria2c 就用自己的 Range 分段
        if seg_threads and seg_threads > 1:
            if total and total >= _DOWNLOAD_SEGMENT_MIN_MB * 1024 * 1024:
                logger.info(f"[R插件] 分段下载 {seg_threads} 线程，共 {total / 1024 / 1024:.1f}MB: {url[:80]}")
                if await _download_segmented(url, path, total, req_headers, seg_threads, timeout):
                    return path
                logger.warning("[R插件] 分段下载失败，回退单连接下载")
                delete_file(path)

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                async with client.stream("GET", url, headers=req_headers) as resp:
                    if resp.status_code >= 400:
                        logger.warning(f"[R插件] 下载失败 {url} 状态码 {resp.status_code}")
                        return None
                    with open(path, "wb") as fp:
                        async for chunk in resp.aiter_bytes(65536):
                            fp.write(chunk)
            if os.path.getsize(path) == 0:
                delete_file(path)
                return None
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件] 下载异常 {url}: {exc}")
            delete_file(path)
            return None


async def download_images(
    urls: Sequence[str],
    *,
    headers: Optional[Dict[str, str]] = None,
    prefix: str = "img",
    referer: Optional[str] = None,
    limit: int = 120,
) -> List[str]:
    """并发下载一组图片，返回成功的本地路径（顺序保持）"""
    targets = [u for u in urls if u][:limit]
    if not targets:
        return []

    async def _one(index: int, url: str) -> Optional[str]:
        path = build_temp_path(f"{prefix}_{index}", ".jpg")
        return await download_file(
            url, path, headers=headers, referer=referer, timeout=60, threads=0
        )

    results = await asyncio.gather(*[_one(i, u) for i, u in enumerate(targets)], return_exceptions=True)
    return [r for r in results if isinstance(r, str)]


# ---------------------------------------------------------------- ffmpeg


def _run_ffmpeg(args: List[str], timeout: int = 300) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(f"[R插件] ffmpeg 失败: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
            return False
        return True
    except FileNotFoundError:
        logger.error("[R插件] 未找到 ffmpeg，请先在服务器上安装")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] ffmpeg 执行异常: {exc}")
        return False


async def merge_video_audio(video_path: str, audio_path: str, output_path: str) -> Optional[str]:
    """B站/部分平台音视频分离，合并成 mp4"""
    ok = await asyncio.to_thread(
        _run_ffmpeg,
        ["-y", "-i", video_path, "-i", audio_path, "-c", "copy", output_path],
    )
    if ok and os.path.exists(output_path):
        return output_path
    return None


async def concat_ts(ts_list_file: str, output_path: str) -> Optional[str]:
    """AcFun 之类的 m3u8 分片，按 concat 列表合并"""
    ok = await asyncio.to_thread(
        _run_ffmpeg,
        ["-y", "-f", "concat", "-safe", "0", "-i", ts_list_file, "-c", "copy", output_path],
    )
    if ok and os.path.exists(output_path):
        return output_path
    return None


async def convert_flv_to_mp4(flv_path: str) -> Optional[str]:
    output = flv_path.rsplit(".", 1)[0] + ".mp4"
    ok = await asyncio.to_thread(_run_ffmpeg, ["-y", "-i", flv_path, "-c", "copy", output])
    return output if ok and os.path.exists(output) else None


# ---------------------------------------------------------------- 消息组装


def create_forward_message(
    content_list: List[Union[List[Any], Dict[str, Any]]],
    default_name: Optional[str] = None,
    default_uin: Optional[Union[int, str]] = None,
) -> Comp.Nodes:
    """把多段内容打包成合并转发节点"""
    nodes = Comp.Nodes([])
    for item in content_list:
        if isinstance(item, list):
            nodes.nodes.append(Comp.Node(uin=default_uin, name=default_name, content=item))
        elif isinstance(item, dict) and "content" in item:
            nodes.nodes.append(Comp.Node(
                uin=item.get("uin", default_uin),
                name=item.get("name", default_name),
                content=item["content"],
            ))
    return nodes


async def send_forward_message(
    event: AstrMessageEvent,
    content_list: List[Union[List[Any], Dict[str, Any]]],
    default_name: Optional[str] = None,
    default_uin: Optional[Union[int, str]] = None,
) -> Any:
    if default_name is None:
        default_name = event.get_sender_name()
    if default_uin is None:
        default_uin = event.get_sender_id()
    nodes = create_forward_message(content_list, default_name, default_uin)
    return event.chain_result([nodes])


def build_image_components(paths: Sequence[str]) -> List[Comp.Image]:
    return [Comp.Image.fromFileSystem(p) for p in paths if p]


# AstrBot 配了 callback_api_base 时，Video.to_dict() 会自动把本地文件注册成 URL，
# 这时候走 file:// 反而最省（流式，不占内存）；没配就只能 base64，否则 NapCat 在容器里
# 看不到宿主机路径，直接 ENOENT。
_USE_FILE_SERVICE = False


def set_file_service(enabled: bool) -> None:
    global _USE_FILE_SERVICE
    _USE_FILE_SERVICE = bool(enabled)


def video_components(path: str, name: str = "", max_mb: int = 0) -> List[Any]:
    """
    本地视频转成发送组件。

    * 配了 callback_api_base：直接用 file:// 交给 AstrBot 的文件服务转 URL（大文件首选）
    * 没配：读成 base64:// 发送（AstrBot 对 Image/Record 就是这么干的，唯独 Video 是透传）

    超过 max_mb（MB）时返回空列表，由调用方改发文字提示。
    """
    if not path or not os.path.exists(path):
        return []

    size_mb = os.path.getsize(path) / 1024 / 1024
    if max_mb and max_mb > 0 and size_mb > max_mb:
        logger.warning(f"[R插件] 视频 {path} 体积 {size_mb:.1f}MB 超过上限 {max_mb}MB，跳过发送")
        return []

    if _USE_FILE_SERVICE:
        return [Comp.Video.fromFileSystem(path)]

    try:
        with open(path, "rb") as fp:
            payload = base64.b64encode(fp.read()).decode()
        return [Comp.Video(f"base64://{payload}")]
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] 读取视频失败 {path}: {exc}")
        return []


async def local_image(
    url: str,
    *,
    referer: str = "",
    headers: Optional[Dict[str, str]] = None,
    prefix: str = "img",
    timeout: float = 60.0,
) -> Optional[Any]:
    """
    先把图片下载到本地再发。

    第三方 CDN（抖音 douyinpic / 小黑盒 / 米游社 …）基本都校验 Referer，而
    `Comp.Image.fromURL()` 是让 AstrBot 自己去下（不带 Referer）→ 403；
    更要命的是**一张图失败会把整条消息链丢掉**，后面的正文也跟着发不出去。
    """
    if not url:
        return None
    path = await download_file(
        url, build_temp_path(prefix, ".jpg"), headers=headers, referer=referer, timeout=timeout
    )
    if not path:
        return None
    release_file(path, 180.0)
    return Comp.Image.fromFileSystem(path)


def image_send_results(
    event: AstrMessageEvent,
    components: Sequence[Any],
    *,
    use_forward: bool = True,
    batch: int = 0,
) -> List[Any]:
    """
    多图统一发送（对应 R 插件的 sendImagesInBatches）。

    * use_forward=True：走合并转发，**一张图一个节点**，和原插件观感一致，也不会刷屏
    * batch>0：每条转发最多装 batch 张，超过就拆成多条转发；0 = 全部塞一条
    * use_forward=False：直接发，batch>0 时同样按批拆成多条消息

    节点里的图片由 AstrBot 的 `Node.to_dict()` 自动转 base64，所以本地文件也能进转发。
    """
    comps = [c for c in components if c is not None]
    if not comps:
        return []

    if not use_forward:
        groups = chunked(comps, batch) if batch and batch > 0 else [comps]
        return [event.chain_result(group) for group in groups]

    name = event.get_sender_name()
    uin = event.get_sender_id()
    nodes = [Comp.Node(uin=uin, name=name, content=[comp]) for comp in comps]
    node_groups = chunked(nodes, batch) if batch and batch > 0 else [nodes]
    return [event.chain_result([Comp.Nodes(list(group))]) for group in node_groups]


def release_file(path: str, delay: float = 180.0) -> None:
    """延后删除：AstrBot 是发送时才读文件（图片会转 base64），删早了会读不到"""
    if not path:
        return
    try:
        asyncio.get_running_loop().call_later(delay, lambda: delete_file(path))
    except RuntimeError:
        delete_file(path)


async def cover_text_result(
    event: AstrMessageEvent,
    cover_url: str,
    text: str,
    *,
    referer: str = "",
    headers: Optional[Dict[str, str]] = None,
    prefix: str = "cover",
) -> Any:
    """
    封面 + 正文的固定组合。

    封面下载失败就只发正文——绝不能让一张封面把整条消息带崩。
    """
    cover = await local_image(cover_url, referer=referer, headers=headers, prefix=prefix)
    if cover is not None:
        return event.chain_result([cover, Comp.Plain("\n" + text)])
    return event.plain_result(text)


def release_video(path: str, delay: float = 300.0) -> None:
    """
    发送完本地视频后清理。

    base64 模式数据已经在内存里了，可以立刻删；
    文件服务模式下 AstrBot 是在真正发送时才把路径注册成 URL，文件必须还在，
    所以延后删除，交给事件循环定时器处理。
    """
    if not path:
        return
    if _USE_FILE_SERVICE:
        try:
            asyncio.get_running_loop().call_later(delay, lambda: delete_file(path))
            return
        except RuntimeError:
            pass
    delete_file(path)


def video_result(event: AstrMessageEvent, path: str, name: str, max_mb: int, fallback: str) -> Any:
    """视频统一发送：能发就发，超上限/读不出来就发文字提示（带原链接）"""
    components = video_components(path, name, max_mb)
    if components:
        return event.chain_result(components)
    return event.plain_result(fallback)


def single_result(event: AstrMessageEvent, *components: Any) -> Any:
    """一组组件拼成一条消息返回"""
    flat: List[Any] = []
    for comp in components:
        if comp is None:
            continue
        if isinstance(comp, (list, tuple)):
            flat.extend([c for c in comp if c is not None])
        else:
            flat.append(comp)
    return event.chain_result(flat)


def text_result(event: AstrMessageEvent, text: str) -> Any:
    return event.plain_result(text)