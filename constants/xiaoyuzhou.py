"""小宇宙（Xiaoyuzhou FM）接口与音频常量"""

WEB_BASE = "https://www.xiaoyuzhoufm.com"
EPISODE_PAGE = "https://www.xiaoyuzhoufm.com/episode/{}"
PODCAST_PAGE = "https://www.xiaoyuzhoufm.com/podcast/{}"
XIAOYUZHOU_REFERER = "https://www.xiaoyuzhoufm.com/"
DEFAULT_COVER = "https://www.xiaoyuzhoufm.com/favicon.ico"

# 官方 App API（可选：用于付费单集或网页 SSR 兜底）
API_BASE = "https://api.xiaoyuzhoufm.com"
EP_EPISODE_GET = "/v1/episode/get"
EP_PODCAST_GET = "/v1/podcast/get"
EP_PRIVATE_MEDIA_GET = "/v1/private-media/get"
EP_TOKEN_REFRESH = "/app_auth_tokens.refresh"

APP_USER_AGENT = "Xiaoyuzhou/2.57.1 (build:1576; iOS 17.4.1)"
APP_VERSION = "2.57.1"
APP_BUILDNO = "1576"
APP_BUNDLE_ID = "app.podcast.cosmos"

# 常见音频扩展名与 MIME 映射
AUDIO_EXTENSIONS = ("mp3", "m4a", "aac", "ogg", "wav", "flac", "opus")
MIME_TO_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/opus": "opus",
}

# 音频发送与体积阈值
VOICE_BITRATE = "128k"
VOICE_BASE64_MAX_MB = 20
FILE_BASE64_MAX_MB = 20
VOICE_MAX_MB_NON_ONEBOT = 1.0
