"""Hermes plugin: hermes-opencode.

Registers a tool catalog, lifecycle hooks, and a background event loop that
orchestrate multiple opencode agents running in git worktrees. Drives the
executor -> reviewer -> commit -> PR -> merge cycle, surfaces opencode
questions/permissions to the user, and runs project bootstrap with opencode-
driven recovery.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
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


_AT_AGENT_RE = re.compile(
    r"^@([A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*)(?:\s+(.+))?\Z",
    re.DOTALL,
)
_AT_AGENT_TERMINAL_PHASES = frozenset({"DONE", "KILLED", "FAILED", "CANCELLED"})


_DISPATCHER_DIRECTIVE = (
    "[hermes-opencode] DISPATCHER MODE. When calling oc_spawn or oc_send, "
    "the opencode agent has FULL authority over its task: it plans its own "
    "work, scopes its own files, designs its own approach. You are a "
    "dispatcher, NOT a planner.\n"
    "  - Forward the human's words VERBATIM in the `prompt` / `text` arg. "
    "Do NOT plan, decompose, analyze, paraphrase, add file hints, prepend "
    "background, or insert your own framing.\n"
    "  - If the human's request is unclear, ASK THE HUMAN a clarifying "
    "question first. Never fill in gaps on opencode's behalf."
)


def _build_pending_items_block() -> str | None:
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


def _build_pre_llm_context() -> str | None:
    if _runtime is None:
        return None
    blocks: list[str] = [_DISPATCHER_DIRECTIVE]
    pending = _build_pending_items_block()
    if pending:
        blocks.append(pending)
    return "\n\n".join(blocks)


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


def _handle_at_agent_dispatch(event: Any, gateway: Any, text: str) -> dict[str, Any] | None:
    """Forward `@<agent_id> <body>` messages to the named agent's opencode
    session via fire-and-forget async dispatch, bypassing the hermes chat LLM.

    Returns None to let the gateway pipeline continue normally (including
    the existing `/oc` parser and the chat LLM beyond it) when the message
    is not `@...` or the agent_id does not resolve. Returns
    `{"action": "skip", "reason": ...}` after attempting dispatch (success
    or terminal-phase / empty-body rejection), echoing a `[hermes-opencode]`
    confirmation back to the user's channel.
    """
    if _runtime is None:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("@"):
        return None
    m = _AT_AGENT_RE.match(stripped)
    if not m:
        return None
    agent_id = m.group(1)
    body = (m.group(2) or "").strip()
    agent = _runtime.agents.get(agent_id)
    if agent is None:
        # unknown agent_id; let the chat LLM handle it as a normal message
        return None
    log = logging.getLogger("hermes_opencode.gateway_hook")
    if agent.phase in _AT_AGENT_TERMINAL_PHASES:
        _gateway_send(
            gateway, event,
            f"[hermes-opencode] cannot dispatch to @{agent_id}: phase={agent.phase}",
        )
        return {"action": "skip", "reason": f"@{agent_id} terminal phase"}
    if not body:
        _gateway_send(
            gateway, event,
            f"[hermes-opencode] empty message; use @{agent_id} <text>",
        )
        return {"action": "skip", "reason": f"@{agent_id} empty body"}

    runtime = _runtime
    worktree_path = Path(agent.worktree_path)
    session_id = agent.session_id

    async def _do_dispatch() -> None:
        try:
            await runtime.client.send_message_async(session_id, worktree_path, body)
            await event_loop._resume_from_awaiting_human(
                agent, reason=f"@{agent_id} human reply",
            )
            _gateway_send(gateway, event, f"[hermes-opencode] -> @{agent_id}")
        except Exception as exc:
            log.exception("@%s dispatch failed", agent_id)
            _gateway_send(
                gateway, event,
                f"[hermes-opencode] -> @{agent_id} failed: {exc}",
            )

    # Bridge sync hook -> async dispatch. Mirrors _gateway_send's pattern:
    # use the running loop when present (gateway async context), otherwise
    # block briefly with asyncio.run (sync test / non-async caller).
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(asyncio.ensure_future, _do_dispatch())
    except RuntimeError:
        try:
            asyncio.run(_do_dispatch())
        except Exception as exc:
            log.exception("@%s asyncio.run dispatch failed", agent_id)
            _gateway_send(
                gateway, event,
                f"[hermes-opencode] -> @{agent_id} failed: {exc}",
            )

    return {"action": "skip", "reason": f"@{agent_id} dispatched"}


def _pre_gateway_dispatch_hook(event: Any = None, gateway: Any = None, **_: Any) -> dict[str, Any] | None:
    if event is None or _runtime is None or _oc_dispatcher_cache is None:
        return None
    text = _event_text(event)
    if not text:
        return None

    # `@<agent_id> <body>` direct dispatch takes precedence. Bypasses the chat
    # LLM entirely; only intercepts when the agent_id resolves.
    at_result = _handle_at_agent_dispatch(event, gateway, text)
    if at_result is not None:
        return at_result

    stripped = text.lstrip()
    parts = stripped.split(None, 1)
    if not parts or parts[0] != "/oc":
        return None
    raw_args = parts[1] if len(parts) > 1 else ""
    try:
        output = _oc_dispatcher_cache(raw_args)
    except Exception as exc:
        logging.getLogger("hermes_opencode.gateway_hook").exception("/oc dispatch failed")
        output = f"/oc error: {exc}"
    if output:
        _gateway_send(gateway, event, output)
    return {"action": "skip", "reason": "/oc handled inline"}


def register(ctx: Any) -> None:
    global _runtime, _oc_dispatcher_cache
    config = Config.from_plugin_entry(load_entry_config())
    config.ensure_dirs()

    client = OpencodeClient(config.server_url, config.server_password, config.host)
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
