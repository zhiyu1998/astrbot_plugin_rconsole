<p align="center">
  <a href="https://gitee.com/kyrzy0416/rconsole-plugin">
    <img width="200" src="./images/AstrR.png">
  </a>
</p>

<div align="center">
    <h1>astrbot_plugin_rconsole</h1>
    专门为朋友们写的 AstrBot 插件（Yunzai 版 rconsole-plugin 的迁移版本），专注图片视频分享
</div>

## 当前状态

第一版迁移：**只做解析类功能**，主要逻辑来自 `rconsole-plugin/apps/tools.js`。

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| 哔哩哔哩 | ✅ | 基于 `bilibili-api-python`，视频/番剧/专栏/动态 |
| 抖音 | ✅ | 短链 + 完整链接 + 图集，免 Cookie 走分享页 SSR 兜底 |
| 快手 | ✅ | 原生网页解析优先，失败落第三方接口 |
| 微博 | ✅ | 正文/图片/视频/热门评论/投票贴图片 |
| AcFun | ✅ | m3u8 分片下载 + ffmpeg 合并 |
| 小红书 | ✅ | 图文/视频 |
| 米游社 | ✅ | 帖子正文/图片/视频（带 DS 签名） |
| 小黑盒 | ✅ | 帖子（图文混排、游戏卡片、评论）+ PC/主机/手机游戏详情 |
| 微视 | ✅ | 腾讯微视短视频 |
| 西瓜 / 皮皮虾 / 皮皮搞笑 / QQ小世界 / 即刻 | ⚠️ | 依赖第三方解析接口，可在配置里替换 |

未迁移：点歌（网易云 / 酷狗 / QQ音乐 / 汽水）、油管、X(Twitter)、Instagram、Telegram、贴吧、
最右、视频号、链接总结、扫码登录、直播录制。

## 部署

插件目录放到 AstrBot 的 `data/plugins/` 下，然后安装依赖：

```shell
# 在 AstrBot 根目录
uv pip install -r data/plugins/astrbot_plugin_rconsole/requirements.txt
# 或者
pip install httpx aiohttp aiofiles bilibili-api-python PyExecJS
```

抖音的 `a_bogus` 签名依赖 `PyExecJS` + 本机 JS 运行时（`node`）。
装不上也不影响主流程：抖音会走免 Cookie 的分享页 SSR 解析。

AcFun 合并分片需要系统里有 `ffmpeg`：

```shell
apt install -y ffmpeg   # Debian/Ubuntu
```

## 配置项

在 AstrBot 管理面板 → 插件配置里填写（v1.2.0 起全部按平台分组，前缀 `【全局】` `【哔哩哔哩】` …）：

| 配置项 | 说明 |
| --- | --- |
| `IDENTIFY_PREFIX` | 识别前缀，默认 `识别：` |
| `VIDEO_DURATION_MAXIMUM` | 全局视频时长上限（秒），默认 480 |
| `VIDEO_SIZE_LIMIT_MB` | 视频体积上限（MB），默认 30，超限只发文字+原链接 |
| `IMAGE_USE_FORWARD` | 多图改用合并转发（一张图一个节点） |
| `IMAGE_BATCH_THRESHOLD` | 每条转发最多几张图，0 = 全部塞一条 |
| `MSG_ELEMENT_LIMIT` | 单条转发消息元素上限（小黑盒图文混排），默认 50 |
| `DISABLED_PLATFORMS` | 禁用的平台，如 `douyin`、`weibo` |
| `GENERAL_PARSE_APIS` | 通用第三方解析接口，每行一个，`{}` 为链接占位 |
| `BILI_SESSDATA` | 哔哩哔哩 SESSDATA |
| `BILI_DURATION` | B站独立时长上限，0 = 沿用全局 |
| `BILI_DISPLAY_COVER` / `_INFO` / `_INTRO` / `_ONLINE` | B站信息展示开关（封面/统计/简介/在线人数） |
| `BILI_INTRO_LEN_LIMIT` | B站简介截断长度，默认 50，0 或 -1 = 不限制 |
| `DOUYIN_CK` | 抖音 Cookie（可选） |
| `DOUYIN_DURATION` | 抖音独立时长上限，0 = 沿用全局 |
| `DOUYIN_DISPLAY_COVER` | 抖音是否显示封面 |
| `DOUYIN_ENABLE_SSR` | 抖音是否启用分享页 SSR 兜底 |
| `WEIBO_CK` | 微博 Cookie |
| `WEIBO_COMMENTS` | 是否附带微博热门评论 |
| `XHS_CK` | 小红书 Cookie（需 a1 + web_session） |
| `XHS_DESC_LEN_LIMIT` | 小红书描述折叠长度，默认 78，0/-1 = 不折叠 |
| `XHH_CK` | 小黑盒 Cookie（必填） |
| `XHH_COMMENT_LIMIT` | 小黑盒评论转发条数上限，默认 50 |

