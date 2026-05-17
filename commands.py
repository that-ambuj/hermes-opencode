from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import event_loop
from .transport import OpencodeError

if TYPE_CHECKING:
    from .state import Agent
    from .tools import Runtime


DEFAULT_ATTACH_LINES = 80


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m:02d}m"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d{h:02d}h"


_PHASE_GLYPH = {
    "EXECUTING": "▶",
    "EXECUTOR_ADDRESSING": "▶",
    "IDLE_TASK_COMPLETE": "⏸",
    "IDLE_REVIEW_ADDRESSED": "⏸",
    "REVIEW_SPAWNING": "🔎",
    "REVIEWING": "🔎",
    "REVIEW_DELIVERED": "🔎",
    "COMMITTING": "💾",
    "PR_OPEN": "🔗",
    "DONE": "✓",
    "FAILED": "✗",
    "KILLED": "🛑",
    "CANCELLED": "🚫",
}


def _fmt_list(agents: list["Agent"], now_ts: float | None = None, *, include_archived: bool = False) -> str:
    if not agents:
        return "no agents tracked"
    now_ts = now_ts if now_ts is not None else time.time()
    visible = [a for a in agents if include_archived or not getattr(a, "archived", False)]
    if not visible:
        return "no agents tracked (use --archived to include archived)"
    blocks: list[str] = []
    for a in visible:
        glyph = _PHASE_GLYPH.get(a.phase, "•")
        age = _format_age(now_ts - a.last_activity_at)
        parts = [f"{glyph} {a.agent_id}", a.phase, age]
        if getattr(a, "archived", False):
            parts.append("archived")
        if a.pr_url:
            parts.append(a.pr_url)
        elif a.pr_number:
            parts.append(f"PR #{a.pr_number}")
        primary = " · ".join(parts)
        cont = _continuation_line(a)
        blocks.append(primary if not cont else f"{primary}\n    {cont}")
    return "\n".join(blocks)


def _continuation_line(a: "Agent") -> str | None:
    if a.phase == "FAILED" and a.last_error:
        return f"error: {a.last_error[:160]}"
    if a.phase == "CANCELLED" and a.cancellation_reason:
        return f"cancelled: {a.cancellation_reason[:160]}"
    if a.phase == "DONE" and a.pr_url is None:
        return "merged"
    return None


def _join_buffer_text(items: list[dict], lines: int) -> str:
    chunks: list[str] = []
    for item in items:
        for part in item.get("parts") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    if not chunks:
        return ""
    joined = "\n".join(chunks)
    if lines <= 0:
        return joined
    split = joined.splitlines()
    if len(split) <= lines:
        return joined
    return "\n".join(split[-lines:])


def _parse_oc_attach_args(raw_args: str) -> tuple[str | None, int, str | None]:
    """Parse ``<agent_id> [--lines N]`` from a raw slash-command arg string.

    Returns ``(agent_id, lines, error)``. ``error`` is non-None when parsing
    failed; callers should surface it verbatim.
    """
    tokens = (raw_args or "").split()
    if not tokens:
        return None, DEFAULT_ATTACH_LINES, "usage: /oc attach <agent_id> [--lines N]"
    agent_id = tokens[0]
    lines = DEFAULT_ATTACH_LINES
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--lines":
            if i + 1 >= len(tokens):
                return agent_id, lines, "missing value for --lines"
            try:
                lines = int(tokens[i + 1])
            except ValueError:
                return agent_id, lines, f"--lines requires an integer, got {tokens[i + 1]!r}"
            if lines <= 0:
                return agent_id, lines, "--lines must be a positive integer"
            i += 2
            continue
        return agent_id, lines, f"unexpected argument: {tok!r}"
    return agent_id, lines, None


def make_oc_list(runtime: "Runtime") -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        tokens = (raw_args or "").split()
        include_archived = False
        unknown: list[str] = []
        for tok in tokens:
            if tok in ("--archived", "--all", "-a"):
                include_archived = True
            else:
                unknown.append(tok)
        if unknown:
            return f"unknown arg(s): {' '.join(unknown)}\nusage: /oc list [--archived]"
        agents = sorted(runtime.agents.list(), key=lambda a: a.created_at)
        return _fmt_list(agents, include_archived=include_archived)
    return handler


