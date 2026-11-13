"""
抖音相关常量定义
"""

# 抖音API请求头
DOUYIN_HEADER = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'referer': 'https://www.douyin.com/'
}

# 抖音API地址（带 Cookie + a_bogus 时使用）
DOUYIN_VIDEO_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={}&pc_client_type=1&version_code=190500&version_name=19.5.0&cookie_enabled=true&screen_width=1344&screen_height=756&browser_language=zh-CN&browser_platform=Win32&browser_name=Firefox&browser_version=118.0&browser_online=true&engine_name=Gecko&engine_version=109.0&os_name=Windows&os_version=10&cpu_core_num=16&device_memory=&platform=PC"

# 无水印播放地址（按 video_id 直出）
DOUYIN_TOUTIAO_API = "https://aweme.snssdk.com/aweme/v1/play/?video_id={}&ratio=1080p&line=0"

# 分享页 SSR（免 Cookie 兜底，页面内嵌 window._ROUTER_DATA）
DOUYIN_SHARE_VIDEO_PAGE = "https://www.iesdouyin.com/share/video/{}/"
DOUYIN_SHARE_NOTE_PAGE = "https://www.iesdouyin.com/share/note/{}/"

# 老的 iteminfo 接口，偶尔还能用
DOUYIN_ITEM_INFO = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={}"

# 第三方兜底接口
DOUYIN_THIRD_PARTY = "https://api.xingzhige.com/API/douyin/?url={}"

# URL类型代码字典
URL_TYPE_CODE_DICT = {
    # 抖音/Douyin
    2: 'image',
    4: 'video',
    68: 'image',
    # TikTok
    0: 'video',
    51: 'video',
    55: 'video',
    58: 'video',
    61: 'video',
    150: 'image'
}

# 抖音链接匹配（短链 / 完整链接 / 图文 / 精选页 modal_id）
DOUYIN_URL_REGEX = (
    r"(?:http://|https://)?(?:"
    r"(?:v|live)\.douyin\.com/[A-Za-z\d._?%&+\-=/&#]*"
    r"|webcast\.amemv\.com/[A-Za-z\d._?%&+\-=/&#]*"
    r"|(?:www\.)?iesdouyin\.com/[A-Za-z\d._?%&+\-=/&#]*"
    r"|(?:www\.)?douyin\.com/(?:(?:video|note|live)/[0-9]+|share/slides/[0-9]+|(?:jingxuan|discover)\?[A-Za-z\d._?%&+\-=/&#]*modal_id=[0-9]+[A-Za-z\d._?%&+\-=/&#]*)"
    r")"
)

# 从链接里抠 aweme_id 的顺序
DOUYIN_ID_REGEXES = [
    r"note/(\d+)",
    r"video/(\d+)",
    r"live\.douyin\.com/(\d+)",
    r"/live/(\d+)",
    r"reflow/(\d+)",
    r"modal_id=(\d+)",
    r"slides/(\d+)",
]