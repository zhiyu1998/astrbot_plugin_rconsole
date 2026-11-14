"""
R插件（AstrBot 迁移版）入口

迁移自 Yunzai 版 rconsole-plugin 的 apps/tools.js，第一版只保留解析类功能：
抖音、哔哩哔哩、快手、微博、AcFun、小红书、米游社、小黑盒、微视。

点歌（网易云 / 酷狗 / QQ音乐）、油管、X、Instagram、Telegram、贴吧、最右、
视频号、链接总结这些分支没有迁移。
"""

import asyncio
import re
from typing import Any, AsyncGenerator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from bilibili_api import Credential

from .core.acfun import process_acfun_url
from .core.bili23 import process_bilibili_url
from .core import card as card_parser
from .core.common import (
    proxy_for,
    set_aria2,
    set_download_limits,
    set_file_service,
    set_proxy,
    set_temp_dir,
)
from .core.douyin import process_douyin_url
from .core.general import process_general_url
from .core.youtube import check_proxy, process_youtube_url, set_cookie_file
from .core.miyoushe import process_miyoushe_url
from .core.weibo import process_weibo_url
from .core.weishi import process_weishi_url
from .core.xhs import process_xiaohongshu_url
from .core.xiaoheihe import process_xiaoheihe_url

PLUGIN_NAME = "astrbot_plugin_rconsole"
PLATFORM_ALIASES = {
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "kuaishou": "快手",
    "weibo": "微博",
    "acfun": "AcFun",
    "xhs": "小红书",
    "miyoushe": "米游社",
    "xiaoheihe": "小黑盒",
    "weishi": "微视",
    "youtube": "YouTube",
}

# 面板里显示的是中文标签，代码里用 auto/hevc/av1/avc（兼容直接填英文）
_CODEC_ALIAS = {
    "自动（推荐）": "auto",
    "HEVC/H.265": "hevc",
    "AV1": "av1",
    "AVC/H.264": "avc",
    "auto": "auto",
    "hevc": "hevc",
    "av1": "av1",
    "avc": "avc",
}


