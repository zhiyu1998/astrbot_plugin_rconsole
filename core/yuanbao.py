"""
腾讯元宝（yuanbao.tencent.com）Web 端对话接口。

为什么是这么写的
----------------
元宝的 `/api/chat/{chatId}` 强校验三个签名头：

    x-uskey          = encodeURIComponent(qimei.getUSKeySync("7800385", h38, params))
    x-bus-params-md5 = md5(params)      # params = "h38=<h38>&timestamp=<ts>&platform=web"
    x-timestamp      = <ts>

缺了直接返回 SSE `{"type":"error","msg":"服务繁忙，请稍后再试。"}`。

uskey 只能由元宝页面里的 Qimei SDK 生成，而那套 SDK 是混淆过的。实测走过的两条死路：

* webpack 直取模块：Node 版写死 `webpackRequire(12601).I5(appKey)`，但元宝改版后 12601 已不存在；
  按源码特征扫全模块（2000+ 个）只能命中一个模块，导出 `NU/TE/f8/h6`，没有 Qimei 工厂。
* 纯算法复现：Qimei 是混淆的，逆出来成本极高，且随前端发版就失效。

所以改成「借页面自己的签名」：用 CDP 驱动 chromium 打开元宝对话页，
在文档创建前注入 hook，劫持 `XMLHttpRequest.setRequestHeader` 与 `window.fetch`，
把页面自己发请求时带的签名头截下来。实测这组头**可以跨请求、跨会话复用**，
（立即用、+30s、+90s 三次调用全部成功），因此按 TTL 缓存即可，不必每次签名。

还有两个坑，都是实测出来的：

* **必须新建会话**再发 chat。复用页面上正在对话的那个 chatId 会回
  `{"type":"error","msg":"回答拉取失败，正在重试","code":"21007"}`。
* `chatModelExtInfo` 里必须带 `agentModeModelSetting`（旧版文档没有这个字段），
  否则模型不生效。

依赖：chromium（headless）+ websockets + httpx。没有借助 playwright。
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import httpx
import websockets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_URL = "https://yuanbao.tencent.com"
EP_CREATE = "/api/user/agent/conversation/create"
EP_UPDATE_MODEL = "/api/user/agent/conversation/updateModel"
EP_CHAT = "/api/chat"
EP_CLEAR = "/api/user/agent/conversation/v1/clear"

# 默认智能体（元宝对话页用的那个，与视频号解析 referer 里一致）
AGENT_ID = "naQivTmsDa"

# 面板可选模型。
#
# 元组是 (chatModelId, chatModelExtInfo.modelId, plugin)：
#   * chatModelId          —— 真正选中的模型
#   * chatModelExtInfo.modelId —— 元宝的「基础模型」。Hy4 系列固定填 hunyuan_quick_hy4，
#                             实际档位由 agentModeModelSetting.modelId 决定（抓包确认）
#   * plugin               —— Hy4 的深度思考/专家档要带 "Adaptive"
#
# Hy3 / DeepSeek 两项来自 `/api/agent/model/list` 的实测返回；
# Hy4 的三档该接口不返回（灰度/会员），是照真实抓包补齐的，实测可用。
MODEL_SPECS: Dict[str, Tuple[str, str, str]] = {
    "Hy3": ("hunyuan_gpt_175B_0404", "hunyuan_gpt_175B_0404", ""),
    "Hy3 深度思考": ("hunyuan_t1", "hunyuan_t1", ""),
    "DeepSeek": ("deep_seek_v3", "deep_seek_v3", ""),
    "DeepSeek 深度思考": ("deep_seek", "deep_seek", ""),
    "Hy4 快速": ("hunyuan_quick_hy4", "hunyuan_quick_hy4", ""),
    "Hy4 深度思考": ("hunyuan_deep_hy4", "hunyuan_quick_hy4", "Adaptive"),
    "Hy4 专家": ("hunyuan_omnipotent_hy4", "hunyuan_quick_hy4", "Adaptive"),
}

# 兼容旧名：resolve_model 仍返回 chatModelId 字符串
MODEL_OPTIONS: Dict[str, str] = {k: v[0] for k, v in MODEL_SPECS.items()}
DEFAULT_MODEL = "Hy3"

# chat 接口 body.model 固定这个值（与 chatModelId 不同，抓包确认）
CHAT_API_MODEL = "gpt_175B_0404"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# 签名头名单（截获时全部带上，少一个都可能被拒）
SIGN_HEADERS = (
    "x-uskey",
    "x-bus-params-md5",
    "x-timestamp",
    "x-hy92",
    "x-hy93",
    "x-device-id",
    "x-instance-id",
    "x-webversion",
    "x-commit-tag",
    "x-os_version",
    "x-web-third-source",
    "x-webdriver",
    "x-ybuitest",
    "x-language",
    "x-platform",
    "x-source",
    "x-requested-with",
    "x-agentid",
    "x-expparams",
    "x-trid-channel",
    "x-web-ch-id",
    "x-hy106",
)

# 签名缓存时长。实测 90s 仍有效，取 180s 留余量；过期重新截获一次（约 10s）
SIGN_TTL = 180.0

# 截获等待上限
CAPTURE_TIMEOUT = 25.0

# 单条回复的字符上限（防止模型吐出超长文本把消息撑爆）
MAX_REPLY_CHARS = 3000

_CHROME_CANDIDATES = [
    "/root/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


class YuanbaoError(Exception):
    """元宝调用失败（配置 / 登录态 / 接口 / 浏览器）"""


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def find_chrome() -> str:
    """找一个能用的 chromium。找不到就抛出带排查提示的错。"""
    for pattern in _CHROME_CANDIDATES:
        for path in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    raise YuanbaoError(
        "没找到 chromium。元宝签名需要浏览器；"
        "可以装 playwright 的 chromium，或把 chromium 放到 /usr/bin 下"
    )


def parse_cookie_string(raw: str) -> List[Dict[str, str]]:
    """`a=1; b=2` -> CDP Network.setCookies 需要的结构"""
    out: List[Dict[str, str]] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".yuanbao.tencent.com",
                "path": "/",
            }
        )
    return out


def resolve_model(name: str) -> str:
    """面板取值 -> chatModelId；未知值退回默认"""
    key = (name or "").strip()
    if key in MODEL_OPTIONS.values():
        return key
    for label, model_id in MODEL_OPTIONS.items():
        if label.lower() == key.lower():
            return model_id
    return MODEL_OPTIONS[DEFAULT_MODEL]


def resolve_spec(name: str) -> Tuple[str, str, str]:
    """
    面板取值 -> (chatModelId, extInfo.modelId, plugin)。

    支持传中文标签（Hy3 / Hy4 深度思考…）或直接传 chatModelId。
    """
    key = (name or "").strip()
    if key in MODEL_SPECS:
        return MODEL_SPECS[key]
    for label, spec in MODEL_SPECS.items():
        if spec[0] == key:
            return spec
    return MODEL_SPECS[DEFAULT_MODEL]


def clean_reply(text: str) -> str:
    """清掉元宝富文本里的空链接标记，群聊里显示会很难看"""
    import re

    out = text or ""
    out = re.sub(r"\[\]\(@?mark[^)]*\)", "", out)
    out = re.sub(r"\[\]\(\*\*[^)]*\)", "", out)
    out = re.sub(r"\[citation:\d+\]", "", out)
    out = re.sub(r"\[\]\([^)]*\)", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _now_ms() -> str:
    return str(int(time.time() * 1000))


# ---------------------------------------------------------------------------
# CDP 客户端（最小实现）
# ---------------------------------------------------------------------------


class CDP:
    """够用的 CDP 封装：一个浏览器级 websocket + flatten session"""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._mid = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._closed = False

    @classmethod
    async def connect(cls, ws_url: str, timeout: float = 15.0) -> "CDP":
        ws = await asyncio.wait_for(
            websockets.connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=timeout),
            timeout=timeout,
        )
        inst = cls(ws)
        inst._reader = asyncio.create_task(inst._read_loop())
        return inst

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(YuanbaoError("CDP 连接已断开"))
            self._pending.clear()

    async def send(
        self, method: str, params: Optional[Dict[str, Any]] = None, session: str = ""
    ) -> Dict[str, Any]:
        if self._closed:
            raise YuanbaoError("CDP 连接已关闭")
        self._mid += 1
        mid = self._mid
        payload: Dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(mid, None)
            raise YuanbaoError(f"CDP 发送失败：{exc}") from exc
        msg = await asyncio.wait_for(fut, timeout=30)
        if "error" in msg:
            raise YuanbaoError(f"CDP {method} 失败：{msg['error']}")
        return msg.get("result") or {}

    async def evaluate(
        self, expression: str, session: str, *, await_promise: bool = True, timeout: float = 30.0
    ) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "timeout": int(timeout * 1000),
            },
            session=session,
        )
        if res.get("exceptionDetails"):
            raise YuanbaoError(f"页面 JS 异常：{res['exceptionDetails'].get('text')}")
        return (res.get("result") or {}).get("value")

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 签名截获
# ---------------------------------------------------------------------------

# 在文档创建前注入：页面自己请求时会把签名头写进 window.__yb
_HOOK_JS = r"""
(() => {
  window.__yb = { uskey: null, md5: null, ts: null, all: {} };
  const grab = (k, v) => {
    const lk = String(k).toLowerCase();
    if (lk === 'x-uskey') window.__yb.uskey = v;
    else if (lk === 'x-bus-params-md5') window.__yb.md5 = v;
    else if (lk === 'x-timestamp') window.__yb.ts = v;
    if (lk.startsWith('x-') || lk.startsWith('chat_')) window.__yb.all[lk] = v;
  };
  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { grab(k, v); } catch (e) {}
    return origSet.call(this, k, v);
  };
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (init && init.headers) {
        const h = new Headers(init.headers);
        h.forEach((v, k) => grab(k, v));
      }
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };
})();
"""

_READ_SIG_JS = "JSON.stringify(window.__yb || {})"

# 页面若迟迟不发带签名的请求，就用真实输入触发一次（实测必定成功）
_TYPE_JS = r"""
(() => {
  const el = document.querySelector("[contenteditable='true']") || document.querySelector('textarea');
  if (!el) return 'no-input';
  el.focus();
  return 'ok';
})()
"""


def _free_port() -> int:
    """向内核要一个空闲端口

    原来是 9222 + pid % 900 猜出来的，撞上别的 chromium 时 urlopen 会连到
    别人的 CDP，签名头就打到无辜实例上了。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_cdp_endpoint(port: int) -> str:
    """同步读 /json/version（调用方丢进线程，别阻塞事件循环）"""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
        return resp.read().decode()