## 目录结构

```
astrbot_plugin_rconsole/
├── main.py                 # 入口：正则路由到各平台 handler
├── _conf_schema.json       # 插件配置
├── constants/
│   ├── bili23.py
│   ├── douyin.py
│   └── tools.py            # 各平台接口常量
├── core/
│   ├── common.py           # 下载 / ffmpeg / 消息组装
│   ├── bili23.py           # B站
│   ├── douyin.py           # 抖音
│   ├── general.py          # 快手 + 通用第三方解析
│   ├── weibo.py            # 微博
│   ├── acfun.py            # AcFun
│   ├── xhs.py              # 小红书
│   ├── miyoushe.py         # 米游社
│   ├── xiaoheihe.py        # 小黑盒
│   └── weishi.py           # 微视
└── utils/
    └── xhh_sign.py         # 小黑盒 hkey 签名
```

## 已知问题（第一版）

- 抖音直播、评论、背景音乐未迁移
- 快手依赖页面结构，官方改版后需要更新解析；第三方接口不稳定
- B站沿用旧实现，只做了配置对接，没有重写下载策略
- AcFun 部分视频分片需要登录，未登录时可能拿不到 m3u8

## 在「家里云内网穿透」服务器上的验证记录

部署位置：`/home/AstrBot/data/plugins/astrbot_plugin_rconsole`
运行环境：Python 3.12.12 / AstrBot v4.29.4（PM2 进程 `astrbot`）
额外依赖：`bilibili-api-python`、`PyExecJS`（已装进 `/home/AstrBot/.venv`，`node v24` 可用）

验证结果（2026-11-13）：

| 项目 | 方式 | 结果 |
| --- | --- | --- |
| 插件加载 | `pm2 restart astrbot` + 日志 | ✅ 加载成功，9 个 handler 全部注册 |
| 路由正则 | 从 `star_handlers_registry` 取真实 RegexFilter 比对样本 | ✅ 9/9 命中 |
| AcFun | 真实链接 `ac4621380` | ✅ 解析 + 分片下载 + ffmpeg 合并 + 发送视频 |
| B站 | 真实 BV，bilibili_api 下载合并 | ✅ 封面/信息/视频 |
| 快手 | 真实短链与网页链接 | ⚠️ 原生解析时灵时不灵（快手已上反爬验证码），走第三方接口时用直链探活兜底 |
| 抖音 | 夹具（SSR 页面结构）+ 真实无效 ID | ✅ 渲染链路正确；真实视频需 Cookie 或有效链接 |
| 微博 | 夹具 | ✅ 正文/图片/视频/评论转发 |
| 米游社 | 夹具 | ✅ 封面/正文/图片/视频（DS 签名） |
| 微视 | 夹具 | ✅ 封面/标题/视频 |
| 小黑盒 | 夹具（帖子 + PC 游戏） | ✅ 图文混排/游戏卡片/评论/视频；`hkey` 签名与 JS 原始实现逐字节对齐 |
| 禁用平台开关 | `DISABLED_PLATFORMS=["acfun"]` | ✅ 命中后不再响应 |

测试脚本留在服务器 `/tmp`：`server_test.py`、`fixture_test.py`、`live_test.py`、`dispatch_test.py`。

## 2026-11-13 修复：B站视频发不出去 / 大视频下载超时

线上日志暴露了两个问题，已修：

1. **视频发送 ENOENT**
   NapCat 跑在 Docker 容器里，看不到宿主机的 `/home/AstrBot/...`，而 AstrBot 的 aiocqhttp 适配器对
   `Video` 是原样透传 `file://`（`Image`/`Record` 才会转 base64）。所以以前 B站视频下载合并成功、
   发出去却报 `ENOENT: no such file or directory`。

   现在的策略：
   - 全局配了 `callback_api_base` → 用 AstrBot 的文件服务，本地路径会被注册成 URL（大文件首选，不占内存）
   - 没配 → 插件自己把视频读成 `base64://` 发送（NapCat 的 `$Q` 解析器支持 Base64 类型，会落盘再上传）
   - 超过 `VIDEO_SIZE_LIMIT_MB`（默认 30）→ 只发文字 + 原链接

2. **10 分钟的视频下载 ReadTimeout**
   `download_b_file` 用的是 httpx 默认 5 秒超时，长视频必然断流。现在给了 connect 20s / read 600s，
   并加了 3 次重试 + 长度校验（CDN 提前断流会重下）。另外顺手修了 `content-length` 缺失时的除零。

3. 顺带的行为调整：**超过 `VIDEO_DURATION_MAXIMUM` 的视频不再下载**（以前是先提示超限、再老老实实下完），
   合并产物和空目录发送后即清理，`data/bilibili_cache` 不会越攒越大。