def make_oc_doctor(runtime: "Runtime") -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        return _fmt_doctor(runtime)
    return handler


def _fmt_doctor(runtime: "Runtime") -> str:
    from . import event_loop as eloop
    import importlib.util
    import shutil as shutil_mod

    cfg = runtime.config
    agents = runtime.agents.list()
    projects = runtime.projects.list()
    phase_counts: dict[str, int] = {}
    for a in agents:
        phase_counts[a.phase] = phase_counts.get(a.phase, 0) + 1
    questions, permissions = eloop.get_pending_snapshot()
    pending_q_count = sum(len(v) for v in questions.values())
    pending_p_count = sum(len(v) for v in permissions.values())

    lines: list[str] = ["plugin health · hermes-opencode"]

    plugin_version = "?"
    try:
        import yaml
        manifest = yaml.safe_load((cfg.projects_file.parent.parent.parent / "config.yaml").read_text()) or {}
    except Exception:
        manifest = {}
    try:
        from . import __file__ as init_path  # type: ignore
        import yaml
        plug_yaml = Path(init_path).parent / "plugin.yaml"
        if plug_yaml.exists():
            plugin_version = (yaml.safe_load(plug_yaml.read_text()) or {}).get("version", "?")
    except Exception:
        pass

    lines.append(f"  version              · {plugin_version}")
    lines.append(f"  state dir            · {cfg.projects_file.parent}")
    lines.append(f"  opencode server      · {cfg.server_url}")

    bg_alive = eloop._thread is not None and eloop._thread.is_alive()
    lines.append(f"  bg event loop alive  · {'yes' if bg_alive else 'NO'}")

    lines.append(f"  projects registered  · {len(projects)}")
    if phase_counts:
        phase_summary = ", ".join(f"{p}={n}" for p, n in sorted(phase_counts.items()))
        lines.append(f"  agents               · {len(agents)} ({phase_summary})")
    else:
        lines.append(f"  agents               · 0")
    lines.append(f"  pending questions    · {pending_q_count}")
    lines.append(f"  pending permissions  · {pending_p_count}")

    sinks = ",".join(cfg.notify_sinks) or "(none)"
    target = "(unset)"
    if cfg.notify_gateway_platform and cfg.notify_gateway_chat_id:
        target = f"{cfg.notify_gateway_platform}:{cfg.notify_gateway_chat_id}"
    elif cfg.notify_gateway_platform or cfg.notify_gateway_chat_id:
        target = f"{cfg.notify_gateway_platform or '?'}:{cfg.notify_gateway_chat_id or '?'}"
    lines.append(f"  notify sinks         · {sinks}")
    lines.append(f"  notify gateway       · {target}")
    if cfg.notify_discovery_source:
        lines.append(f"  notify discovery     · {cfg.notify_discovery_source}")
    lines.append(f"  notify events        · {','.join(sorted(cfg.notify_events))}")
    lines.append(
        f"  heartbeat            · enabled={cfg.heartbeat_enabled} "
        f"window={cfg.heartbeat_day_start}-{cfg.heartbeat_day_end} tz={cfg.heartbeat_timezone or '(system)'}"
    )
    classifier_state = "enabled" if cfg.classifier_enabled else "disabled"
    lines.append(
        f"  classifier           · {classifier_state} task={cfg.classifier_task_name!r} "
        f"max_input={cfg.classifier_max_input_chars} timeout={cfg.classifier_timeout_sec}s"
    )
    lines.append(
        f"  awaiting input       · stall_after={int(cfg.awaiting_input_stall_timeout_sec)}s "
        f"reminder_every={int(cfg.awaiting_input_reminder_interval_sec)}s"
    )
    awaiting_agents = [a for a in agents if a.last_awaiting_notify_at]
    if awaiting_agents:
        for a in awaiting_agents:
            verdict = a.last_classifier_verdict or {}
            src = verdict.get("source", "?")
            conf = verdict.get("confidence", "?")
            awaiting = verdict.get("awaiting", "?")
            lines.append(
                f"  awaiting · {a.agent_id} · phase={a.phase} "
                f"verdict=awaiting={awaiting} src={src} conf={conf}"
            )

    deps = []
    deps.append(("opencode", shutil_mod.which("opencode")))
    deps.append(("gh", shutil_mod.which("gh")))
    deps.append(("git", shutil_mod.which("git")))
    deps.append(("bun", shutil_mod.which("bun")))
    for name, path in deps:
        lines.append(f"  binary {name:<14} · {path or 'MISSING (PATH)'}")
    for mod_name in ("httpx", "httpx_sse", "yaml"):
        ok = importlib.util.find_spec(mod_name) is not None
        lines.append(f"  python {mod_name:<14} · {'ok' if ok else 'MISSING'}")

    state_files = [
        ("projects.json", cfg.projects_file),
        ("agents.json", cfg.agents_file),
        ("notifications.jsonl", cfg.notifications_file),
        ("events.log", cfg.events_log),
    ]
    for label, p in state_files:
        if p.exists():
            try:
                size = p.stat().st_size
                lines.append(f"  file {label:<20} · {size} bytes")
            except OSError:
                lines.append(f"  file {label:<20} · (unreadable)")
        else:
            lines.append(f"  file {label:<20} · (absent)")

    if cfg.events_log.exists():
        try:
            tail_lines = cfg.events_log.read_text(encoding="utf-8").splitlines()[-3:]
            if tail_lines:
                lines.append("  last events:")
                for ln in tail_lines:
                    lines.append(f"    {ln[:160]}")
        except OSError:
            pass
    return "\n".join(lines)