class YuanbaoSigner:
    """
    起一个常驻 chromium，截获元宝签名头并缓存。

    进程与 CDP 连接跨请求复用（启动一次约 3~5 秒，截获约 5~10 秒），
    cookie 变化时整只重建。
    """

    def __init__(self, cookie: str, *, sign_ttl: float = SIGN_TTL) -> None:
        self.cookie = cookie or ""
        self.sign_ttl = sign_ttl
        self._proc: Optional[subprocess.Popen] = None
        self._cdp: Optional[CDP] = None
        self._session = ""
        self._target = ""
        self._user_dir = ""
        self._sig: Optional[Dict[str, str]] = None
        self._sig_at = 0.0
        self._lock = asyncio.Lock()
        self._loaded_cookie = ""

    # ---- 生命周期 ----

    async def _launch(self) -> None:
        chrome = find_chrome()
        # 上一轮启动失败会留下 _proc / _user_dir，先收干净再起新的：
        # 否则 mkdtemp 的新目录覆盖旧值，旧 profile 永远没人删
        if self._proc is not None or self._user_dir:
            await self.close()
        self._user_dir = tempfile.mkdtemp(prefix="yb_profile_")
        port = _free_port()
        args = [
            chrome,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--mute-audio",
            f"--user-data-dir={self._user_dir}",
            "about:blank",
        ]
        logger.info(f"[R插件][元宝] 启动浏览器：{chrome}")
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            ws_url = ""
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise YuanbaoError("浏览器启动后立即退出（常见于缺 --no-sandbox 或缺共享库）")
                try:
                    # urlopen 是同步阻塞，这一等最多 20 秒，别卡住事件循环
                    body = await asyncio.to_thread(_read_cdp_endpoint, port)
                    ws_url = json.loads(body)["webSocketDebuggerUrl"]
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.3)
            if not ws_url:
                raise YuanbaoError("浏览器没起来（CDP 端点 20 秒内不可用）")

            self._cdp = await CDP.connect(ws_url)
            info = await self._cdp.send("Target.createTarget", {"url": "about:blank"})
            self._target = info["targetId"]
            attach = await self._cdp.send(
                "Target.attachToTarget", {"targetId": self._target, "flatten": True}
            )
            self._session = attach["sessionId"]
            await self._cdp.send("Page.enable", session=self._session)
            await self._cdp.send("Network.enable", session=self._session)
            # 必须在 navigate 之前注入，否则页面首屏的请求漏掉
            await self._cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _HOOK_JS},
                session=self._session,
            )
        except BaseException:
            # Popen 之后任何一步失败（端点起不来 / attach 失败 / 被取消）都要回收，
            # 否则一只 chrome + 一个 tmp profile 就留在机器上了
            await self.close()
            raise

    async def _load(self, cookie: str) -> None:
        if self._cdp is None:
            await self._launch()
        assert self._cdp is not None
        cookies = parse_cookie_string(cookie)
        if not cookies:
            raise YuanbaoError("元宝 Cookie 为空")
        await self._cdp.send("Network.setCookies", {"cookies": cookies}, session=self._session)
        await self._cdp.send(
            "Page.navigate",
            {"url": f"{BASE_URL}/chat/{AGENT_ID}"},
            session=self._session,
        )
        self._loaded_cookie = cookie
        logger.info("[R插件][元宝] 已打开元宝对话页，等待签名")

    async def _poll_signature(self, timeout: float) -> Optional[Dict[str, str]]:
        assert self._cdp is not None
        deadline = time.monotonic() + timeout
        typed = False
        while time.monotonic() < deadline:
            raw = await self._cdp.evaluate(_READ_SIG_JS, self._session)
            try:
                data = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                data = {}
            if data.get("uskey") and data.get("md5") and data.get("ts"):
                return {k.lower(): str(v) for k, v in (data.get("all") or {}).items()}

            # 页面没自动带签名：用真实输入触发一次（点发送），这是最可靠的兜底
            if not typed and time.monotonic() > deadline - timeout * 0.55:
                typed = True
                try:
                    state = await self._cdp.evaluate(_TYPE_JS, self._session)
                    if state == "ok":
                        await self._cdp.send(
                            "Input.insertText", {"text": "你好"}, session=self._session
                        )
                        await asyncio.sleep(0.8)
                        for kind in ("keyDown", "char", "keyUp"):
                            params: Dict[str, Any] = {
                                "type": kind,
                                "key": "Enter",
                                "code": "Enter",
                                "windowsVirtualKeyCode": 13,
                            }
                            if kind == "char":
                                params["text"] = "\r"
                            else:
                                params["nativeVirtualKeyCode"] = 13
                            await self._cdp.send(
                                "Input.dispatchKeyEvent", params, session=self._session
                            )
                        logger.info("[R插件][元宝] 页面未自动带签名，已用输入触发")
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[R插件][元宝] 触发输入失败：{exc}")
            await asyncio.sleep(1.0)
        return None

    async def signature(self) -> Dict[str, str]:
        """拿一组可用的签名头（带 TTL 缓存 + 串行化）"""
        async with self._lock:
            if (
                self._sig
                and self.cookie == self._loaded_cookie
                and time.monotonic() - self._sig_at < self.sign_ttl
            ):
                return dict(self._sig)

            if self._cdp is None or self.cookie != self._loaded_cookie:
                if self._cdp is not None and self.cookie != self._loaded_cookie:
                    await self.close()
                await self._load(self.cookie)

            sig = await self._poll_signature(CAPTURE_TIMEOUT)
            if sig is None:
                # 页面可能停在加载态，重载一次再试
                logger.warning("[R插件][元宝] 首次截获签名超时，重载页面重试")
                await self._load(self.cookie)
                sig = await self._poll_signature(CAPTURE_TIMEOUT)
            if sig is None:
                raise YuanbaoError(
                    "没能从元宝页面截到签名。常见原因：Cookie 失效、页面改版、或服务器出网被拦"
                )

            self._sig = sig
            self._sig_at = time.monotonic()
            logger.info(
                f"[R插件][元宝] 签名已就绪（uskey {len(sig.get('x-uskey', ''))} 字符，"
                f"ts={sig.get('x-timestamp')}）"
            )
            return dict(sig)

    def invalidate(self) -> None:
        self._sig = None
        self._sig_at = 0.0

    async def close(self) -> None:
        if self._cdp is not None:
            try:
                await self._cdp.close()
            except Exception:  # noqa: BLE001
                pass
            self._cdp = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None
        if self._user_dir:
            shutil.rmtree(self._user_dir, ignore_errors=True)
            self._user_dir = ""
        self._sig = None
        self._loaded_cookie = ""


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


