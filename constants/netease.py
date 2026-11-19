"""
网易云音乐接口常量

依赖一个**自建**的 NeteaseCloudMusicApi 实例（插件只做客户端）：
https://gitlab.com/Binaryify/neteasecloudmusicapi

端点统一写成路径，base 由配置项 `NETEASE_API_SERVER` 提供，
这样换服务器只用改一个配置，不用改代码。
"""

# ------------------------------------------------------------------ 端点路径
EP_LOGIN_STATUS = "/login/status"
EP_SEARCH = "/search"
EP_CLOUDSEARCH = "/cloudsearch"
EP_SONG_URL_V1 = "/song/url/v1"
EP_SONG_DETAIL = "/song/detail"
EP_SONG_WIKI_SUMMARY = "/song/wiki/summary"
EP_LYRIC = "/lyric"
EP_DJ_PROGRAM_DETAIL = "/dj/program/detail"
EP_USER_CLOUD = "/user/cloud"

# ------------------------------------------------------------------ 音质档位
# 由高到低，用于推导回退链
QUALITY_ORDER = (
    "jymaster",
    "hires",
    "lossless",
    "exhigh",
    "higher",
    "standard",
)

# 空间音频不是普通降级序列，单独编排
SPATIAL_QUALITIES = ("jyeffect", "sky", "dolby")

# 英文档位 -> 中文展示名
QUALITY_LABELS = {
    "standard": "标准",
    "higher": "较高",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
    "jymaster": "超清母带",
    "jyeffect": "高清环绕声",
    "sky": "沉浸环绕声",
    "dolby": "杜比全景声",
}

# 面板下拉选项（中文标签），顺序即建议优先级
QUALITY_OPTIONS = (
    "极高",
    "无损",
    "Hi-Res",
    "超清母带",
    "较高",
    "标准",
    "杜比全景声",
    "沉浸环绕声",
    "高清环绕声",
)

# 中文标签 -> 英文档位（面板取值后反查）
QUALITY_FROM_LABEL = {label: level for level, label in QUALITY_LABELS.items()}

DEFAULT_QUALITY = "exhigh"

# 未登录 / Cookie 失效时，接口实际能给到的最高档位
GUEST_MAX_QUALITY = "exhigh"

# ------------------------------------------------------------------ 资源
DEFAULT_COVER = "https://p2.music.126.net/UeTuwE7pvjBpypWLudqukA==/3132508627578625.jpg"

# 仅用于「音乐卡片」的 url 字段。**绝不能拼进文字消息**：
# 机器人发出的文本里一旦出现 music.163.com，同群其它机器人会再次命中路由形成死循环。
SONG_PAGE = "https://music.163.com/song?id={}"
PROGRAM_PAGE = "https://music.163.com/dj?id={}"

# 网易云常见的占位图，命中就当作没有封面
BROKEN_COVER_MARKERS = (
    "109951169484091680.jpg",
    "5639395138885805.jpg",
)


# ------------------------------------------------------------------ 第三方兜底
# 自建 API 拿不到音源（无版权 / 需要会员 / 服务器限额）时才会用到。
# 这两个接口随时可能失效：失败必须静默降级，绝不能挡住主链路。
TEMP_AUDIO_APIS = (
    "https://www.hhlqilongzhu.cn/api/dg_wyymusic.php?gm={}&n=1&type=json",
    "https://api.lolimi.cn/API/wydg/api.php?msg={}&n=1",
    "http://kilz.top/api/wangyi_music.php?msg={}&n=1",
)

# 音频后缀白名单（临时接口返回的直链常常没有后缀）
AUDIO_EXTENSIONS = ("mp3", "flac", "m4a", "wav", "ape", "ogg", "aac", "mp4")