def make_oc_attach(runtime: "Runtime") -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        agent_id, lines, err = _parse_oc_attach_args(raw_args)
        if err:
            return err
        agent = runtime.agents.get(agent_id)
        if agent is None:
            return f"unknown agent: {agent_id}"
        worktree = Path(agent.worktree_path)
        try:
            body = asyncio.run(runtime.client.get_messages(agent.session_id, worktree))
        except OpencodeError as e:
            return f"transport error: {e}"
        items = body.get("items") or []
        if not items:
            return "no transcript yet"
        text = _join_buffer_text(items, lines)
        if not text:
            return "no transcript yet"
        return text
    return handler


def _parse_oc_cancel_args(raw_args: str) -> tuple[str | None, str | None, str | None]:
    text = (raw_args or "").strip()
    if not text:
        return None, None, "usage: /oc cancel <agent_id> [reason ...]"
    agent_id, _, rest = text.partition(" ")
    reason = rest.strip() or None
    return agent_id, reason, None


def make_oc_cancel(runtime: "Runtime") -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        agent_id, reason, err = _parse_oc_cancel_args(raw_args)
        if err:
            return err
        agent = runtime.agents.get(agent_id)
        if agent is None:
            return f"unknown agent: {agent_id}"
        if agent.phase in {"DONE", "KILLED", "CANCELLED"}:
            return f"cannot cancel {agent_id}: already {agent.phase}"
        try:
            from . import tools as tools_mod
            spawn_fn = tools_mod.make_cancel(runtime)
            result = asyncio.run(spawn_fn({"agent_id": agent_id, "reason": reason}))
        except RuntimeError as e:
            return f"cancel failed: {e}"
        import json
        try:
            payload = json.loads(result)
        except (ValueError, TypeError):
            return result
        if not payload.get("ok"):
            return f"cancel failed: {payload.get('error', 'unknown')}"
        data = payload.get("data") or {}
        errors = data.get("errors") or []
        out = [f"cancelled {agent_id}", f"reason: {data.get('reason')}"]
        if errors:
            out.append(f"non-fatal: {'; '.join(errors)}")
        return "\n".join(out)
    return handler


