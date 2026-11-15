"""
AI 总结（迁移自 R 插件 apps/tools.js 的 linkShareSummary）

原版做法：
    1. 用正则判断链接属于哪个平台（微信文章 / ArXiv / 少数派 / B站专栏 / 知乎问题 /
       ChinaDaily / GitHub / V2EX），或者用户直接发 `#总结一下 <链接>`
    2. `llmRead()` 把网页抓下来 → 去掉脚本样式 → 压成纯文本（最多 5 万字符）
    3. 把正文塞进 prompt，交给大模型按 SUMMARY_PROMPT 输出「标题 + 摘要 + 关键段落」
    4. 先回一条「《标题》 预计阅读时间 x 分钟，总字数 y」，再用合并转发发总结

本版差异（用户要求）：
    * 不再走元宝接口、也不再用插件自己的 OpenAI Key，
      直接用 AstrBot 的 provider（默认取当前会话正在用的模型，也可以指定 provider id）
    * 输出与 Node 版保持一致：识别前缀、统计行、合并转发两节点
"""

import inspect
import math
import re
from typing import Any, AsyncGenerator, List, Optional, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .common import get_message_text, send_forward_message, _httpx_client, proxy_for

# ---------------------------------------------------------------- 识别规则

# 顺序与 Node 版 SUMMARY_CONTENT_ESTIMATOR_PATTERNS 完全一致
SUMMARY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("微信文章", re.compile("(?:https?://)?mp\\.weixin\\.qq\\.com/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("ArXiv论文", re.compile("(?:https?://)?arxiv\\.org/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("少数派", re.compile("(?:https?://)?sspai\\.com/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("哔哩哔哩专栏", re.compile("(?:https?://)?www\\.bilibili\\.com/read/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("知乎问题", re.compile("(?:https?://)?www\\.zhihu\\.com/question/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("ChinaDaily", re.compile("(?:https?://)?(?:www\\.)?chinadaily\\.com\\.cn/a/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("Github", re.compile("(?:https?://)?(?:www\\.)?github\\.com/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("V2EX", re.compile("(?:https?://)?(?:www\\.)?v2ex\\.com/t/[^\\s\"'<>，。；：！？、（）【】》»“”‘’]*")),
    ("网页", re.compile("https?://[^\\s\"'<>，。；：！？、（）【】》»“”‘’]+")),
]

SUMMARY_COMMAND = "#总结一下"

SUMMARY_PROMPT = """# Role: Web Content Summarization Assistant

## Profile
- author: R-plugin
- version: 1.0
- language: 中文
- description: 一个专门用于网页内容摘要的AI助手，能够提取关键点，总结文章，并提供复杂主题的简洁概览。

## Skills
1. 精通自然语言理解和摘要技术。
2. 能够从各种网页内容格式（文章、博客、报告）中提取关键信息。
3. 能够总结短篇和长篇内容。
4. 适应不同的写作风格和语调。

## Rules
1. 确保摘要捕捉到网页的主要思想和关键点。
2. 摘要应简洁、清晰、准确，避免不必要的细节。
3. 根据原始内容的复杂性和长度调整摘要的长度和细节。
4. 保持原始内容的上下文和意图，不添加个人解释。

## Workflows
1. 分析网页的结构和主要部分。
2. 识别并提取内容中的关键点、论点或信息。
3. 首先，生成一个标题，格式约束为："标题: {title}"。
4. 然后，生成一个包含最重要细节的简洁摘要。
5. 接下来，生成一个"关键段落"标题，内容包括你认为文章中一些关键信息点，每个关键点前都带有一个表情符号。
6. 接下来，审查摘要的准确性和完整性。不要在摘要审查中包含当前网页的URL。
7. 最后，输出内容保证为中文。

## Init
在第一次对话中，请直接输出以下：您好！我将联合R插件为您提供简洁明了的网页内容。"""

DEFAULT_SUMMARY_PROMPT = SUMMARY_PROMPT
DEFAULT_USER_PROMPT_TEMPLATE = (
    "这是网页链接: {url}\n\n网页正文如下：\n{content}\n\n请根据以上内容进行总结。"
)

DEFAULT_MAX_CHARS = 30000
COMMON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def detect_summary_target(text: str) -> Optional[Tuple[str, str]]:
    """
    从消息里认出「要总结什么」。

    返回 (平台名, 链接)；认不出来返回 None。
    `#总结一下 <链接>` 的优先级最高，不要求命中平台正则。
    """
    text = (text or "").strip()
    if not text:
        return None

    if text.startswith(SUMMARY_COMMAND):
        rest = text[len(SUMMARY_COMMAND):].strip()
        match = re.search(r"https?://[^\s，。；：！？、（）【】》»\"“”'`]+", rest)
        if not match:
            return None
        url = match.group(0)
        # 命令只是「强制总结」，平台名还是按链接认一下，别一律叫「网页」
        for name, pattern in SUMMARY_PATTERNS:
            if name == "网页":
                continue
            if pattern.search(url):
                return name, url
        return "网页", url

    for name, pattern in SUMMARY_PATTERNS:
        match = pattern.search(text)
        if match:
            url = match.group(0)
            if not url.lower().startswith("http"):
                url = "https://" + url.lstrip("/")
            return name, url
    return None


# ---------------------------------------------------------------- 网页正文

def _decode_entities(text: str) -> str:
    replacements = [
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&lt;", "<"),
        ("&gt;", ">"),
    ]
    for src, dst in replacements:
        text = re.sub(re.escape(src), dst, text, flags=re.I)
    return text


_WX_NOISE = (
    "在小说阅读器读本章",
    "在公众号小说中沉浸阅读",
    "去阅读",
    "预览时标签不可点",
    "微信扫一扫关注该公众号",
    "继续滑动看下一个",
    "向上滑动看下一个",
    "长按识别二维码关注",
)

_TITLE_PATTERNS = (
    r"<meta[^>]+property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)[\"']",
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*property=[\"']og:title[\"']",
    r"var\s+msg_title\s*=\s*[\"']([^\"']+)[\"']",
    r"<title[^>]*>([\s\S]*?)</title>",
)


def _title_from_html(raw: str) -> str:
    """按 og:title → 微信 msg_title → <title> 的顺序取标题（微信 <title> 常为空）"""
    if not raw:
        return ""
    for pattern in _TITLE_PATTERNS:
        match = re.search(pattern, raw, flags=re.I)
        if match:
            text = re.sub(r"\s+", " ", _decode_entities(match.group(1))).strip()
            if text:
                return text
    return ""


def strip_html_to_text(raw: str) -> str:
    """复刻 Node 版 llmRead 的清理流程：保留标题，丢掉脚本样式，压成一行一段"""
    if not raw:
        return ""
    title = _title_from_html(raw)

    cleaned = raw
    for tag in ("script", "style", "noscript", "template", "svg", "iframe"):
        cleaned = re.sub(rf"<{tag}[\s\S]*?</{tag}>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for noise in _WX_NOISE:
        cleaned = cleaned.replace(noise, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    text = _decode_entities(cleaned)
    if not title:
        return text
    return f"Title: {title}\n\n{text}"


def page_title(raw: str) -> str:
    return _title_from_html(raw)


async def fetch_page_text(
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: float = 30.0,
    proxy: Optional[str] = None,
) -> Tuple[str, str]:
    """
    抓网页 → (纯文本, 页面标题)。失败抛异常，由上层转成用户可读提示。
    """
    if not re.match(r"^https?://", url, flags=re.I):
        raise ValueError("链接格式无效，无法抓取网页内容")

    headers = {
        "User-Agent": COMMON_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    async with _httpx_client(timeout, proxy) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"网页返回状态码 {resp.status_code}")
    raw = resp.text
    content = strip_html_to_text(raw)
    if not content:
        raise RuntimeError("网页正文为空或提取失败")
    return content[:max_chars], page_title(raw)


# ---------------------------------------------------------------- 文本统计

def count_chinese_chars(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fa5]", text or ""))


def estimate_reading_time(text: str, wpm: int = 200) -> Tuple[int, int]:
    """与 Node 版 estimateReadingTime 一致：只数汉字，按 200 字/分钟向上取整"""
    words = count_chinese_chars(text)
    return max(1, math.ceil(words / wpm)), words


def build_user_prompt(url: str, content: str, template: str = "") -> str:
    """按用户模板拼 user 提示词；占位符写错或缺 content 时自动兜底/补正文"""
    tpl = (template or "").strip() or DEFAULT_USER_PROMPT_TEMPLATE
    data = {"url": url, "content": content}
    try:
        out = tpl.format(**data)
    except (KeyError, IndexError, ValueError):
        logger.warning("[R插件][AI总结] 用户提示词模板占位符有误，已回退默认模板")
        out = DEFAULT_USER_PROMPT_TEMPLATE.format(**data)
    if content and content not in out:
        out = out.rstrip() + "\n\n" + content
    return out


def extract_title(summary: str, fallback: str = "") -> str:
    match = re.search(r"(?:Title|标题)[:：]\s*(.*?)\n", summary or "")
    if not match:
        match = re.search(r"(?:Title|标题)[:：]\s*(.*)", summary or "")
    title = (match.group(1).strip() if match else "") or fallback
    return title or "未知标题"


# ---------------------------------------------------------------- provider

async def resolve_provider(context, event: AstrMessageEvent, preferred: str = "") -> Tuple[str, str]:
    """
    解析要用的 chat provider → (provider_id, 展示名)。

    优先级：配置里指定的 provider id → 当前会话正在用的 → 全局默认。
    """
    if preferred:
        try:
            prov = context.get_provider_by_id(provider_id=preferred)
            if prov:
                return preferred, _provider_name(prov, preferred)
            logger.warning(f"[R插件][AI总结] 配置的 provider 不存在：{preferred}，改用默认模型")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][AI总结] 读取 provider {preferred} 失败：{exc}")

    try:
        umo = event.unified_msg_origin
        pid = context.get_current_chat_provider_id(umo=umo)
        if inspect.isawaitable(pid):
            pid = await pid
        pid = str(pid or "")
        if pid:
            prov = context.get_provider_by_id(provider_id=pid)
            return pid, _provider_name(prov, pid)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][AI总结] 获取会话 provider 失败：{exc}")

    try:
        prov = await context.get_using_provider_async()
        if prov is not None:
            meta = prov.meta() if hasattr(prov, "meta") else None
            pid = getattr(meta, "id", "") or ""
            return pid, _provider_name(prov, pid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][AI总结] 获取默认 provider 失败：{exc}")

    return "", ""


def _provider_name(provider, fallback: str = "") -> str:
    if provider is None:
        return fallback or "AstrBot"
    try:
        meta = provider.meta()
        model = getattr(meta, "model", "") or ""
        if model:
            return model
        return getattr(meta, "id", "") or fallback or "AstrBot"
    except Exception:  # noqa: BLE001
        return fallback or "AstrBot"


# ---------------------------------------------------------------- 主流程

async def process_summary_url(
    context,
    event: AstrMessageEvent,
    *,
    identify_prefix: str = "识别：",
    provider_id: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout: float = 120.0,
    use_forward: bool = True,
    text: str = "",
    system_prompt: str = "",
    user_prompt: str = "",
) -> AsyncGenerator[Any, None]:
    """
    识别并总结一条链接消息。识别不出来直接 return（不影响其它解析器）。

    Yields: 提示语 → 统计行 → 总结正文（合并转发或纯文本）
    """
    target = detect_summary_target(text or get_message_text(event))
    if not target:
        return
    name, url = target

    if identify_prefix is None:
        identify_prefix = ""

    pid, model = await resolve_provider(context, event, provider_id)
    if not pid and not model:
        yield event.plain_result(
            f"{identify_prefix}{name}（AI 总结）失败：没有可用的大模型。\n"
            "请在 AstrBot 面板「服务提供商」里配置一个对话模型，或在插件设置里指定 provider。"
        )
        return

    yield event.plain_result(f"{identify_prefix}{name}（AI 总结），正在抓取网页并总结，请稍等…")

    try:
        content, html_title = await fetch_page_text(
            url, max_chars=max_chars, timeout=min(60.0, timeout), proxy=proxy_for("summary")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][AI总结] 抓取失败 {url}: {exc}")
        yield event.plain_result(f"{identify_prefix}{name}（AI 总结）失败：{exc}\n{url}")
        return

    prompt = build_user_prompt(url, content, user_prompt)
    system = (system_prompt or "").strip() or DEFAULT_SUMMARY_PROMPT
    try:
        logger.info(f"[R插件][AI总结] 使用模型 {model or pid} 总结 {url}（正文 {len(content)} 字符）")
        resp = await context.llm_generate(
            chat_provider_id=pid,
            prompt=prompt,
            system_prompt=system,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件][AI总结] 模型调用失败：{exc}")
        yield event.plain_result(f"{identify_prefix}{name}（AI 总结）失败：模型调用出错（{exc}）")
        return

    summary = (getattr(resp, "completion_text", "") or "").strip()
    if not summary:
        yield event.plain_result(f"{identify_prefix}{name}（AI 总结）失败：模型没有返回内容，请稍后重试")
        return

    minutes, words = estimate_reading_time(summary)
    title = extract_title(summary, html_title)
    yield event.plain_result(f"《{title}》 预计阅读时间: {minutes} 分钟，总字数: {words}")

    header = f"「R插件 x {model}（AstrBot 默认模型）」联合为您总结内容："
    if not use_forward:
        yield event.plain_result(f"{header}\n\n{summary}")
        return

    try:
        yield await send_forward_message(event, [[Comp.Plain(header)], [Comp.Plain(summary)]])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件][AI总结] 合并转发失败，改为纯文本发送：{exc}")
        yield event.plain_result(f"{header}\n\n{summary}")
