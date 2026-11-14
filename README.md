<p align="center">
  <a href="https://gitee.com/kyrzy0416/rconsole-plugin">
    <img width="200" src="./images/AstrR.png">
  </a>
</p>

<div align="center">
<h1>astrbot_plugin_rconsole</h1>
<p>
<a href="https://github.com/zhiyu1998/astrbot_plugin_rconsole/releases"><img src="https://img.shields.io/badge/version-v1.2.6-2f6f5e" alt="Version v1.2.6"></a>
<a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-%3E%3D3.10-3776ab" alt="Python 3.10+"></a>
<a href="https://astrbot.app/"><img src="https://img.shields.io/badge/AstrBot-plugin-4c78a8" alt="AstrBot Plugin"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-2f6f5e" alt="AGPL-3.0 License"></a>
</p>
专门为朋友们写的AstrBot插件（AstrBot_R插件的迁移版本），专注图片视频分享、生活、健康和学习的插件！

<img src="https://cdn.z.wiki/autoupload/20240819/Zn2g/github-contribution-grid-snake.svg">
</div>

## 安装部署

### 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | v4.5.0 及以上 |
| Python | 3.10+（用 AstrBot 自带的虚拟环境即可） |
| ffmpeg | 可选但建议安装，AcFun 分片合并、YouTube 音视频合并需要 |
| aria2 | 可选，开启后视频下载更快（多连接） |
| 代理 | 可选，YouTube 需要（`PROXY_URL` 填地址） |

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

其余配置项（画质、时长上限、合并转发、下载并发、代理等）都在面板里，装好后按需调整即可。

## 功能列表

| 平台 | 状态 | 说明 |
| --- | :---: | --- |
| 哔哩哔哩 | ✅ | 视频 / 番剧 / 专栏 / 动态 |
| 抖音 | ✅ | 短视频、图集；无 Cookie 时走分享页 SSR 兜底 |
| 快手 | ✅ | 原生页面解析，失败回退第三方接口 |
| 西瓜视频 / 皮皮虾 / 皮皮搞笑 / QQ小世界 / 即刻 | ⚠️ | 依赖第三方解析接口，可在面板里替换 |
| 微博 | ✅ | 正文、九宫格、视频、热门评论、投票贴图片 |
| AcFun | ✅ | m3u8 分片下载 + ffmpeg 合并 |
| 小红书 | ✅ | 图文 / 视频，正文超长自动折叠 |
| 米游社 | ✅ | 帖子图文 / 视频（带 DS 签名） |
| 小黑盒 | ✅ | 帖子（图文混排、游戏卡片、评论）+ PC / 主机 / 手机游戏详情 |
| 微视 | ✅ | 短视频 |
| YouTube | ✅ | yt-dlp 解析，需在配置里填代理地址 |
| 微信视频号 | ✅ | 走腾讯元宝接口，需填元宝 Cookie |
| 网易云 / 酷狗 / QQ音乐 点歌 | ❌ | 未迁移 |

> ✅ 可用　⚠️ 依赖外部接口　❌ 未迁移

插件还会把 **QQ 小程序 / 分享卡片**（OneBot `json` 消息段）解析成可读文本，所以从 QQ 直接分享的
B站、小红书、抖音等卡片也能识别——否则这类消息在 AstrBot 里只有一个 `[ComponentType.Json]`，
正则路由根本进不来。对应配置项 `CARD_PARSE_ENABLED`。

## 待完善...

## 致谢

本插件是 [rconsole-plugin](https://github.com/zhiyu1998/rconsole-plugin)（Yunzai 版 R 插件）的 AstrBot 迁移版本，
解析逻辑、配置项设计与文案均来自原项目，感谢原作者与**一起参与开发的团队**，没有他们的工作就没有这个插件。

同时也感谢这些开源项目：

- [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download) —— 微信视频号解析思路与接口参考
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— YouTube 解析
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) —— B 站解析
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) —— 插件运行框架

## 免责声明

本项目仅提供公开链接的技术解析与消息展示能力，不提供内容托管、批量抓取或访问控制绕过服务。使用者应遵守所在地法律法规、AstrBot 使用规范、目标平台服务条款及内容版权要求，并妥善保管 Cookie、账号和协议端配置。

解析结果来自第三方平台，本项目不保证其准确性、完整性或持续可用性。各平台名称和商标归其权利人所有；本项目与 AstrBot 官方及各内容平台均无隶属、授权或背书关系。

## 许可证

本项目采用 **GNU Affero General Public License v3.0（AGPL-3.0）** 发布，软件按许可证约定“按原样”提供且不附带担保。

AGPL-3.0 要求：基于本项目的修改版本、以及通过网络向用户提供服务的衍生作品，也需以同一许可证开源。
完整条款见 [LICENSE](LICENSE)。

