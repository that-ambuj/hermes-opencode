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


def _resolve_live_adapter(
    platform: str,
    *,
    runner_ref: Callable[[], Any] | None = None,
    platform_enum_cls: Any = None,
) -> tuple[Any | None, str | None]:
    """Return (adapter, err) for the live in-process gateway adapter; exactly one side is non-None."""
    if runner_ref is None or platform_enum_cls is None:
        try:
            from gateway.run import _gateway_runner_ref as _runner  # type: ignore
            from gateway.config import Platform as _Platform  # type: ignore
        except ImportError as e:
            return None, f"gateway not importable: {e}"
        if runner_ref is None:
            runner_ref = _runner
        if platform_enum_cls is None:
            platform_enum_cls = _Platform

    try:
        platform_enum = (
            platform
            if isinstance(platform, platform_enum_cls)
            else platform_enum_cls(platform)
        )
    except Exception as e:
        return None, f"unknown platform {platform!r}: {e}"

    runner = runner_ref()
    if runner is None:
        return None, "no live gateway runner in this process"
    adapter = getattr(runner, "adapters", {}).get(platform_enum)
    if adapter is None:
        name = getattr(platform_enum, "value", platform_enum)
        return None, f"no live adapter for {name!r} (gateway not running this platform)"
    return adapter, None


def _send_gateway(title: str, body: str, _meta: dict[str, Any], platform: str | None, chat_id: str | None) -> NotifyResult:
    if not platform or not chat_id:
        return NotifyResult("gateway", False, "platform or chat_id not configured")
    adapter, err = _resolve_live_adapter(platform)
    if adapter is None:
        return NotifyResult("gateway", False, err or "adapter resolution failed")
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
    if result is None:
        return NotifyResult("gateway", False, "send returned None")
    ok = bool(getattr(result, "success", getattr(result, "ok", False)))
    detail = "" if ok else (str(getattr(result, "error", "") or "") or "send returned non-ok")
    return NotifyResult("gateway", ok, detail)


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