## 2026-11-13 修复：抖音「只有视频没有正文」

日志里那条链：

```
chain = MessageChain([Image(file='https://p3-pc-sign.douyinpic.com/...'),
                      Plain('\n识别：抖音\n👤作者：…\n📝简介：…')])
error = Failed to download file from https URL host='p3-pc-sign.douyinpic.com' … HTTP 403
```

抖音 CDN 校验 Referer，而 `Comp.Image.fromURL()` 是让 AstrBot 自己去下（不带 Referer）→ 403；
**一张封面失败会把整条消息链丢掉**，夹在后面的正文也就没发出去，只剩先发出去的视频。

改法：

- 新增 `common.local_image()`：封面/图片先带 Referer 下到本地，再 `Image.fromFileSystem` 发
- 新增 `common.cover_text_result()`：封面挂了就只发正文，绝不让一张图带走整条消息
- 抖音、B站、米游社、微视、快手/通用、小黑盒（缩略图/正文图/游戏卡片/截图/评论图）全部改走本地图

顺带把抖音详情来源也加固了（这次排查时发现的第二个坑）：

- 分享页 SSR 改成**移动端 UA + `?region=CN&mid=0`**，并对每个来源做「有没有真直链」校验
  （裸链接返回的是「请打开 App」空壳，以前会当成解析成功）
- 第三方接口字段映射修正：`item.url`/`item.cover`/`item.title` 才是正文和直链，
  `jx.url` 是作品页地址（以前会把 `https://www.douyin.com/video/xxx` 当视频下载，存下来是个 HTML）
- 时长兼容毫秒/秒
- `a_bogus` 生成失败降级为 warning（不影响 SSR/第三方链路）

实测同一个链接：`Image(base64 封面 10.8KB) + 正文(作者/简介齐全)`、`Video(11.8MB base64)`，正文与视频都在。

## 2026-11-13 修复：多图没有走合并转发

之前图集只会「按阈值拆成多条普通消息」，代码里根本没实现转发；阈值填 0 时 `chunked(..., 0)`
被当成"一整条"，所以还是一堆普通图片。

现在按 R 插件的 `sendImagesInBatches` 语义重做：

| 配置 | 行为 |
| --- | --- |
| `IMAGE_USE_FORWARD`（默认开） | 多图走合并转发，**一张图一个节点**，不刷屏 |
| `IMAGE_BATCH_THRESHOLD` = 0 | 全部塞进一条转发 |
| `IMAGE_BATCH_THRESHOLD` = N | 每条转发最多 N 张，超过拆成多条转发 |
| `IMAGE_USE_FORWARD` 关闭 | 直接按条发图（阈值仍生效） |

覆盖抖音图集、微博九宫格、米游社图文、快手/通用解析。

另外修了个时序隐患：图片原来是发完立刻 `delete_file`，而转发消息是在**真正发送时**才把节点里的图
转成 base64（`Node.to_dict()`），删早了会 `FileNotFoundError`。现在统一走 `release_file()`
延后 180 秒删除。

实测（用户给的 4 图图集）：`batch=0` → 1 条转发 / 4 节点；`batch=3` → 2 条转发（3+1）；
关闭转发 → 4 张图一条消息。

## 2026-11-13 修复：小红书 `Expecting value: line 1 column xxxxx`

三个独立问题叠加：

1. **`__INITIAL_STATE__` 不是严格 JSON**。小红书往里塞了 JS 字面量：
   `"AiNoteDetailStore":{"noteDetailMap":new Map([])}`，`json.loads` 直接炸。
   新增 `normalize_xhs_state_text()`：`undefined`→`null`、`new Map(...)`→`{}`、
   `new Set(...)`→`[]`、`new Date(...)`→`null`（嵌套的替换多轮），再用
   `JSONDecoder().raw_decode()` 解析（顺带忽略尾部 JS）。
2. **没有跟随跳转**。`httpx.AsyncClient()` 默认 `follow_redirects=False`，小红书 302 到规范化地址时
   拿到的只是一个空跳转页 → 解析不到 state。已改成 `follow_redirects=True`，UA 也换成新版 Chrome。
3. **字段位置变了**。新版把 `title/desc/user/interactInfo/imageList` 放在 note 顶层，
   旧代码只读 `shareInfo`，于是标题永远「无标题」、作者永远「未知作者」。
   `extract_note_info()` 重写为顶层优先 + shareInfo 兜底，标题为空时用正文首行顶替，
   图片地址兼容 `urlDefault` / `infoList` / `url`。

另外把小红书的发送接进统一链路：图片走合并转发（受 `IMAGE_USE_FORWARD` 控制）、
视频走 base64、文件延后清理。

实测两条真实笔记：标题/描述/作者/位置/点赞评论收藏/发布时间全部正确，图片以 2~3 节点的合并转发出图。