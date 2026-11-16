"""
哔哩哔哩的头请求
"""
BILIBILI_HEADER = {
    'User-Agent':
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 '
        'Safari/537.36',
    'referer': 'https://www.bilibili.com',
}

# ---------------------------------------------------------------- 直播

# 直播间基础信息（对应 R 插件的 BILI_STREAM_INFO）
BILI_LIVE_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={}"

# 直播流地址（对应 R 插件的 BILI_STREAM_FLV；quality=4 是原画，platform=web 走 flv）
BILI_LIVE_PLAY_URL = (
    "https://api.live.bilibili.com/room/v1/Room/playUrl?cid={}&quality={}&platform=web"
)

# 直播流清晰度档位（playUrl 的 quality 参数）：4 原画 / 3 蓝光 / 2 超清 / 1 高清 / 0 流畅
BILI_LIVE_QUALITY_MAX = 4

# get_info 里的 live_status：0 未开播 / 1 直播中 / 2 轮播
BILI_LIVE_STATUS_LIVING = 1