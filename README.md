<p align="center">
  <a href="https://gitee.com/kyrzy0416/rconsole-plugin">
    <img width="200" src="./images/AstrR.png">
  </a>
</p>

<div align="center">
    <h1>astrbot_plugin_rconsole</h1>
    专门为朋友们写的AstrBot插件，专注图片视频分享、生活、健康和学习的插件！
    <img src="https://cdn.z.wiki/autoupload/20240819/Zn2g/github-contribution-grid-snake.svg">
</div>

> AstrBot_R插件的迁移版本

专门为朋友们写的AstrBot插件，专注图片视频分享、生活、健康和学习的插件！

## 手动部署

1. 在 AstrBot 的根目录安装以下依赖
```
uv add bilibili-api-python PyExecJS httpx aiohttp
```

2. 将 `astrbot_plugin_rconsole` 放入根目录下 `data/plugins`，例如：

> /home/AstrBot/data/plugins

3. 运行，填入相关 Cookie 即可进行解析

## 功能列表

- [x] 哔哩哔哩
- [x] Douyin
- [x] XHS (小红书)
- [ ] 油管

## 配置项

在 AstrBot 配置中设置以下项：

- `BILI_SESSDATA`: 哔哩哔哩的 SESSDATA Cookie
- `DOUYIN_CK`: 抖音的 Cookie
- `XHS_CK`: 小红书的 Cookie
- `VIDEO_DURATION_MAXIMUM`: 视频时长上限，单位为秒，默认 480 秒

## 待完善...