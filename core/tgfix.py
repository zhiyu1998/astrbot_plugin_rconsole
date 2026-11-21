"""Telegram 发送兜底补丁：文字先行 + 组件级容错。

问题（AstrBot 4.29 + python-telegram-bot）：
`TelegramPlatformEvent.send_with_client` 是把一条消息链**按顺序逐个组件**发出去的，
而且每个组件都没有 try/except。插件普遍把结果拼成 `[封面图, 文本]` 这样一条链，
于是：

1. 封面图 `send_photo` 上传走 `https://api.telegram.org`，python-telegram-bot 的
   默认 `HTTPXRequest` 读超时只有 5s，经代理/网络抖动时抛 `telegram.error.TimedOut`；
2. 该异常直接冒泡中断整条链 —— 后面的 `Plain` 文本永远不会发；
3. 用户侧现象：只收到图片和视频，文案整段消失（日志里能看到 traceback）。

修复方式（不改 AstrBot 源码，插件卸载还原）：
1. 一条链里同时有「非空文本」和「媒体」时，先单独把文本发出去（文本最贵，先保它）；
2. 其余媒体逐个发送，单组件失败重试（图片 1 次、视频 2 次），仍然失败只 warning，
   不影响后续组件；
3. 文本发送本身失败时用 `client.send_message` 直接兜底，按 4000 字符切片；
4. 链里带 At / Reply / ActionRow 这类不支持拆分语义的组件时，完全回落原实现。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

RETRY_DELAY = 1.5
TEXT_CHUNK = 4000

# python-telegram-bot 默认 httpx 超时全部 5s，上传封面/视频经常不够；
# 这里在首次发送时把 client 的超时放宽（幂等，只做一次）
_CONNECT_TIMEOUT = 15.0
_READ_TIMEOUT = 60.0
_MEDIA_WRITE_TIMEOUT = 120.0
_POOL_TIMEOUT = 15.0

_relaxed = False


def _relax_timeouts(client: Any) -> None:
    """放宽 telegram client 的上传超时，失败不影响发送"""
    global _relaxed
    if _relaxed:
        return
    _relaxed = True
    try:
        requests = getattr(client, "_request", None) or []
        if not isinstance(requests, (list, tuple)):
            requests = [requests]
        touched = []
        for req in requests:
            http = getattr(req, "_client", None)
            if http is not None and hasattr(http, "timeout"):
                try:
                    import httpx

                    http.timeout = httpx.Timeout(
                        connect=_CONNECT_TIMEOUT,
                        read=_READ_TIMEOUT,
                        write=_READ_TIMEOUT,
                        pool=_POOL_TIMEOUT,
                    )
                    touched.append("httpx")
                except Exception:  # noqa: BLE001
                    pass
            if hasattr(req, "_media_write_timeout"):
                try:
                    req._media_write_timeout = _MEDIA_WRITE_TIMEOUT  # noqa: SLF001
                    touched.append("media_write")
                except Exception:  # noqa: BLE001
                    pass
        if touched:
            logger.info(
                f"[R插件][Telegram] 已放宽上传超时（read={_READ_TIMEOUT}s, "
                f"media={_MEDIA_WRITE_TIMEOUT}s）"
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[R插件][Telegram] 放宽超时失败（忽略）：{exc}")


_installed = False
_orig_func = None


def _classes() -> Tuple[Any, Any, Tuple[type, ...]]:
    """返回 (message_components 模块, Plain 类, 不可拆分的组件元组)"""
    from astrbot.api import message_components as mc

    plain = mc.Plain
    risky = tuple(
        cls
        for cls in (
            getattr(mc, "At", None),
            getattr(mc, "AtAll", None),
            getattr(mc, "Reply", None),
            getattr(mc, "ActionRow", None),
            getattr(mc, "Buttons", None),
        )
        if isinstance(cls, type)
    )
    return mc, plain, risky


def _text_of(component: Any) -> str:
    return str(getattr(component, "text", "") or "")


async def _send_chain(
    cls: Any,
    client: Any,
    message: Any,
    user_name: Any,
    *,
    retries: int = 1,
    tag: str = "组件",
) -> bool:
    """发送一条子链，失败重试，返回是否成功（异常不外抛）"""
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            await _orig_func(cls, client, message, user_name)
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                logger.debug(
                    f"[R插件][Telegram] {tag} 第 {attempt + 1} 次发送失败，"
                    f"{RETRY_DELAY}s 后重试：{type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(RETRY_DELAY)
    logger.warning(
        f"[R插件][Telegram] {tag} 发送失败（已重试 {retries} 次，跳过该组件）："
        f"{type(last).__name__}: {last}"
    )
    return False


async def _fallback_text(client: Any, user_name: Any, body: str) -> None:
    """文本链发送失败时直接调 send_message 兜底"""
    body = (body or "").strip()
    if not body:
        return
    for start in range(0, len(body), TEXT_CHUNK):
        chunk = body[start : start + TEXT_CHUNK]
        try:
            await client.send_message(chat_id=user_name, text=chunk)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件][Telegram] 文本兜底发送失败：{type(exc).__name__}: {exc}")
            return


async def _dispatch(cls: Any, client: Any, message: Any, user_name: Any) -> None:
    chain: List[Any] = list(getattr(message, "chain", None) or [])
    _relax_timeouts(client)
    try:
        mc, plain_cls, risky = _classes()
    except Exception:  # noqa: BLE001
        await _orig_func(cls, client, message, user_name)
        return

    # 只有一条链时才值得拆分；带 At/Reply/按钮的消息保持原样，避免破坏语义
    text_idx = {
        i for i, comp in enumerate(chain) if isinstance(comp, plain_cls) and _text_of(comp).strip()
    }
    if len(chain) < 2 or not text_idx or len(text_idx) == len(chain):
        await _orig_func(cls, client, message, user_name)
        return
    if any(isinstance(comp, risky) for comp in chain):
        await _orig_func(cls, client, message, user_name)
        return

    texts = [chain[i] for i in sorted(text_idx)]
    media = [comp for i, comp in enumerate(chain) if i not in text_idx]

    # 1) 文字先行：媒体上传超时也吞不掉文案
    if not await _send_chain(
        cls, client, message.derive(chain=texts), user_name, retries=0, tag="文本"
    ):
        await _fallback_text(client, user_name, "\n".join(_text_of(c) for c in texts))

    # 2) 媒体逐个发，单个失败不影响后面的
    video_cls = getattr(mc, "Video", None)
    for comp in media:
        is_video = isinstance(comp, video_cls) if isinstance(video_cls, type) else False
        await _send_chain(
            cls,
            client,
            message.derive(chain=[comp]),
            user_name,
            retries=2 if is_video else 1,
            tag=f"{type(comp).__name__}",
        )


def install() -> Dict[str, bool]:
    """挂载补丁，返回 {"send": bool}"""
    global _installed, _orig_func

    try:
        from astrbot.core.platform.sources.telegram import tg_event as tg_mod
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] Telegram 发送补丁：无法导入适配器: {exc}")
        return {"send": False}

    target = getattr(tg_mod, "TelegramPlatformEvent", None)
    if target is None:
        return {"send": False}

    current = target.send_with_client
    if getattr(current, "__func__", current).__dict__.get("__rconsole_patched__"):
        _installed = True
        return {"send": True}

    _orig_func = getattr(current, "__func__", current)
    wrapped = getattr(_orig_func, "__wrapped__", None)
    if wrapped is not None:  # 别的补丁已经包过一层，穿透到最原始实现
        _orig_func = wrapped

    async def _send_with_client(cls, client, message, user_name):  # noqa: ANN001
        try:
            await _dispatch(cls, client, message, user_name)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件][Telegram] 发送补丁内部异常：{type(exc).__name__}: {exc}")
            raise

    _send_with_client.__wrapped__ = _orig_func  # type: ignore[attr-defined]
    _send_with_client.__rconsole_patched__ = True  # type: ignore[attr-defined]
    target.send_with_client = classmethod(_send_with_client)
    _installed = True
    return {"send": True}


def uninstall() -> None:
    """插件卸载/重载时还原"""
    global _installed, _orig_func

    if not _installed or _orig_func is None:
        return
    try:
        from astrbot.core.platform.sources.telegram import tg_event as tg_mod

        target = getattr(tg_mod, "TelegramPlatformEvent", None)
        if target is not None:
            current = getattr(target.send_with_client, "__func__", target.send_with_client)
            if getattr(current, "__rconsole_patched__", False):
                target.send_with_client = classmethod(_orig_func)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[R插件] 还原 Telegram 发送补丁失败: {exc}")
    finally:
        _installed = False
        _orig_func = None
