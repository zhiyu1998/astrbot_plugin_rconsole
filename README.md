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

## 安装部署

### 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | v4.5.0 及以上 |
| Python | 3.10+（用 AstrBot 自带的虚拟环境即可） |
| ffmpeg | 可选但建议安装，AcFun 分片合并、部分视频合并需要 |
| aria2 | 可选，开启后视频下载更快（多连接） |

### 方式一：面板安装（推荐）

AstrBot 面板 → **插件** → **安装插件**，填入仓库地址：

```
https://github.com/zhiyu1998/astrbot_plugin_rconsole
```

安装完成后重载插件（或重启 AstrBot）。面板不支持从仓库安装时，用下面的方式二。

### 方式二：手动安装

1. 把插件放进 AstrBot 的 `data/plugins`：

```shell
cd /path/to/AstrBot
git clone https://github.com/zhiyu1998/astrbot_plugin_rconsole.git data/plugins/astrbot_plugin_rconsole
```

2. 安装依赖（在 **AstrBot 根目录** 执行）：

```shell
# AstrBot 默认用 uv
uv pip install -r data/plugins/astrbot_plugin_rconsole/requirements.txt
```

```shell
# 没有 uv 的话直接 pip
pip install httpx aiohttp aiofiles bilibili-api-python PyExecJS
```

3. 重启 AstrBot（或面板里重载插件）。

### 安装 ffmpeg（建议）

```shell
# Debian / Ubuntu
apt install -y ffmpeg

# CentOS / RHEL
yum install -y ffmpeg

# Windows
# 下载 https://www.gyan.dev/ffmpeg/builds/ 的 release 包，解压后把 bin 目录加进 PATH
```

### 安装 aria2（可选）

装好后把面板里的 `DOWNLOADS_USE_ARIA2` 打开，>5MB 的视频会交给 aria2c 多连接下载；没装也不影响（自动回退内置下载）。

```shell
# Debian / Ubuntu
apt install -y aria2

# CentOS / RHEL
yum install -y aria2
```

### 配置 Cookie

打开面板 → **插件** → **R插件**，按需填写（不填也能跑一部分平台，但成功率会低）：

| 配置项 | 是否必需 | 说明 |
| --- | --- | --- |
| `XHH_CK` | **必填** | 小黑盒没 Cookie 直接无法解析 |
| `XHS_CK` | **强烈建议** | 小红书网页版必须带 Cookie（需要 `a1` 和 `web_session`） |
| `BILI_SESSDATA` | **建议** | B 站 720P 以上画质必需，失效会退回 360P/480P |
| `DOUYIN_CK` | 可选 | 填了成功率和清晰度更高 |
| `WEIBO_CK` | 可选 | 提高接口成功率、能看评论 |

Cookie 获取：浏览器登录对应网站 → `F12` → **Network** → 随便点一个请求 → **Request Headers** → 复制整段 `Cookie` 值。

其余配置项（画质、时长上限、合并转发、下载并发等）见下方「配置项」。

### 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 小红书提示「页面里没有找到笔记数据」 | 要用 App / 网页「分享」出来的、带 `xsec_token` 的链接；手抄的裸链接 `…/explore/<id>` 无效 |
| B 站只能解析到 360P / 480P | `BILI_SESSDATA` 失效（日志会打「账号未登录 -101」），重新获取；4K/8K/HDR/杜比还需要大会员 |
| 视频提示 `ENOENT: no such file or directory` | NapCat 在容器 / 另一台机器上读不到宿主机路径。插件默认已改成 base64 发送；视频较多时建议在 AstrBot 配置里设置 `callback_api_base`，改走文件服务 |
| 小黑盒提示需要验证码 | 优先用 App 分享链接，并更新 `XHH_CK` |
| AcFun 解析失败 | 大概率没装 `ffmpeg`，见上面「安装 ffmpeg」 |
| 图片/视频发得很慢 | 调小 `DOWNLOAD_CONCURRENCY`（同时任务数）与 `DOWNLOAD_THREADS`（单文件分段数） |

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
| `DOWNLOAD_CONCURRENCY` | `2` | 同时下载任务数（队列长度），服务器弱可调 1 |
| `DOWNLOAD_THREADS` | `4` | 单文件 Range 分段并发数（>5MB 生效），0/1 = 不分段 |
| `TEMP_PATH` | 空 | 视频/图片暂存目录，留空 = `data/astrbot_plugin_rconsole/temp` |
| `DOWNLOADS_USE_ARIA2` | `false` | 用 aria2c 下载视频，失败自动回退内置下载 |
| `ARIA2C_PATH` | `aria2c` | aria2c 可执行文件路径（默认从 PATH 找） |
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
| 哔哩哔哩 | `BILI_BBDOWN_ARIA2C` | `false` | 给 BBDown 加 `--use-aria2c` |
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
