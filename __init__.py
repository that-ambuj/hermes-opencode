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
import time
from pathlib import Path
from typing import Any

from . import cli as cli_mod
from . import commands as commands_mod
from . import event_loop
from . import notify
from .config import Config, load_entry_config
from .projects import ProjectRegistry
from .state import AgentStore, TERMINAL_PHASES
from .tools import Runtime, all_tool_specs
from .transport import OpencodeClient


_runtime: Runtime | None = None


_AT_AGENT_RE = re.compile(
    r"^@([A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*)(?:\s+(.+))?\Z",
    re.DOTALL,
)
_AT_AGENT_TERMINAL_PHASES = frozenset({"DONE", "KILLED", "FAILED", "CANCELLED"})


_DISPATCHER_DIRECTIVE = (
    "[hermes-opencode] DISPATCHER MODE - MANDATORY RULES.\n"
    "\n"
    "You orchestrate opencode coding agents. When the human asks for "
    "code work (build, fix, implement, add, create, change, refactor, "
    "write, migrate, port, ship, debug-and-fix, and similar) your "
    "FIRST action is a tool call, not prose. Specifically:\n"
    "  1. New task -> call `oc_spawn` IMMEDIATELY. Forward the human's "
    "words VERBATIM in `prompt`. Do NOT plan, decompose, paraphrase, "
    "add file hints, prepend background, or insert your own framing.\n"
    "  2. Follow-up to a live agent -> `oc_send` (verbatim text). For "
    "answers to tracked /question entries -> `oc_answer` (the plugin "
    "tells you which question_id when it applies).\n"
    "  3. Status / progress questions -> consult the 'Active agents' "
    "and 'Recent activity' blocks below, then call `oc_status` "
    "(summary) or `oc_output` (full text) if more detail is needed.\n"
    "  4. Stuck / failed agent -> `oc_retry`.\n"
    "\n"
    "Opencode has FULL authority over its task. It does its own "
    "planning, file exploration, design, and execution. You are a "
    "DISPATCHER, not a planner. Never fill in gaps on opencode's "
    "behalf. If the human's request is unclear, ask THEM, not "
    "opencode."
)


_TASK_VERBS = (
    "build", "fix", "implement", "add", "create", "write", "make",
    "change", "refactor", "migrate", "port", "ship", "rewrite",
    "wire", "connect", "install", "set up", "setup", "update",
    "upgrade", "remove", "delete", "rename", "split", "extract",
    "inline", "patch", "handle", "do", "hook up", "fixup",
)
_TASK_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|would you\s+|let'?s\s+|"
    r"go\s+(?:ahead\s+and\s+)?|do me a favor and\s+|hey,?\s+)?"
    r"(" + "|".join(re.escape(v) for v in _TASK_VERBS) + r")\b",
    re.IGNORECASE,
)