@register(
    PLUGIN_NAME,
    "RrOrange",
    "R插件迁移版：抖音/B站/快手/微博/AcFun/小红书/米游社/小黑盒/微视 链接解析",
    "1.2.5",
)
class RPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.credential = Credential(sessdata=str(self._cfg("BILI_SESSDATA", "")))
        self.video_duration_maximum = int(self._cfg("VIDEO_DURATION_MAXIMUM", 480) or 480)
        self.video_size_limit_mb = int(self._cfg("VIDEO_SIZE_LIMIT_MB", 30) or 30)
        # 0/空 → 用默认 30MB：超限只发文字和原链接，避免把几十 MB 的视频塞进消息通道
        if self.video_size_limit_mb <= 0:
            self.video_size_limit_mb = 30
        self.image_batch_threshold = int(self._cfg("IMAGE_BATCH_THRESHOLD", 0) or 0)
        self.image_use_forward = bool(self._cfg("IMAGE_USE_FORWARD", True))
        self.msg_element_limit = int(self._cfg("MSG_ELEMENT_LIMIT", 50) or 50)
        self.identify_prefix = str(self._cfg("IDENTIFY_PREFIX", "识别：") or "识别：")

        # 平台独立时长上限：0 / 未填 → 沿用全局 VIDEO_DURATION_MAXIMUM
        self.bili_duration = int(self._cfg("BILI_DURATION", 0) or 0) or self.video_duration_maximum
        self.douyin_duration = int(self._cfg("DOUYIN_DURATION", 0) or 0) or self.video_duration_maximum

        # 哔哩哔哩展示开关
        self.bili_display_cover = bool(self._cfg("BILI_DISPLAY_COVER", True))
        self.bili_display_info = bool(self._cfg("BILI_DISPLAY_INFO", True))
        self.bili_display_intro = bool(self._cfg("BILI_DISPLAY_INTRO", True))
        self.bili_display_online = bool(self._cfg("BILI_DISPLAY_ONLINE", True))
        self.bili_intro_len_limit = int(self._cfg("BILI_INTRO_LEN_LIMIT", 50) or 0)

        # 哔哩哔哩画质 / 编码 / 下载方式
        self.bili_resolution = str(self._cfg("BILI_RESOLUTION", "自动（最高可用）") or "自动（最高可用）")
        self.video_codec = _CODEC_ALIAS.get(str(self._cfg("VIDEO_CODEC", "自动（推荐）")), "auto")
        self.bili_use_bbdown = bool(self._cfg("BILI_USE_BBDOWN", False))
        self.bbdown_path = str(self._cfg("BBDOWN_PATH", "BBDown") or "BBDown")
        self.bili_cdn = str(self._cfg("BILI_CDN", "不使用") or "不使用")

        # 抖音
        self.douyin_enable_ssr = bool(self._cfg("DOUYIN_ENABLE_SSR", True))

        # 小红书折叠长度（0 = 不折叠）
        self.xhs_desc_len_limit = int(self._cfg("XHS_DESC_LEN_LIMIT", 78) or 0)

        # 小黑盒
        self.xhh_comment_limit = int(self._cfg("XHH_COMMENT_LIMIT", 50) or 50)

        self.douyin_ck = str(self._cfg("DOUYIN_CK", ""))
        self.xhs_ck = str(self._cfg("XHS_CK", ""))
        self.weibo_ck = str(self._cfg("WEIBO_CK", ""))
        self.xhh_ck = str(self._cfg("XHH_CK", ""))

        self.douyin_display_cover = bool(self._cfg("DOUYIN_DISPLAY_COVER", True))
        self.weibo_comments = bool(self._cfg("WEIBO_COMMENTS", True))
        self.general_parse_apis = self._list_cfg("GENERAL_PARSE_APIS", [])

        # 视频发送方式：配了 callback_api_base 说明有文件服务，本地文件能转成 URL；
        # 否则 NapCat 在容器里看不到宿主机路径，只能走 base64
        self.file_service_base = ""
        self._apply_file_service_mode(quiet=True)

        # 下载并发：concurrency = 同时几个任务（队列），threads = 单文件分段数（多线程）
        self.download_concurrency = int(self._cfg("DOWNLOAD_CONCURRENCY", 2) or 2)
        self.download_threads = int(self._cfg("DOWNLOAD_THREADS", 4) or 0)
        set_download_limits(self.download_concurrency, self.download_threads)

        # aria2c：视频走 aria2 多连接下载（没装就自动回退内置下载）
        self.use_aria2 = bool(self._cfg("DOWNLOADS_USE_ARIA2", False))
        self.aria2c_path = str(self._cfg("ARIA2C_PATH", "aria2c") or "aria2c")
        set_aria2(self.use_aria2, self.aria2c_path)
        self.bbdown_aria2c = bool(self._cfg("BILI_BBDOWN_ARIA2C", False))

        # 代理（YouTube 等需要魔法的平台）：填一个完整地址，留空即直连
        self.proxy_platforms = self._list_cfg("PROXY_PLATFORMS", ["youtube"])
        self.proxy_url = set_proxy(str(self._cfg("PROXY_URL", "") or ""), self.proxy_platforms)
        if self.proxy_url:
            logger.info(f"[R插件] 代理已启用：{self.proxy_url} → {self.proxy_platforms or '全部平台'}")

        # YouTube
        self.youtube_duration = int(self._cfg("YOUTUBE_DURATION", 0) or 0) or self.video_duration_maximum
        self.youtube_display_cover = bool(self._cfg("YOUTUBE_DISPLAY_COVER", True))
        self.youtube_display_info = bool(self._cfg("YOUTUBE_DISPLAY_INFO", True))
        self.youtube_desc_len_limit = int(self._cfg("YOUTUBE_DESC_LEN_LIMIT", 0) or 0)
        self.youtube_max_resolution = str(self._cfg("YOUTUBE_MAX_RESOLUTION", "1080P") or "1080P")
        self.youtube_cookies = set_cookie_file(str(self._cfg("YOUTUBE_COOKIES", "") or ""))
        if self.youtube_cookies:
            logger.info(f"[R插件] YouTube Cookie：{self.youtube_cookies}")

        # 视频/图片暂存目录（对应 R 插件的「视频暂存位置」），留空用插件默认目录
        set_temp_dir(str(self._cfg("TEMP_PATH", "") or ""))

        # 小程序 / 分享卡片解析：把 OneBot json 段的内容注入 message_str，
        # 否则卡片消息的 message_str 为空，正则路由根本进不来
        self.card_parse_enabled = bool(self._cfg("CARD_PARSE_ENABLED", True))
        self._card_patch_state = {}
        if self.card_parse_enabled:
            self._card_patch_state = card_parser.install()
            logger.info(
                "[R插件] 卡片解析补丁：适配器={} 日志大纲={}".format(
                    "OK" if self._card_patch_state.get("adapter") else "FAIL",
                    "OK" if self._card_patch_state.get("outline") else "FAIL",
                )
            )

    # ------------------------------------------------------------ 配置读取

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _list_cfg(self, key: str, default: list) -> list:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[\n,]", value) if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return default

    def _enabled(self, name: str) -> bool:
        """DISABLED_PLATFORMS 里可以写英文 key 或中文名"""
        disabled = {item.lower() for item in self._list_cfg("DISABLED_PLATFORMS", [])}
        if not disabled:
            return True
        return name.lower() not in disabled and PLATFORM_ALIASES.get(name, "") not in disabled

    def _apply_file_service_mode(self, quiet: bool = False) -> None:
        """
        视频发送模式选择。

        * 全局配了 callback_api_base → Video.to_dict() 会把本地文件注册成 URL，大文件走这条路最稳
        * 没配 → 把视频读成 base64:// 发送（NapCat 在容器里看不到宿主机路径）
        """
        base = ""
        try:
            cfg = self.context.get_config()
            base = str((cfg or {}).get("callback_api_base") or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件] 读取全局 callback_api_base 失败，按 base64 模式发送视频: {exc}")
        self.file_service_base = base
        set_file_service(bool(base))
        if not quiet:
            logger.info(f"[R插件] 视频发送模式：{'文件服务 ' + base if base else 'base64（未配置 callback_api_base）'}")

    # ------------------------------------------------------------ 生命周期

    async def initialize(self):
        self._apply_file_service_mode()
        logger.info(f"[R插件] 已加载，解析平台：{'、'.join(PLATFORM_ALIASES.values())}")
        # 配了代理就顺手自检一次，连不上时提前在日志里说清楚
        if self.proxy_url and self._enabled("youtube"):
            asyncio.create_task(self._check_proxy_health())

    async def _check_proxy_health(self):
        try:
            result = await check_proxy(self.proxy_url)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件] 代理自检异常: {exc}")
            return
        if result.get("ok"):
            logger.info(f"[R插件] 代理自检通过：{self.proxy_url} 可访问 YouTube")
        else:
            logger.warning(
                f"[R插件] 代理自检失败：{self.proxy_url} 连不上 YouTube"
                f"（{result.get('error') or result.get('status')}），请检查代理是否可用"
            )

    async def terminate(self):
        if self.card_parse_enabled:
            card_parser.uninstall()
        logger.info("[R插件] 已卸载")

    # ------------------------------------------------------------ 抖音

    @filter.regex(r".*(douyin\.com|iesdouyin\.com|webcast\.amemv\.com).*")
    async def douyin(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("douyin"):
            return
        async for result in process_douyin_url(
            event,
            douyin_ck=self.douyin_ck,
            video_duration_maximum=self.douyin_duration,
            identify_prefix=self.identify_prefix,
            display_cover=self.douyin_display_cover,
            image_batch_threshold=self.image_batch_threshold,
            video_size_limit_mb=self.video_size_limit_mb,
            image_use_forward=self.image_use_forward,
            enable_ssr=self.douyin_enable_ssr,
        ):
            yield result

    # ------------------------------------------------------------ YouTube

    @filter.regex(r".*(youtube\.com|youtu\.be).*")
    async def youtube(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("youtube"):
            return
        async for result in process_youtube_url(
            event,
            self.youtube_duration,
            video_size_limit_mb=self.video_size_limit_mb,
            proxy=proxy_for("youtube"),
            display_cover=self.youtube_display_cover,
            display_info=self.youtube_display_info,
            desc_len_limit=self.youtube_desc_len_limit,
            max_resolution=self.youtube_max_resolution,
        ):
            yield result

    # ------------------------------------------------------------ 哔哩哔哩

    @filter.regex(r".*(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}).*")
    async def bilibili(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("bilibili"):
            return
        async for result in process_bilibili_url(
            event,
            self.credential,
            self.bili_duration,
            video_size_limit_mb=self.video_size_limit_mb,
            display_cover=self.bili_display_cover,
            display_info=self.bili_display_info,
            display_intro=self.bili_display_intro,
            display_online=self.bili_display_online,
            intro_len_limit=self.bili_intro_len_limit,
            resolution=self.bili_resolution,
            video_codec=self.video_codec,
            use_bbdown=self.bili_use_bbdown,
            bbdown_path=self.bbdown_path,
            cdn=self.bili_cdn,
            bbdown_aria2c=self.bbdown_aria2c,
        ):
            yield result

    # ------------------------------------------------------------ 快手 / 西瓜 / 皮皮虾等

    @filter.regex(
        r".*(kuaishou\.com|chenzhongtech\.com|ixigua\.com|pipix\.com|pipigx\.com|xsj\.qq\.com|okjike\.com).*"
    )
    async def general(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("kuaishou"):
            return
        async for result in process_general_url(
            event,
            parse_apis=self.general_parse_apis,
            identify_prefix=self.identify_prefix,
            image_batch_threshold=self.image_batch_threshold,
            video_size_limit_mb=self.video_size_limit_mb,
            image_use_forward=self.image_use_forward,
        ):
            yield result

    # ------------------------------------------------------------ 微博

    @filter.regex(r".*(weibo\.com|weibo\.cn).*")
    async def weibo(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("weibo"):
            return
        async for result in process_weibo_url(
            event,
            cookie=self.weibo_ck,
            identify_prefix=self.identify_prefix,
            enable_comments=self.weibo_comments,
            image_batch_threshold=self.image_batch_threshold,
            video_size_limit_mb=self.video_size_limit_mb,
            image_use_forward=self.image_use_forward,
        ):
            yield result

    # ------------------------------------------------------------ AcFun

    @filter.regex(r".*(acfun\.cn|ac[0-9]{6,10}).*")
    async def acfun(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("acfun"):
            return
        async for result in process_acfun_url(
            event,
            identify_prefix=self.identify_prefix,
            video_size_limit_mb=self.video_size_limit_mb,
        ):
            yield result

    # ------------------------------------------------------------ 小红书

    @filter.regex(r".*(xhslink\.(com|cn)|xiaohongshu\.com).*")
    async def xiaohongshu(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("xhs"):
            return
        async for result in process_xiaohongshu_url(
            event,
            self.xhs_ck,
            image_use_forward=self.image_use_forward,
            image_batch_threshold=self.image_batch_threshold,
            video_size_limit_mb=self.video_size_limit_mb,
            identify_prefix=self.identify_prefix,
            desc_len_limit=self.xhs_desc_len_limit,
        ):
            yield result

    # ------------------------------------------------------------ 米游社

    @filter.regex(r".*miyoushe\.com.*")
    async def miyoushe(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("miyoushe"):
            return
        async for result in process_miyoushe_url(
            event,
            identify_prefix=self.identify_prefix,
            image_batch_threshold=self.image_batch_threshold,
            video_size_limit_mb=self.video_size_limit_mb,
            image_use_forward=self.image_use_forward,
        ):
            yield result

    # ------------------------------------------------------------ 小黑盒

    @filter.regex(r".*xiaoheihe\.cn.*")
    async def xiaoheihe(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("xiaoheihe"):
            return
        async for result in process_xiaoheihe_url(
            event,
            cookie=self.xhh_ck,
            identify_prefix=self.identify_prefix,
            image_batch_threshold=self.image_batch_threshold,
            video_size_limit_mb=self.video_size_limit_mb,
            msg_element_limit=self.msg_element_limit,
            comment_limit=self.xhh_comment_limit,
        ):
            yield result

    # ------------------------------------------------------------ 微视

    @filter.regex(r".*weishi\.qq\.com.*")
    async def weishi(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        if not self._enabled("weishi"):
            return
        async for result in process_weishi_url(
            event,
            identify_prefix=self.identify_prefix,
            video_size_limit_mb=self.video_size_limit_mb,
        ):
            yield result