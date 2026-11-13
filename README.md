<p align="center">
  <a href="https://gitee.com/kyrzy0416/rconsole-plugin">
    <img width="200" src="./images/AstrR.png">
  </a>
</p>

<div align="center">
    <h1>astrbot_plugin_rconsole</h1>
    专门为朋友们写的AstrBot插件（AstrBot_R插件的迁移版本），专注图片视频分享、生活、健康和学习的插件！
    <img src="https://cdn.z.wiki/autoupload/20240819/Zn2g/github-contribution-grid-snake.svg">
</div>

## 手动部署

1. 在 AstrBot 的根目录安装以下依赖
```
uv add bilibili-api-python PyExecJS
```

2. 将 `astrbot_plugin_rconsole` 放入根目录下 `data/plugins`，例如：

> /home/AstrBot/data/plugins

或者直接克隆

```shell
git clone https://github.com/zhiyu1998/astrbot_plugin_rconsole.git ./data/plugins/astrbot_plugin_rconsole
```

3. 运行，填入相关 Cookie 即可进行解析

## 功能列表

| 平台 | 状态 | 说明 |
| --- | :---: | --- |
| 哔哩哔哩 | ✅ | 视频 / 番剧 / 专栏 / 动态 |
| 抖音 | ✅ | 短视频、图集；无 Cookie 时走分享页 SSR 兜底 |
| 快手 | ✅ | 原生页面解析，失败回退第三方接口 |
| 西瓜视频 / 皮皮虾 / 皮皮搞笑 / QQ小世界 / 即刻 | ⚠️ | 依赖第三方解析接口，可在配置项里替换 |
| 微博 | ✅ | 正文、九宫格、视频、热门评论、投票贴图片 |
| AcFun | ✅ | m3u8 分片下载 + ffmpeg 合并 |
| 小红书 | ✅ | 图文 / 视频，正文超长自动折叠 |
| 米游社 | ✅ | 帖子图文 / 视频（带 DS 签名） |
| 小黑盒 | ✅ | 帖子（图文混排、游戏卡片、评论）+ PC / 主机 / 手机游戏详情 |
| 微视 | ✅ | 短视频 |
| 油管 YouTube | ❌ | 未迁移 |
| 网易云 / 酷狗 / QQ音乐 点歌 | ❌ | 未迁移 |

> ✅ 可用　⚠️ 依赖外部接口　❌ 未迁移

## 配置项

### 全局配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `IDENTIFY_PREFIX` | `识别：` | 解析结果前缀，留空则不带前缀 |
| `VIDEO_DURATION_MAXIMUM` | `480` | 视频时长上限（秒），超时只发封面和文字，不下载视频 |
| `VIDEO_SIZE_LIMIT_MB` | `30` | 视频体积上限（MB），超限只发文字和原链接 |
| `IMAGE_USE_FORWARD` | `true` | 多图走合并转发（一张图一个节点），不会刷屏 |
| `IMAGE_BATCH_THRESHOLD` | `0` | 每条转发最多几张图，`0` 表示全部塞进一条 |
| `MSG_ELEMENT_LIMIT` | `50` | 单条转发消息元素上限（图文混排，如小黑盒） |
| `DOWNLOAD_CONCURRENCY` | `4` | 同时下载任务数（队列长度），服务器弱可调 1~2 |
| `DOWNLOAD_THREADS` | `4` | 单文件 Range 分段并发数（>5MB 生效），0/1 = 不分段 |
| `DISABLED_PLATFORMS` | `[]` | 禁用的平台：`douyin` / `bilibili` / `kuaishou` / `weibo` / `acfun` / `xhs` / `miyoushe` / `xiaoheihe` / `weishi` |
| `GENERAL_PARSE_APIS` | 见插件默认值 | 通用第三方解析接口，每行一个，`{}` 为原始链接占位 |

### 平台配置

| 平台 | 配置项 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 哔哩哔哩 | `BILI_SESSDATA` | 空 | 哔哩哔哩 SESSDATA，用于高清晰度解析 |
| 哔哩哔哩 | `BILI_DURATION` | `0` | 独立时长上限（秒），`0` 表示沿用全局 |
| 哔哩哔哩 | `BILI_DISPLAY_COVER` | `true` | 是否显示封面 |
| 哔哩哔哩 | `BILI_DISPLAY_INFO` | `true` | 是否显示点赞 / 硬币 / 收藏等数据 |
| 哔哩哔哩 | `BILI_DISPLAY_INTRO` | `true` | 是否显示简介 |
| 哔哩哔哩 | `BILI_INTRO_LEN_LIMIT` | `50` | 简介截断长度，`0` 或 `-1` 表示不限制 |
| 哔哩哔哩 | `BILI_DISPLAY_ONLINE` | `true` | 是否显示在线人数 |
| 哔哩哔哩 | `BILI_RESOLUTION` | `自动（最高可用）` | 最高画质（8K / 杜比 / HDR / 4K / 1080P+ / 1080P / 720P / 480P / 360P） |
| 哔哩哔哩 | `VIDEO_CODEC` | `自动（推荐）` | 编码偏好：自动 / HEVC / AV1 / AVC |
| 哔哩哔哩 | `BILI_USE_BBDOWN` | `false` | 用 BBDown 接管下载，失败自动回退原生 |
| 哔哩哔哩 | `BBDOWN_PATH` | `BBDown` | BBDown 可执行文件路径（默认从 PATH 找） |
| 哔哩哔哩 | `BILI_CDN` | `不使用` | BBDown 强制 CDN（`--upos-host`） |
| 抖音 | `DOUYIN_CK` | 空 | 抖音 Cookie，留空也能解析（成功率略低） |
| 抖音 | `DOUYIN_DURATION` | `0` | 独立时长上限（秒），`0` 表示沿用全局 |
| 抖音 | `DOUYIN_DISPLAY_COVER` | `true` | 是否显示封面 |
| 抖音 | `DOUYIN_ENABLE_SSR` | `true` | 是否启用分享页 SSR 兜底 |
| 微博 | `WEIBO_CK` | 空 | 微博 Cookie |
| 微博 | `WEIBO_COMMENTS` | `true` | 是否附带热门评论 |
| 小红书 | `XHS_CK` | 空 | 小红书 Cookie（需 `a1` 与 `web_session`） |
| 小红书 | `XHS_DESC_LEN_LIMIT` | `78` | 描述折叠长度，超长会补一条合并转发放全文；`0` 或 `-1` 表示不折叠 |
| 小黑盒 | `XHH_CK` | 空 | 小黑盒 Cookie，**必填** |
| 小黑盒 | `XHH_COMMENT_LIMIT` | `50` | 评论转发条数上限 |

## 待完善...
