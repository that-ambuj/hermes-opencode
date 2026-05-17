"""Hermes plugin: hermes-opencode.

Registers a tool catalog, lifecycle hooks, and a background event loop that
orchestrate multiple opencode agents running in git worktrees. Drives the
executor -> reviewer -> commit -> PR -> merge cycle, surfaces opencode
questions/permissions to the user, and runs project bootstrap with opencode-
driven recovery.
"""
from __future__ import annotations

from typing import Any

from . import cli as cli_mod
from . import commands as commands_mod
from . import event_loop
from . import notify
from .config import Config, load_entry_config
from .projects import ProjectRegistry
from .state import AgentStore
from .tools import Runtime, all_tool_specs
from .transport import OpencodeClient


_runtime: Runtime | None = None


def _build_pre_llm_context() -> str | None:
    if _runtime is None:
        return None
    questions, permissions = event_loop.get_pending_snapshot()
    if not questions and not permissions:
        return None
    lines: list[str] = ["[hermes-opencode] pending items awaiting the user:"]
    for agent_id, qs in questions.items():
        for entry in qs:
            qid = entry.get("id")
            inner = entry.get("questions") or []
            for q in inner:
                body = (q.get("question") or "").strip()
                opts = q.get("options") or []
                opt_lines = [
                    f"      - {o.get('label')!r}: {o.get('description', '')}"
                    for o in opts if isinstance(o, dict)
                ]
                multi = " (multi-select)" if q.get("multiple") else ""
                custom = " (custom answer allowed)" if q.get("custom") is not False else ""
                block = [
                    f"  • agent={agent_id}  question_id={qid}{multi}{custom}",
                    f"    {body}",
                ]
                if opt_lines:
                    block.append("    options:")
                    block.extend(opt_lines)
                lines.extend(block)
    for agent_id, ps in permissions.items():
        for entry in ps:
            pid = entry.get("id")
            lines.append(
                f"  • agent={agent_id}  permission_id={pid}  type={entry.get('permission')!r}  patterns={entry.get('patterns')}"
            )
    lines.extend([
        "",
        "If the user's reply is an answer to one of these, call oc_answer(question_id=<id>, "
        "answer=<verbatim user reply>) and do NOT respond yourself. For permission requests, "
        "the reply must be one of: once | always | reject. If the user is changing topic, "
        "answer normally.",
    ])
    return "\n".join(lines)


def _pre_llm_call_hook(**_: Any) -> dict[str, Any] | None:
    ctx = _build_pre_llm_context()
    if ctx:
        return {"context": ctx}
    return None


_oc_dispatcher_cache: Any = None


def _event_text(event: Any) -> str:
    return str(getattr(event, "text", "") or "")


def _gateway_send(gateway: Any, event: Any, message: str) -> None:
    import logging
    log = logging.getLogger("hermes_opencode.gateway_hook")
    if not message:
        return
    source = getattr(event, "source", None)
    if source is None:
        return
    platform = getattr(source, "platform", None)
    chat_id = getattr(source, "chat_id", None)
    thread_id = getattr(source, "thread_id", None)
    if platform is None or chat_id is None:
        return
    adapter = notify._resolve_live_adapter(platform)
    if adapter is None:
        log.warning("no live gateway adapter for platform=%r; slash-command echo dropped", platform)
        return

    async def _send_async() -> None:
        try:
            metadata = {"thread_id": thread_id} if thread_id else None
            await adapter.send(chat_id=str(chat_id), content=message, metadata=metadata)
        except Exception as exc:
            log.warning("adapter.send failed: %s", exc)

    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(asyncio.ensure_future, _send_async())
    except RuntimeError:
        try:
            asyncio.run(_send_async())
        except Exception as exc:
            log.warning("asyncio.run send failed: %s", exc)


def _pre_gateway_dispatch_hook(event: Any = None, gateway: Any = None, **_: Any) -> dict[str, Any] | None:
    if event is None or _runtime is None or _oc_dispatcher_cache is None:
        return None
    text = _event_text(event)
    if not text:
        return None
    stripped = text.lstrip()
    parts = stripped.split(None, 1)
    if not parts or parts[0] != "/oc":
        return None
    raw_args = parts[1] if len(parts) > 1 else ""
    try:
        output = _oc_dispatcher_cache(raw_args)
    except Exception as exc:
        import logging
        logging.getLogger("hermes_opencode.gateway_hook").exception("/oc dispatch failed")
        output = f"/oc error: {exc}"
    if output:
        _gateway_send(gateway, event, output)
    return {"action": "skip", "reason": "/oc handled inline"}


def register(ctx: Any) -> None:
    global _runtime, _oc_dispatcher_cache
    config = Config.from_plugin_entry(load_entry_config())
    config.ensure_dirs()

    client = OpencodeClient(config.server_url, config.server_password)
    projects = ProjectRegistry(config.projects_file)
    agents = AgentStore(config.agents_file)

    _runtime = Runtime(config=config, client=client, projects=projects, agents=agents)
    _oc_dispatcher_cache = commands_mod.make_oc_dispatcher(_runtime)

    for spec in all_tool_specs(_runtime):
        ctx.register_tool(**spec)

    ctx.register_hook("on_session_start", lambda **kw: _runtime.on_session_start(**kw))
    ctx.register_hook("on_session_end", lambda **kw: _runtime.on_session_end(**kw))
    ctx.register_hook("pre_llm_call", _pre_llm_call_hook)
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch_hook)

    inject = getattr(ctx, "inject_message", None)
    if callable(inject):
        notify.set_inject_message(inject)

    register_cmd = getattr(ctx, "register_command", None)
    if callable(register_cmd):
        register_cmd(
            "oc",
            handler=_oc_dispatcher_cache,
            description="hermes-opencode commands: list / attach / questions / cancel / doctor. Run /oc for help.",
            args_hint="[list|attach <agent_id>|questions|cancel <agent_id>|doctor|help]",
        )

    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            name="oco",
            help="Drive hermes-opencode agents from the shell.",
            setup_fn=cli_mod.setup,
            handler_fn=cli_mod.handler,
            description="list / status / attach / kill / projects without a hermes chat session.",
        )

    event_loop.start(_runtime)
