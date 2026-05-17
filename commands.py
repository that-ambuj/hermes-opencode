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


def _fmt_table(agents: list["Agent"], now_ts: float | None = None) -> str:
    if not agents:
        return "no agents tracked"
    now_ts = now_ts if now_ts is not None else time.time()
    headers = ("agent_id", "project", "branch", "phase", "pr", "age")
    rows: list[tuple[str, ...]] = [headers]
    for a in agents:
        pr = str(a.pr_number) if a.pr_number else "-"
        age = _format_age(now_ts - a.last_activity_at)
        rows.append((a.agent_id, a.project_label, a.branch, a.phase, pr, age))
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    lines = []
    for idx, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        lines.append(line)
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


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
        agents = sorted(runtime.agents.list(), key=lambda a: a.created_at)
        return _fmt_table(agents)
    return handler


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
    "/oc — opencode-orchestrator slash command\n"
    "\n"
    "subcommands:\n"
    "  /oc list                              list tracked agents (id, project, branch, phase, pr, age)\n"
    "  /oc attach <agent_id> [--lines N]     print the last N (default 80) lines of an agent's transcript\n"
    "  /oc questions                         list pending opencode questions awaiting a human answer\n"
    "  /oc help                              show this help\n"
    "\n"
    "for richer ops outside an active chat session, use the `hermes oco` CLI subcommand."
)


def make_oc_dispatcher(runtime: "Runtime") -> Callable[[str], str]:
    list_fn = make_oc_list(runtime)
    attach_fn = make_oc_attach(runtime)
    questions_fn = make_oc_questions(runtime)
    subcommands = {
        "list": list_fn,
        "attach": attach_fn,
        "questions": questions_fn,
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