class YuanbaoClient:
    """元宝对话：create -> updateModel -> chat(SSE) -> clear"""

    def __init__(
        self,
        cookie: str,
        signer: YuanbaoSigner,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 90.0,
    ) -> None:
        self.cookie = cookie or ""
        self.signer = signer
        # (chatModelId, extInfo.modelId, plugin)
        self.model_id, self.ext_model_id, self.model_plugin = resolve_spec(model)
        self.timeout = timeout

    # ---- 内部 ----

    def _headers(self, sig: Dict[str, str]) -> Dict[str, str]:
        headers = {
            "user-agent": UA,
            "origin": BASE_URL,
            "referer": f"{BASE_URL}/chat/{AGENT_ID}",
            "cookie": self.cookie,
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }
        for name in SIGN_HEADERS:
            value = sig.get(name)
            if value:
                headers[name] = value
        return headers

    def _ext_info_for(self, model_id: str) -> str:
        """
        必须带 agentModeModelSetting，否则模型切换不生效（实测）。

        注意两处 modelId 不总相同：Hy4 系列的基础模型恒为 hunyuan_quick_hy4，
        真正的档位在 agentModeModelSetting 里（如 hunyuan_deep_hy4）。
        """
        return json.dumps(
            {
                "modelId": self.ext_model_id,
                "agentModeModelSetting": {"modelId": model_id},
                "subModelId": "",
                "supportFunctions": {"internetSearch": ""},
                "internetSearch": "autoInternetSearch",
            },
            ensure_ascii=False,
        )

    async def _create(self, cli: httpx.AsyncClient) -> str:
        resp = await cli.post(f"{BASE_URL}{EP_CREATE}", json={"agentId": AGENT_ID})
        if resp.status_code != 200:
            raise YuanbaoError(f"新建会话失败 HTTP {resp.status_code}")
        chat_id = ""
        try:
            chat_id = (resp.json() or {}).get("id") or ""
        except Exception:  # noqa: BLE001
            pass
        if not chat_id:
            raise YuanbaoError(f"元宝没返回会话 ID：{resp.text[:120]}")
        return chat_id

    async def _update_model(self, cli: httpx.AsyncClient, chat_id: str) -> None:
        # 注意字段名是 cid（不是 chatId），抓包确认
        await cli.post(
            f"{BASE_URL}{EP_UPDATE_MODEL}",
            json={
                "cid": chat_id,
                "chatModelId": self.model_id,
                "chatModelExtInfo": self._ext_info_for(self.model_id),
            },
        )

    async def _clear(self, cli: httpx.AsyncClient, chat_id: str) -> None:
        try:
            await cli.post(
                f"{BASE_URL}{EP_CLEAR}",
                json={"conversationIds": [chat_id], "uiOptions": {"noToast": True}},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[R插件][元宝] 清理会话失败（不影响结果）：{exc}")

    async def _chat(self, cli: httpx.AsyncClient, chat_id: str, prompt: str) -> str:
        body = {
            "model": CHAT_API_MODEL,
            "prompt": prompt,
            "plugin": self.model_plugin,
            "displayPrompt": prompt,
            "displayPromptType": 1,
            "agentId": AGENT_ID,
            "isTemporary": False,
            "projectId": "",
            "chatModelId": self.model_id,
            "supportFunctions": ["openAutoSearchSwitch", "autoInternetSearch"],
            "docOpenid": "",
            "options": {
                "imageIntention": {
                    "needIntentionModel": True,
                    "backendUpdateFlag": 2,
                    "intentionStatus": True,
                }
            },
            "multimedia": [],
            "supportHint": 1,
            "chatModelExtInfo": self._ext_info_for(self.model_id),
            "applicationIdList": [],
            "version": "v2",
            "extReportParams": None,
            "isAtomInput": False,
            "parsingPromptUrl": [],
            "conversationId": chat_id,
            "offsetOfHour": 8,
            "offsetOfMinute": 0,
        }
        headers = dict(cli.headers)
        headers["content-type"] = "text/plain;charset=UTF-8"
        headers["accept"] = "*/*"
        headers["chat_version"] = "v1"
        headers["x-input-type"] = "text"
        headers["x-event-input-type"] = "11"

        parts: List[str] = []
        error_text = ""
        async with cli.stream(
            "POST", f"{BASE_URL}{EP_CHAT}/{chat_id}", json=body, headers=headers
        ) as resp:
            if resp.status_code != 200:
                raise YuanbaoError(f"对话接口 HTTP {resp.status_code}")
            async for line in resp.aiter_lines():
                text = (line or "").strip()
                if not text.startswith("data:"):
                    continue
                chunk = text[5:].strip()
                if not chunk or chunk.startswith("[") or chunk in ("[DONE]", "status", "text"):
                    continue
                try:
                    obj = json.loads(chunk)
                except Exception:  # noqa: BLE001
                    continue
                kind = obj.get("type")
                if kind == "error":
                    error_text = str(obj.get("msg") or obj.get("error") or "未知错误")
                    break
                # 普通模型：{"type":"text","msg":"增量"}
                if kind == "text" and isinstance(obj.get("msg"), str):
                    parts.append(obj["msg"])
                # Hy4 系列走这个：{"type":"deepSearchAgent","contents":[{"type":"text"/"think","text":"增量"}]}
                # contents[].type == "think" 是思考过程，不能当正文
                elif kind == "deepSearchAgent":
                    for item in obj.get("contents") or []:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text" and isinstance(item.get("text"), str):
                            parts.append(item["text"])
                if sum(len(p) for p in parts) > MAX_REPLY_CHARS * 2:
                    break

        if error_text:
            if "服务繁忙" in error_text:
                self.signer.invalidate()
                raise YuanbaoError(
                    "元宝返回「服务繁忙」。签名已失效或被风控，"
                    "可以稍后重试；持续出现请检查 Cookie 是否还有效"
                )
            raise YuanbaoError(f"元宝返回错误：{error_text}")

        reply = clean_reply("".join(parts))
        if not reply:
            raise YuanbaoError("元宝没有返回任何文本")
        return reply[:MAX_REPLY_CHARS]

    # ---- 对外 ----

    async def ask(self, prompt: str) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            raise YuanbaoError("问题是空的")

        sig = await self.signer.signature()
        headers = self._headers(sig)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            follow_redirects=True,
            headers=headers,
        ) as cli:
            chat_id = await self._create(cli)
            try:
                await self._update_model(cli, chat_id)
                return await self._chat(cli, chat_id, prompt)
            finally:
                await self._clear(cli, chat_id)