def make_oc_test_notify(runtime: "Runtime") -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        from . import notify as notify_mod
        cfg = runtime.config
        body = (raw_args or "").strip() or "Gateway DM trigger test from /oc test-notify."
        title = "test-notify"
        sinks = list(dict.fromkeys(["gateway", *cfg.notify_sinks]))
        results = notify_mod.fanout(
            sinks=sinks,
            title=title,
            body=body,
            meta={"kind": "test_notify"},
            dashboard_path=cfg.notifications_file,
            gateway_platform=cfg.notify_gateway_platform,
            gateway_chat_id=cfg.notify_gateway_chat_id,
        )
        target = f"{cfg.notify_gateway_platform or '(unset)'}:{cfg.notify_gateway_chat_id or '(unset)'}"
        lines = [
            "test-notify dispatched",
            f"  title:     {title}",
            f"  body:      {body}",
            f"  sinks:     {','.join(sinks)}",
            f"  gateway:   {target}",
            f"  discovery: {cfg.notify_discovery_source or '(unknown)'}",
        ]
        for r in results:
            tag = "ok  " if r.ok else "FAIL"
            detail = f" · {r.detail}" if r.detail else ""
            lines.append(f"  [{tag}] {r.sink}{detail}")
        return "\n".join(lines)
    return handler


def make_oc_questions(runtime: "Runtime") -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        questions, _permissions = event_loop.get_pending_snapshot()
        if not questions:
            return "no pending questions"
        blocks: list[str] = []
        for agent_id, entries in sorted(questions.items()):
            for entry in entries:
                qid = entry.get("id")
                inner = entry.get("questions") or []
                for q in inner:
                    body = (q.get("question") or "").strip() or "(no body)"
                    block = [f"[{agent_id}] {qid}", body]
                    opts = q.get("options") or []
                    for o in opts:
                        if not isinstance(o, dict):
                            continue
                        label = o.get("label")
                        desc = o.get("description", "")
                        block.append(f"  - {label!r}: {desc}")
                    blocks.append("\n".join(block))
        return "\n\n".join(blocks)
    return handler


_OC_HELP_TEXT = (
    "/oc - hermes-opencode slash command\n"
    "\n"
    "subcommands:\n"
    "  /oc list [--archived]                 list tracked agents (one line per agent, status + age + pr_url); --archived also shows archived\n"
    "  /oc attach <agent_id> [--lines N]     print the last N (default 80) lines of an agent's transcript\n"
    "  /oc questions                         list pending opencode questions awaiting a human answer\n"
    "  /oc cancel <agent_id> [reason ...]    wind down an agent without merging; keeps record as CANCELLED\n"
    "  /oc doctor                            plugin health report (versions, bg loop alive, deps, state files)\n"
    "  /oc test-notify [message ...]         force a notify fanout (gateway DM + dashboard + cli); reports per-sink ok/FAIL with detail\n"
    "  /oc help                              show this help\n"
    "\n"
    "for richer ops outside an active chat session, use the `hermes oco` CLI subcommand."
)


def make_oc_dispatcher(runtime: "Runtime") -> Callable[[str], str]:
    list_fn = make_oc_list(runtime)
    attach_fn = make_oc_attach(runtime)
    questions_fn = make_oc_questions(runtime)
    doctor_fn = make_oc_doctor(runtime)
    cancel_fn = make_oc_cancel(runtime)
    test_notify_fn = make_oc_test_notify(runtime)
    subcommands = {
        "list": list_fn,
        "attach": attach_fn,
        "questions": questions_fn,
        "doctor": doctor_fn,
        "cancel": cancel_fn,
        "test-notify": test_notify_fn,
    }

    def handler(raw_args: str) -> str:
        text = (raw_args or "").strip()
        if not text or text in {"help", "-h", "--help"}:
            return _OC_HELP_TEXT
        head, _, rest = text.partition(" ")
        sub = head.lower()
        fn = subcommands.get(sub)
        if fn is None:
            return f"unknown /oc subcommand: {head!r}\n\n{_OC_HELP_TEXT}"
        return fn(rest.strip())

    return handler
