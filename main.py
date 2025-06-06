from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from bilibili_api import Credential
from typing import Any, AsyncGenerator

from .core.bili23 import process_bilibili_url
from .core.douyin import process_douyin_url
from .core.xhs import process_xiaohongshu_url


@register("R插件", "RrOrange", "专门为朋友们写的AstrBot插件，专注图片视频分享、生活、健康和学习的插件！", "1.0.0")
class RPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.credential = Credential(sessdata=self.config["BILI_SESSDATA"])
        self.VIDEO_DURATION_MAXIMUM = self.config["VIDEO_DURATION_MAXIMUM"]
        self.DOUYIN_CK = self.config.get("DOUYIN_CK", "")
        self.XHS_CK = self.config.get("XHS_CK", "")

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.regex(r".*(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}).*")
    async def bilibili(self, event: AstrMessageEvent):
        """
        处理B站链接，业务逻辑已移至 core/bili23.py
        """
        async for result in process_bilibili_url(event, self.credential, self.VIDEO_DURATION_MAXIMUM):
            yield result

    @filter.regex(r".*v\.douyin\.com\/[A-Za-z\d._?%&+\-=#]*.*")
    async def douyin(self, event: AstrMessageEvent):
        """
        处理抖音链接，业务逻辑已移至 core/douyin.py
        """
        async for result in process_douyin_url(event, self.DOUYIN_CK):
            yield result
            
    @filter.regex(r".*(https?:\/\/)?(?:www\.)?(xhslink\.com|xiaohongshu\.com)\/[A-Za-z\d._?%&+\-=\/#@]*.*")
    async def xiaohongshu(self, event: AstrMessageEvent):
        """
        处理小红书链接，业务逻辑已移至 core/xhs.py
        """
        async for result in process_xiaohongshu_url(event, self.XHS_CK):
            yield result

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
