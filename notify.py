from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("hermes_opencode.notify")


@dataclass
class NotifyResult:
    sink: str
    ok: bool
    detail: str = ""


_inject_message: Callable[..., bool] | None = None


def set_inject_message(callback: Callable[..., bool] | None) -> None:
    global _inject_message
    _inject_message = callback


def _send_cli(title: str, body: str, _meta: dict[str, Any]) -> NotifyResult:
    if _inject_message is None:
        return NotifyResult("cli", False, "no inject_message bound (plugin not in CLI context)")
    payload = f"[hermes-opencode] {title}\n{body}"
    try:
        ok = _inject_message(content=payload, role="user")
    except TypeError:
        ok = _inject_message(payload, "user")
    except Exception as e:
        return NotifyResult("cli", False, repr(e))
    return NotifyResult("cli", bool(ok), "" if ok else "inject_message returned falsy")


def _send_dashboard(title: str, body: str, meta: dict[str, Any], path: Path) -> NotifyResult:
    record = {
        "ts": time.time(),
        "title": title,
        "body": body,
        "meta": meta,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return NotifyResult("dashboard", True, str(path))
    except OSError as e:
        return NotifyResult("dashboard", False, repr(e))


def _send_gateway(title: str, body: str, _meta: dict[str, Any], platform: str | None, chat_id: str | None) -> NotifyResult:
    if not platform or not chat_id:
        return NotifyResult("gateway", False, "platform or chat_id not configured")
    try:
        from gateway.platform_registry import platform_registry  # type: ignore
        from gateway.config import Platform, load_gateway_config  # type: ignore
    except ImportError as e:
        return NotifyResult("gateway", False, f"gateway not importable: {e}")
    try:
        platform_enum = Platform(platform) if not isinstance(platform, Platform) else platform
    except Exception as e:
        return NotifyResult("gateway", False, f"unknown platform {platform!r}: {e}")
    try:
        gconfig = load_gateway_config()
    except Exception as e:
        return NotifyResult("gateway", False, f"load_gateway_config failed: {e}")
    pconfig = (gconfig.platforms or {}).get(platform_enum)
    if pconfig is None:
        return NotifyResult("gateway", False, f"no platform config for {platform_enum!r} in gateway config")
    try:
        adapter = platform_registry.create_adapter(str(platform_enum.value if hasattr(platform_enum, 'value') else platform_enum), pconfig)
    except Exception as e:
        return NotifyResult("gateway", False, f"create_adapter failed: {e}")
    if adapter is None:
        return NotifyResult("gateway", False, f"create_adapter returned None for {platform_enum!r}")
    content = f"*{title}*\n{body}"
    try:
        from model_tools import _run_async  # type: ignore
        result = _run_async(adapter.send(chat_id=chat_id, content=content))
    except ImportError:
        import asyncio
        try:
            result = asyncio.run(adapter.send(chat_id=chat_id, content=content))
        except RuntimeError as e:
            return NotifyResult("gateway", False, f"asyncio.run failed: {e}")
    except Exception as e:
        return NotifyResult("gateway", False, repr(e))
    ok = bool(getattr(result, "ok", True) if result is not None else False)
    return NotifyResult("gateway", ok, "" if ok else "send returned non-ok")


def fanout(
    sinks: list[str],
    title: str,
    body: str,
    *,
    meta: dict[str, Any] | None = None,
    dashboard_path: Path | None = None,
    gateway_platform: str | None = None,
    gateway_chat_id: str | None = None,
) -> list[NotifyResult]:
    meta = meta or {}
    results: list[NotifyResult] = []
    for s in sinks:
        if s == "cli":
            results.append(_send_cli(title, body, meta))
        elif s == "dashboard":
            if dashboard_path is None:
                results.append(NotifyResult("dashboard", False, "no dashboard_path"))
            else:
                results.append(_send_dashboard(title, body, meta, dashboard_path))
        elif s == "gateway":
            results.append(_send_gateway(title, body, meta, gateway_platform, gateway_chat_id))
        else:
            results.append(NotifyResult(s, False, f"unknown sink {s!r}"))
    return results