def _looks_like_task(user_message: str) -> bool:
    text = (user_message or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return False
    if len(text) > 4000:
        return False
    return bool(_TASK_PREFIX_RE.match(text))


def _humanize_short(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


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


def _build_active_agents_block() -> str | None:
    if _runtime is None:
        return None
    agents_store = getattr(_runtime, "agents", None)
    if agents_store is None or not hasattr(agents_store, "list"):
        return None
    rows: list[str] = []
    now = time.time()
    for a in agents_store.list():
        if a.phase in TERMINAL_PHASES:
            continue
        status = event_loop.get_session_status(a.agent_id) or {}
        status_kind = status.get("type") or ""
        status_tag = f"  session={status_kind}" if status_kind else ""
        age = _humanize_short(now - (a.phase_entered_at or a.created_at))
        line = f"  • {a.agent_id}  phase={a.phase}{status_tag}  in_phase_for={age}"
        if a.pr_url:
            line += f"  pr={a.pr_url}"
        rows.append(line)
        buffer = event_loop.get_text_buffer(a.agent_id)
        if buffer:
            joined = "\n".join(buffer[k] for k in sorted(buffer.keys()) if isinstance(buffer[k], str)).strip()
            if joined:
                snippet = joined if len(joined) <= 220 else joined[-220:].lstrip()
                snippet = snippet.replace("\n", " ").strip()
                rows.append(f"      latest: {snippet}")
    if not rows:
        return None
    header = f"[hermes-opencode] Active agents ({len(rows) - sum(1 for r in rows if r.lstrip().startswith('latest:'))}):"
    footer = "  (Use oc_status for details, oc_output for full text, oc_send to message, oc_answer for pending /question, oc_retry to kick.)"
    return "\n".join([header, *rows, footer])


_session_watermarks: dict[str, float] = {}


def _build_recent_events_block(session_id: str | None) -> str | None:
    if _runtime is None or not session_id:
        return None
    if not hasattr(_runtime, "config"):
        return None
    now = time.time()
    watermark = _session_watermarks.get(session_id)
    if watermark is None:
        _session_watermarks[session_id] = now
        return None
    events = event_loop.tail_recent_events(since_ts=watermark, limit=20)
    if not events:
        _session_watermarks[session_id] = max(watermark, now)
        return None
    rows: list[str] = []
    latest_ts = watermark
    for ev in events:
        try:
            ts = float(ev.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts > latest_ts:
            latest_ts = ts
        kind = ev.get("kind") or "event"
        agent_id = ev.get("agent_id") or "?"
        body = (ev.get("body") or "").strip()
        if len(body) > 220:
            body = body[:220].rstrip() + "..."
        rows.append(f"  • {_humanize_short(now - ts)} ago  {agent_id} {kind}: {body}")
    _session_watermarks[session_id] = max(latest_ts, now)
    if not rows:
        return None
    header = f"[hermes-opencode] Since your last message ({len(rows)} event{'s' if len(rows) != 1 else ''}):"
    return "\n".join([header, *rows])


def _build_dispatch_nudge_block(user_message: str) -> str | None:
    if not _looks_like_task(user_message):
        return None
    return (
        "[hermes-opencode] DISPATCH NUDGE — the user's message reads as a "
        "task. Your first action MUST be `oc_spawn` (or `oc_send` to an "
        "existing agent if this is clearly a follow-up to one named "
        "above). Forward their exact words. Do NOT plan, analyze, or "
        "ask the agent for a plan — opencode plans for itself. If you "
        "genuinely cannot tell what they want, ask THEM ONE clarifying "
        "question before dispatching."
    )


def _normalize_answer_token(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


_YES_NO_TOKENS = frozenset({
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "go",
    "go ahead", "do it", "proceed", "ship it", "approved",
    "no", "n", "nope", "nah", "skip", "cancel", "reject", "stop",
    "abort",
})


def _build_answer_nudge_block(user_message: str) -> str | None:
    text = _normalize_answer_token(user_message)
    if not text or len(text) > 200:
        return None
    questions, _permissions = event_loop.get_pending_snapshot()
    if not questions:
        return None
    matches: list[tuple[str, str, str]] = []
    for agent_id, qs in questions.items():
        for entry in qs:
            qid = entry.get("id") or ""
            inner = entry.get("questions") or []
            for q in inner:
                opts = q.get("options") or []
                labels = [
                    str(o.get("label") or "").strip()
                    for o in opts if isinstance(o, dict)
                ]
                for label in labels:
                    if not label:
                        continue
                    if text == label.lower() or text == label.lower().replace(" ", "-") or text == label.lower().replace("-", " "):
                        matches.append((agent_id, qid, label))
                        break
    if not matches and len(questions) == 1 and text in _YES_NO_TOKENS:
        first_agent = next(iter(questions))
        first_entry = questions[first_agent][0]
        qid = first_entry.get("id") or ""
        matches.append((first_agent, qid, "(yes/no)"))
    if not matches:
        return None
    if len(matches) > 1:
        rows = [
            "[hermes-opencode] ANSWER NUDGE — the user's reply matches "
            "labels on multiple pending questions. Pick the one they "
            "most plausibly meant and call oc_answer with that "
            "question_id; do not answer them yourself:",
        ]
        for agent_id, qid, label in matches:
            rows.append(f"  • agent={agent_id}  question_id={qid}  matched_label={label!r}")
        return "\n".join(rows)
    agent_id, qid, label = matches[0]
    return (
        f"[hermes-opencode] ANSWER NUDGE — the user's reply ({user_message.strip()!r}) "
        f"matches option {label!r} of pending question_id={qid} (agent={agent_id}). "
        f"Call oc_answer(question_id={qid!r}, answer=<verbatim user reply>) and do NOT "
        f"respond to them yourself."
    )


def _build_pre_llm_context(session_id: str | None = None, user_message: str = "") -> str | None:
    if _runtime is None:
        return None
    blocks: list[str] = [_DISPATCHER_DIRECTIVE]
    active = _build_active_agents_block()
    if active:
        blocks.append(active)
    recent = _build_recent_events_block(session_id)
    if recent:
        blocks.append(recent)
    dispatch = _build_dispatch_nudge_block(user_message)
    if dispatch:
        blocks.append(dispatch)
    answer = _build_answer_nudge_block(user_message)
    if answer:
        blocks.append(answer)
    pending = _build_pending_items_block()
    if pending:
        blocks.append(pending)
    return "\n\n".join(blocks)


def _pre_llm_call_hook(
    session_id: str = "",
    user_message: str = "",
    **_: Any,
) -> dict[str, Any] | None:
    if not isinstance(session_id, str):
        session_id = ""
    if not isinstance(user_message, str):
        user_message = ""
    ctx = _build_pre_llm_context(session_id=session_id or None, user_message=user_message)
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

    client = OpencodeClient(config.host, config.port, config.server_password)
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