# ---------------------------------------------------------------------------
# 全局单例（浏览器很贵，进程内复用）
# ---------------------------------------------------------------------------

_signer: Optional[YuanbaoSigner] = None
_signer_cookie = ""


async def get_signer(cookie: str) -> YuanbaoSigner:
    """按 cookie 复用同一个签名器；cookie 变了先换新的，再把旧的那只收干净

    判定→创建→赋值全在同步段里跑完（中间没有 await），并发调用不会各自起一只浏览器；
    close 放在后面 await，旧实例的 chromium 进程 / CDP 连接 / tmp profile 才回收得掉。
    """
    global _signer, _signer_cookie
    if _signer is not None and _signer_cookie == cookie:
        return _signer
    old, _signer = _signer, YuanbaoSigner(cookie)
    _signer_cookie = cookie
    if old is not None:
        logger.info("[R插件][元宝] Cookie 变更，回收旧签名器")
        try:
            await old.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件][元宝] 回收旧签名器失败: {exc}")
    return _signer


async def shutdown() -> None:
    """插件卸载 / 重载时清理浏览器进程"""
    global _signer, _signer_cookie
    if _signer is not None:
        await _signer.close()
        _signer = None
        _signer_cookie = ""


async def ask(
    prompt: str, *, cookie: str, model: str = DEFAULT_MODEL, timeout: float = 90.0
) -> str:
    """一句话调用（供上层 handler 用）"""
    if not (cookie or "").strip():
        raise YuanbaoError("没有配置元宝 Cookie")
    client = YuanbaoClient(cookie, await get_signer(cookie), model=model, timeout=timeout)
    return await client.ask(prompt)
