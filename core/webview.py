"""插件可视化视图的后端 API

对应 views/settings/ 页面，提供：
  GET  /<PLUGIN>/view/schema  配置结构 + 当前值（密钥脱敏）
  POST /<PLUGIN>/view/save    保存配置并热应用
  GET  /<PLUGIN>/view/status  运行状态与依赖诊断
  POST /<PLUGIN>/view/probe   连通性自检（代理 / 暂存目录）
  POST /<PLUGIN>/view/platform 平台开关（点一下就立即生效）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

# 这些字段不回传真实值，只告诉前端「有没有配置过」。
# 注意：**必须按段 / 后缀匹配**，不能用子串 —— 否则 FALLBACK 里的 "CK"
# 会把普通布尔开关误判成密钥（实测踩过）。
SECRET_SEGMENTS = {
    "CK",
    "COOKIE",
    "COOKIES",
    "SESSDATA",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "APIKEY",
}
SECRET_SUFFIXES = (
    "_CK",
    "_COOKIE",
    "_COOKIES",
    "_SESSDATA",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_KEY",
)
_TRUE = {"true", "1", "on", "yes", "是"}

# 值需要从 AstrBot 运行时动态拉取的字段：key -> 数据源名
DYNAMIC_OPTION_FIELDS = {
    "SUMMARY_PROVIDER_ID": "providers",
}


def _is_secret(key: str) -> bool:
    """
    判断某个配置项是否属于「密钥类」，需要对前端脱敏。

    不能直接用子串匹配：`NETEASE_QUALITY_FALLBACK` / `NETEASE_TEMP_FALLBACK`
    里的 `FALLB**ACK**` 含有 "CK"，会被误判成密钥 —— 前端拿到空串，
    布尔开关一律显示成「已关闭」，保存时又因为「空值代表不修改」而存不进去。
    所以这里按 `_` 分段匹配整段，或按后缀匹配。
    """
    upper = key.upper()
    segments = set(upper.split("_"))
    if segments & SECRET_SEGMENTS:
        return True
    return upper.endswith(SECRET_SUFFIXES)


def _split_desc(desc: str):
    """把「【全局】显示封面」拆成 (分组, 标签)"""
    text = (desc or "").strip()
    if text.startswith("【") and "】" in text:
        group, _, rest = text[1:].partition("】")
        return group.strip(), rest.strip() or group.strip()
    return "其他", text


def _schema_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json"))


def _load_schema() -> Dict[str, Any]:
    try:
        with open(_schema_path(), encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] 读取 _conf_schema.json 失败: {exc}")
        return {}


def _coerce(field: Dict[str, Any], value: Any) -> Any:
    """按 schema 的 type 做一次类型收敛，避免把字符串塞进 int/bool/list"""
    kind = field.get("type", "string")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in _TRUE
    if kind == "int":
        try:
            return int(str(value).strip() or 0)
        except (TypeError, ValueError):
            return field.get("default", 0)
    if kind == "list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [line.strip() for line in str(value).splitlines() if line.strip()]
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return field.get("default", 0)
    return "" if value is None else str(value)


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        return 0
    return total


def _which(name: str) -> str:
    return shutil.which(name) or ""


def _as_list(value: Any) -> List[str]:
    """DISABLED_PLATFORMS / PROXY_PLATFORMS 这类 list 字段统一展开成字符串列表

    允许写成 list，也允许写成「一行一个」或「逗号分隔」的字符串 ——
    AstrBot 内置配置页里填的是字符串时也要能读。
    """
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[\r\n,;，、]+", str(value or "")) if item.strip()]


def _platform_states(plugin, config) -> List[Dict[str, Any]]:
    """把 DISABLED_PLATFORMS 展开成「平台 -> 是否启用」，给视图里的平台开关用

    注意配置里可以写英文 key（douyin）也可以写中文名（抖音），两边都要认，
    否则面板上显示「已开启」而运行时其实是关掉的。
    """
    disabled = {item.lower() for item in _as_list(config.get("DISABLED_PLATFORMS"))}
    states: List[Dict[str, Any]] = []
    for key, label in (getattr(plugin, "platform_labels", {}) or {}).items():
        off = key.lower() in disabled or str(label).strip().lower() in disabled
        states.append({"key": key, "label": label, "enabled": not off})
    return states


async def _save_config(plugin, config) -> str:
    """把配置写盘并热应用；正常返回空串，出问题返回给前端看的错误文本"""
    try:
        saver = getattr(config, "save_config_async", None)
        if callable(saver):
            await saver()
        else:
            config.save_config()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] 写配置文件失败: {exc}")
        return f"写配置文件失败: {exc}"
    try:
        plugin.apply_runtime_settings()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[R插件] 热应用配置失败: {exc}")
        return f"热应用配置失败: {exc}"
    return ""


def register_view_apis(context, plugin, plugin_name: str) -> None:
    """把所有视图 API 注册到 Dashboard"""

    def _config():
        return getattr(plugin, "config", None) or {}

    async def page_schema():
        schema = _load_schema()
        config = _config()
        groups: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []

        for key, field in schema.items():
            group, label = _split_desc(field.get("description", key))
            secret = _is_secret(key)
            raw = config.get(key, field.get("default"))
            if field.get("type") == "text":
                # 长文本（提示词）：配置里没写过就回空，让前端展示内置默认值
                raw = config.get(key, "")
            if secret:
                value = ""
                has_value = bool(str(raw or "").strip())
            else:
                value = raw
                has_value = bool(str(raw or "").strip())

            item = {
                "key": key,
                "label": label,
                "group": group,
                "type": field.get("type", "string"),
                "hint": field.get("hint", ""),
                "options": field.get("options") or [],
                "labels": field.get("labels") or [],
                "default": field.get("default"),
                "value": value,
                "is_secret": secret,
                "has_value": has_value,
                "obvious": bool(field.get("obvious_hint")),
                "dynamic_options": DYNAMIC_OPTION_FIELDS.get(key, ""),
            }
            if group not in groups:
                groups[group] = []
                order.append(group)
            groups[group].append(item)

        return json_response(
            {
                "plugin": plugin_name,
                "version": getattr(plugin, "version", ""),
                "groups": [{"name": name, "fields": groups[name]} for name in order],
            }
        )

    async def page_save():
        payload = await request.json(default={})
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, dict) or not values:
            return error_response("没有需要保存的配置", status_code=400)

        schema = _load_schema()
        config = _config()
        saved: List[str] = []
        skipped: List[str] = []

        for key, value in values.items():
            field = schema.get(key)
            if not field:
                skipped.append(key)
                continue
            if _is_secret(key):
                # 空字符串代表「不修改」
                if value is None or str(value).strip() == "":
                    continue
            try:
                config[key] = _coerce(field, value)
                saved.append(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[R插件] 保存 {key} 失败: {exc}")
                skipped.append(key)

        if not saved:
            return error_response("没有有效的配置项", status_code=400)

        try:
            saver = getattr(config, "save_config_async", None)
            if callable(saver):
                await saver()
            else:
                config.save_config()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件] 写配置文件失败: {exc}")
            return error_response(f"写配置文件失败: {exc}", status_code=500)

        applied = []
        try:
            applied = plugin.apply_runtime_settings() or []
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[R插件] 热应用配置失败: {exc}")
            return json_response(
                {"saved": saved, "skipped": skipped, "applied": applied, "warning": str(exc)}
            )

        return json_response({"saved": saved, "skipped": skipped, "applied": applied})

    async def page_status():
        from .common import proxy_for, temp_dir
        from .youtube import check_proxy

        config = _config()
        cache_root = temp_dir()
        free_gb = 0.0
        writable = False
        try:
            usage = shutil.disk_usage(cache_root)
            free_gb = round(usage.free / 1024 / 1024 / 1024, 2)
            probe = os.path.join(cache_root, ".write_test")
            with open(probe, "w", encoding="utf-8") as fp:
                fp.write("ok")
            os.remove(probe)
            writable = True
        except Exception:  # noqa: BLE001
            writable = False

        yt_version = ""
        try:
            import yt_dlp

            yt_version = getattr(yt_dlp.version, "__version__", "")
        except Exception:  # noqa: BLE001
            yt_version = ""

        proxy_url = proxy_for("youtube") or ""
        proxy_info: Dict[str, Any] = {"url": proxy_url, "ok": None}
        if request.query.get("probe", "", type=str) in ("1", "true", "yes"):
            if proxy_url:
                started = time.time()
                result = await check_proxy(proxy_url, timeout=8)
                proxy_info["ok"] = bool(result.get("ok"))
                proxy_info["cost"] = round(time.time() - started, 1)
                proxy_info["error"] = result.get("error", "")
            else:
                proxy_info["ok"] = None

        # 平台开关：DISABLED_PLATFORMS 里可能写英文 key 也可能写中文名，
        # 统一走 _platform_states 判定，避免面板显示「开」而运行时其实是关的
        platforms = _platform_states(plugin, config)

        return json_response(
            {
                "version": getattr(plugin, "version", ""),
                "config_path": getattr(config, "config_path", "") or "",
                "platforms": platforms,
                "proxy": proxy_info,
                "temp": {
                    "path": cache_root,
                    "free_gb": free_gb,
                    "writable": writable,
                    "used_mb": round(_dir_size(cache_root) / 1024 / 1024, 2),
                },
                "tools": {
                    "ffmpeg": _which("ffmpeg"),
                    "aria2c": _which(str(config.get("ARIA2C_PATH") or "aria2c")),
                    "bbdown": _which(str(config.get("BBDOWN_PATH") or "BBDown")),
                    "yt_dlp": yt_version,
                },
                "secrets": {
                    "bili_sessdata": bool(str(config.get("BILI_SESSDATA") or "").strip()),
                    "douyin_ck": bool(str(config.get("DOUYIN_CK") or "").strip()),
                    "weibo_ck": bool(str(config.get("WEIBO_CK") or "").strip()),
                    "xhs_ck": bool(str(config.get("XHS_CK") or "").strip()),
                    "xhh_ck": bool(str(config.get("XHH_CK") or "").strip()),
                    "wxchannel_cookie": bool(str(config.get("WXCHANNEL_COOKIE") or "").strip()),
                    "youtube_cookies": bool(str(config.get("YOUTUBE_COOKIES") or "").strip()),
                },
                "download": {
                    "concurrency": config.get("DOWNLOAD_CONCURRENCY", 2),
                    "threads": config.get("DOWNLOAD_THREADS", 4),
                    "aria2": bool(config.get("DOWNLOADS_USE_ARIA2")),
                },
            }
        )

    async def page_providers():
        """列出 AstrBot 当前可用的对话模型 Provider，供下拉框直接选择"""
        items: List[Dict[str, Any]] = []

        default_id = ""
        try:
            using = context.get_using_provider()
            if using is not None:
                default_id = getattr(using.meta(), "id", "") or ""
        except Exception:  # noqa: BLE001
            default_id = ""

        try:
            providers = context.get_all_providers() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[R插件] 读取 Provider 列表失败: {exc}")
            providers = []

        for provider in providers:
            try:
                meta = provider.meta()
            except Exception:  # noqa: BLE001
                continue
            pid = str(getattr(meta, "id", "") or "").strip()
            if not pid:
                continue
            model = str(getattr(meta, "model", "") or "").strip()
            ptype = str(getattr(meta, "type", "") or "").strip()
            label = f"{model or pid} · {pid}"
            if ptype:
                label = f"{label} 〔{ptype}〕"
            items.append(
                {
                    "id": pid,
                    "label": label,
                    "model": model,
                    "type": ptype,
                    "is_default": pid == default_id,
                }
            )

        return json_response({"items": items, "default": default_id})

    async def page_probe():
        from .common import proxy_for, temp_dir
        from .youtube import check_proxy

        payload = await request.json(default={})
        target = str((payload or {}).get("target") or "proxy")

        if target == "proxy":
            proxy_url = proxy_for("youtube") or ""
            if not proxy_url:
                return json_response({"target": "proxy", "ok": None, "message": "未配置代理（直连）"})
            started = time.time()
            result = await check_proxy(proxy_url, timeout=10)
            return json_response(
                {
                    "target": "proxy",
                    "ok": bool(result.get("ok")),
                    "cost": round(time.time() - started, 1),
                    "message": "可达 YouTube" if result.get("ok") else (result.get("error") or "连接失败"),
                }
            )

        if target == "temp":
            path = temp_dir()
            try:
                probe = os.path.join(path, f".probe_{int(time.time())}")
                with open(probe, "w", encoding="utf-8") as fp:
                    fp.write("ok")
                os.remove(probe)
                return json_response({"target": "temp", "ok": True, "message": f"可写：{path}"})
            except Exception as exc:  # noqa: BLE001
                return json_response({"target": "temp", "ok": False, "message": str(exc)})

        return error_response(f"未知的探测目标: {target}", status_code=400)

    async def page_platform():
        """点一下平台开关就立即生效：改 DISABLED_PLATFORMS → 写盘 → 热应用

        运行时解析入口每次都用 `_enabled()` 现读配置，所以这里改完立刻生效，
        不需要重启插件。传 `enabled` 是设置成指定状态；不传就是翻转。
        """
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        key = str(payload.get("key") or "").strip()
        if not key:
            return error_response("缺少平台标识 key", status_code=400)

        labels = getattr(plugin, "platform_labels", {}) or {}
        canonical = next((item for item in labels if item.lower() == key.lower()), "")
        if not canonical:
            # 也允许直接传中文名（抖音 / 哔哩哔哩 …）
            canonical = next(
                (item for item, label in labels.items() if str(label).strip() == key), ""
            )
        if not canonical:
            return error_response(f"未知平台: {key}", status_code=404)
        label = str(labels[canonical]).strip()

        config = _config()
        disabled = _as_list(config.get("DISABLED_PLATFORMS"))
        blocked = {canonical.lower(), label.lower()}
        current = not any(item.lower() in blocked for item in disabled)

        enabled = payload.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in _TRUE
        enabled = (not current) if enabled is None else bool(enabled)

        # 顺手把中文名形式的旧条目一起清掉，避免同一平台在列表里出现两次
        rest = [item for item in disabled if item.lower() not in blocked]
        if not enabled:
            rest.append(canonical)

        try:
            config["DISABLED_PLATFORMS"] = rest
        except Exception as exc:  # noqa: BLE001
            return error_response(f"写入配置失败: {exc}", status_code=500)

        error = await _save_config(plugin, config)
        if error:
            return error_response(error, status_code=500)

        logger.info(f"[R插件] 平台开关：{label} → {'开启' if enabled else '关闭'}")
        return json_response(
            {
                "key": canonical,
                "label": label,
                "enabled": enabled,
                "disabled": rest,
                "platforms": _platform_states(plugin, config),
            }
        )

    context.register_web_api(f"/{plugin_name}/view/schema", page_schema, ["GET"], "R插件配置结构")
    context.register_web_api(f"/{plugin_name}/view/save", page_save, ["POST"], "R插件保存配置")
    context.register_web_api(f"/{plugin_name}/view/status", page_status, ["GET"], "R插件运行状态")
    context.register_web_api(f"/{plugin_name}/view/probe", page_probe, ["POST"], "R插件连通性探测")
    context.register_web_api(
        f"/{plugin_name}/view/platform", page_platform, ["POST"], "R插件平台开关"
    )
    context.register_web_api(
        f"/{plugin_name}/view/providers", page_providers, ["GET"], "R插件可用模型列表"
    )
