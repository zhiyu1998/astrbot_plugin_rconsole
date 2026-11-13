"""
各平台接口常量（迁移自 rconsole-plugin/constants/tools.js）

第一版只保留抖音、B站、快手、微博、AcFun、小红书、米游社、小黑盒、微视的解析链路，
点歌（网易云/酷狗/QQ音乐）、油管、X、Instagram 等不做迁移。
"""

# 通用 UA，B站/微博/小黑盒/米游社等接口都吃这个
COMMON_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 微博

# 单条微博接口（X-Requested-With 必须带）
WEIBO_SINGLE_INFO = "https://m.weibo.cn/statuses/show?id={}"
# 详情页，接口失败时兜底从 HTML 里抠 status JSON
WEIBO_DETAIL_PAGE = "https://m.weibo.cn/detail/{}"
# 热门评论
WEIBO_COMMENTS = "https://m.weibo.cn/comments/hotflow?id={mid}&mid={mid}&max_id_type=0"
# 桌面版接口，用于投票贴图片
WEIBO_AJAX_STATUS = "https://weibo.com/ajax/statuses/show?id={}"
WEIBO_DESKTOP_PAGE = "https://weibo.com/{uid}/{id}"

# ---------------------------------------------------------------- 微视

WEISHI_VIDEO_INFO = "https://h5.weishi.qq.com/webapp/json/weishi/WSH5GetPlayPage?feedid={}"

# ---------------------------------------------------------------- 米游社

MIYOUSHE_ARTICLE = "https://bbs-api.miyoushe.com/post/wapi/getPostFull?post_id={}"
# 米游社网页端 DS 签名盐
MIYOUSHE_SALT = "ZSHlXeQUBis52qD1kEgKt5lUYed4b7Bb"

# ---------------------------------------------------------------- 小黑盒

XHH_BBS_LINK = "https://api.xiaoheihe.cn/bbs/app/link/tree"
XHH_GAME_LINK = "https://api.xiaoheihe.cn/game/get_game_detail"
XHH_CONSOLE_LINK = "https://api.xiaoheihe.cn/game/console/get_game_detail"
XHH_MOBILE_LINK = "https://api.xiaoheihe.cn/game/mobile/get_game_detail"

# ---------------------------------------------------------------- 通用第三方解析

# 快手/西瓜/皮皮虾/皮皮搞笑/QQ小世界/即刻 等平台在没有原生实现时走第三方接口，
# 顺序尝试，{} 处填原始分享链接。
# 注意：这类接口随时可能失效，可在插件配置项 GENERAL_PARSE_APIS 里替换成自己可用的接口。
GENERAL_PARSE_APIS = [
    "http://47.99.158.118/video-crack/v2/parse?content={}",
    "https://api.jkyai.top/API/jhspjx.php?url={}",
    "https://api.yujn.cn/api/pipixia.php?url={}",
    "https://api.bugpk.com/api/pipixia?url={}",
]

# 快手网页端（原生解析）
KUAISHOU_VIDEO_PAGE = "https://www.kuaishou.com/short-video/{}"
KUAISHOU_MOBILE_PAGE = "https://m.chenzhongtech.com/fw/photo/{}"
KUAISHOU_V_MOBILE_PAGE = "https://v.m.chenzhongtech.com/fw/photo/{}"
KUAISHOU_REFERER = "https://www.kuaishou.com/"