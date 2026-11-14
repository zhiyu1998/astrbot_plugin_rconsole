"""QQ 小程序 / 结构化分享卡片的解析与注入。

问题（AstrBot 4.29 + NapCat）：群里发小程序卡片，日志只看到 `[ComponentType.Json]`，
卡片里的标题、描述、跳转链接全部丢失。

根因：
1. NapCat 把卡片以 OneBot `json` 消息段发来，适配器兜底分支把它包成 `Json` 组件，
   但**不会**往 `message_str` 追加任何文本（只有 text 分支才会）；
2. 于是 `event.message_str` 为空：
   - 正则过滤器（本插件的平台路由）匹配不到，handler 根本不进；
   - `get_message_outline()` 渲染成 `[ComponentType.Json]`，日志/历史都看不到内容；
   - 只发卡片不 @ 机器人时，唤醒判断也过不去。

修复：在本进程内给 `AiocqhttpAdapter._convert_handle_message_event` 挂一层后处理，
把 `Json` 组件解析成文本追加进 `message_str`；同时把 `get_message_outline` 的占位符
替换成解析结果。不改 AstrBot 源码，插件卸载时还原。

（逻辑来自 astrbot_plugin_card_parser，现并入本插件）
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from astrbot.api import logger

MAX_TEXT_LEN = 600

# meta 下常见的卡片主体字段，按优先级排列
_META_ORDER = (
    "detail_1",
    "detail",
    "news",
    "music",
    "musicDetail",
    "video",
    "article",
    "summary",
    "collection",
    "qqmusic",
)

# 小程序 appid → 可读名
_APPID_NAMES = {
    "1109937557": "哔哩哔哩",
    "1112384716": "小红书",
    "1108961705": "AcFun",
    "1109503475": "抖音",
    "1110065209": "快手",
    "1112112732": "微博",
    "1108873254": "知乎",
    "1111819757": "网易云音乐",
    "1109950533": "米游社",
    "1108914571": "小黑盒",
}

_JSON_FENCE = re.compile(r"^\s*json\s*", re.IGNORECASE)
# 卡片里能直接拿来解析的链接字段
_URL_KEYS = ("qqdocurl", "jumpUrl", "jump_url", "url")

_patched = False


def _as_dict(raw: Any) -> Dict[str, Any]:
    """把 json 段里的 data 归一化成 dict（dict / JSON 字符串 / {"data": "<json>"} 都兼容）"""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, (bytes, bytearray)):
        data = _as_dict(raw.decode("utf-8", "ignore"))
    elif isinstance(raw, str):
        text = _JSON_FENCE.sub("", raw).strip()
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            return {"_raw": text}
    else:
        return {}

    inner = data.get("data")
    if isinstance(inner, str) and inner.strip().startswith("{"):
        try:
            parsed = json.loads(_JSON_FENCE.sub("", inner).strip())
            if isinstance(parsed, dict):
                merged = dict(parsed)
                for key, value in data.items():
                    if key != "data":
                        merged.setdefault(key, value)
                return merged
        except Exception:  # noqa: BLE001
            pass
    return data


def _pick(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _walk_strings(node: Any) -> List[str]:
    out: List[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_walk_strings(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk_strings(value))
    return out


def parse_card(raw: Any) -> str:
    """把一张卡片解析成可读单行文本；解析不出内容返回空串"""
    data = _as_dict(raw)
    if not data:
        return ""

    if "_raw" in data:
        text = str(data["_raw"]).strip()
        return f"[卡片] {text[:MAX_TEXT_LEN]}" if text else ""

    app = _pick(data.get("app"))
    prompt = _pick(data.get("prompt"))
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}

    candidates: List[Dict[str, Any]] = []
    for key in _META_ORDER:
        node = meta.get(key)
        if isinstance(node, dict):
            candidates.append(node)
    for node in meta.values():
        if isinstance(node, dict) and node not in candidates:
            candidates.append(node)

    title = desc = url = tag = appid = ""
    for node in candidates:
        title = title or _pick(node.get("title")) or _pick(node.get("nickname"))
        desc = desc or _pick(node.get("desc")) or _pick(node.get("abstract")) or _pick(node.get("content"))
        for key in _URL_KEYS:
            url = url or _pick(node.get(key))
        tag = tag or _pick(node.get("tag"))
        appid = appid or _pick(node.get("appid")) or _pick(node.get("app_id"))
        if title and (desc or url):
            break

    appid = appid or _pick(extra.get("appid")) or _pick(extra.get("app_id"))
    if not title and not desc and not url and not prompt:
        loose = [
            value.strip()
            for value in _walk_strings(data)
            if 2 <= len(value.strip()) <= 120 and "://" not in value
        ]
        if not loose:
            return f"[卡片] app={app or 'unknown'}"
        return f"[卡片] {' | '.join(dict.fromkeys(loose))[:MAX_TEXT_LEN]}"

    if appid and appid in _APPID_NAMES:
        title = title or _APPID_NAMES[appid]
        tag = tag or _APPID_NAMES[appid]

    head = prompt or title or "卡片"
    if head.startswith("[") and head.endswith("]") and title:
        head = f"{head}{title}"

    parts = [f"[小程序/卡片] {head}"]
    if title and title not in head:
        parts.append(f"标题: {title}")
    if tag and tag not in head:
        parts.append(f"来源: {tag}")
    if desc and desc not in head:
        parts.append(f"简介: {desc}")
    if url:
        parts.append(f"链接: {url}")
    if appid:
        parts.append(f"appid: {appid}")
    return " ".join(parts)[:MAX_TEXT_LEN]


def extract_card_url(raw: Any) -> str:
    """只抽卡片里的链接，给解析链路用"""
    data = _as_dict(raw)
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for node in meta.values():
        if not isinstance(node, dict):
            continue
        for key in _URL_KEYS:
            url = _pick(node.get(key))
            if url.startswith("http"):
                return url
    for value in _walk_strings(data):
        text = value.strip()
        if text.startswith("http://") or text.startswith("https://"):
            return text
    return ""


def enrich_message_str(abm: Any) -> bool:
    """把 AstrBotMessage 里所有 Json 组件的文本并进 message_str"""
    try:
        from astrbot.core.message.components import Json
    except Exception:  # noqa: BLE001
        return False

    chain = getattr(abm, "message", None)
    if not isinstance(chain, list):
        return False

    texts: List[str] = []
    for comp in chain:
        if not isinstance(comp, Json):
            continue
        try:
            text = parse_card(getattr(comp, "data", None))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件] 解析卡片失败: {exc}")
            continue
        if text:
            texts.append(text)

    if not texts:
        return False

    extra = " ".join(dict.fromkeys(texts))
    base = (getattr(abm, "message_str", "") or "").strip()
    abm.message_str = f"{base} {extra}".strip() if base else extra
    return True


def _patch_outline() -> bool:
    """让日志/会话历史里的 `[ComponentType.Json]` 也显示卡片内容"""
    try:
        from astrbot.core.message.components import Json
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
    except Exception:  # noqa: BLE001
        return False

    original = AstrMessageEvent.get_message_outline
    if getattr(original, "__rconsole_patched__", False):
        return True

    def _patched(self):
        try:
            text = original(self)
        except Exception:  # noqa: BLE001
            return ""
        chain = getattr(getattr(self, "message_obj", None), "message", None) or []
        cards = []
        for comp in chain:
            if isinstance(comp, Json):
                try:
                    parsed = parse_card(getattr(comp, "data", None))
                except Exception:  # noqa: BLE001
                    parsed = ""
                if parsed:
                    cards.append(parsed)
        for card in cards:
            for placeholder in ("[ComponentType.Json]", "[Json]"):
                if placeholder in text:
                    text = text.replace(placeholder, card, 1)
                    break
        return text

    _patched.__rconsole_patched__ = True  # type: ignore[attr-defined]
    _patched.__wrapped__ = original  # type: ignore[attr-defined]
    AstrMessageEvent.get_message_outline = _patched
    return True


def install() -> Dict[str, bool]:
    """挂载卡片解析补丁，返回各部件是否成功"""
    global _patched
    result = {"adapter": False, "outline": False}
    if _patched:
        return {"adapter": True, "outline": True}

    try:
        from astrbot.core.platform.sources.aiocqhttp import (
            aiocqhttp_platform_adapter as adapter_mod,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] 卡片解析：无法导入 aiocqhttp 适配器: {exc}")
        return result

    original = adapter_mod.AiocqhttpAdapter._convert_handle_message_event
    if getattr(original, "__rconsole_patched__", False):
        _patched = True
        result["adapter"] = True
    else:
        async def _patched_convert(self, event, get_reply=True):  # noqa: ANN001
            abm = await original(self, event, get_reply=get_reply)
            try:
                if enrich_message_str(abm):
                    logger.debug(f"[R插件] 卡片已解析: {(getattr(abm, 'message_str', '') or '')[:160]}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 卡片 message_str 后处理失败: {exc}")
            return abm

        _patched_convert.__rconsole_patched__ = True  # type: ignore[attr-defined]
        _patched_convert.__wrapped__ = original  # type: ignore[attr-defined]
        adapter_mod.AiocqhttpAdapter._convert_handle_message_event = _patched_convert
        _patched = True
        result["adapter"] = True

    result["outline"] = _patch_outline()
    return result


def uninstall() -> None:
    """插件卸载时还原补丁"""
    global _patched
    if not _patched:
        return
    try:
        from astrbot.core.platform.sources.aiocqhttp import (
            aiocqhttp_platform_adapter as adapter_mod,
        )

        current = adapter_mod.AiocqhttpAdapter._convert_handle_message_event
        original = getattr(current, "__wrapped__", None)
        if original is not None:
            adapter_mod.AiocqhttpAdapter._convert_handle_message_event = original

        from astrbot.core.platform.astr_message_event import AstrMessageEvent

        current_outline = AstrMessageEvent.get_message_outline
        original_outline = getattr(current_outline, "__wrapped__", None)
        if original_outline is not None:
            AstrMessageEvent.get_message_outline = original_outline
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 还原卡片解析补丁失败: {exc}")
    finally:
        _patched = False