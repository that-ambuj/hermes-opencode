from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from . import notify
from .state import Agent

if TYPE_CHECKING:
    from .tools import Runtime

logger = logging.getLogger("hermes_opencode.heartbeat")

TERMINAL_PHASES = {"DONE", "KILLED", "FAILED"}
PR_OPEN_PHASES = {"PR_OPEN"}
DONE_VISIBLE_SEC = 4 * 3600.0


def _resolve_tz(name: str | None) -> tzinfo | None:
    candidates = [
        name,
        os.environ.get("HERMES_TIMEZONE"),
        os.environ.get("TZ"),
    ]
    for c in candidates:
        if not c:
            continue
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(c)
        except Exception:
            continue
    return None


def _phase_glyph(phase: str) -> str:
    return {
        "EXECUTING": "▶",
        "EXECUTOR_ADDRESSING": "▶",
        "AWAITING_HUMAN": "✋",
        "NEEDS_INTERVENTION": "🛟",
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
    }.get(phase, "•")


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m:02d}min"


def _visible_done(agent: Agent, now_ts: float) -> bool:
    if agent.phase != "DONE":
        return False
    if not agent.done_at:
        return False
    return (now_ts - agent.done_at) < DONE_VISIBLE_SEC


def build_report(runtime: "Runtime", now_local: datetime) -> tuple[bool, str]:
    agents = runtime.agents.list()
    now_ts = time.time()
    visible = [a for a in agents if a.phase not in {"KILLED", "FAILED"} and (a.phase != "DONE" or _visible_done(a, now_ts))]

    active = [a for a in visible if a.phase not in TERMINAL_PHASES and a.phase != "PR_OPEN"]
    awaiting = [a for a in visible if a.phase in {"IDLE_TASK_COMPLETE", "IDLE_REVIEW_ADDRESSED"}]
    reviewing = [a for a in visible if a.phase in {"REVIEWING", "REVIEW_SPAWNING", "REVIEW_DELIVERED"}]
    pr_open = [a for a in visible if a.phase in PR_OPEN_PHASES]
    done_recent = [a for a in visible if a.phase == "DONE"]

    has_pending = bool(active or reviewing or pr_open)
    should_send = (runtime.config.heartbeat_day_start <= now_local.hour <= runtime.config.heartbeat_day_end) or has_pending
    if not should_send:
        return False, ""

    header = (
        f"Hermes • {now_local.strftime('%H:%M %Z')}\n"
        f"Active {len(active)}  •  Reviewing {len(reviewing)}  •  PR open {len(pr_open)}  •  Done (recent) {len(done_recent)}"
    )
    if not visible:
        return should_send, header + "\n\n(no agents tracked)"

    rows: list[str] = []
    for a in visible:
        glyph = _phase_glyph(a.phase)
        age = _format_age(now_ts - a.last_activity_at)
        suffix_parts: list[str] = [a.phase, age]
        if a.pr_url:
            suffix_parts.append(a.pr_url)
        if a.phase == "DONE" and a.done_at:
            suffix_parts.append(f"merged {_format_age(now_ts - a.done_at)} ago")
        if a.last_error:
            suffix_parts.append(f"ERR: {a.last_error[:120]}")
        rows.append(f"{glyph} {a.agent_id}   {a.project_label} · {a.branch}   {'  ·  '.join(suffix_parts)}")

    return should_send, header + "\n\n" + "\n".join(rows)


def send_heartbeat(runtime: "Runtime", *, force: bool = False) -> dict:
    tz = _resolve_tz(runtime.config.heartbeat_timezone)
    now_local = datetime.now(tz) if tz else datetime.now()
    should_send, body = build_report(runtime, now_local)
    if not should_send and not force:
        return {"sent": False, "reason": "outside_window_and_no_pending"}
    if force and not body:
        body = "(no agents)"
    results = notify.fanout(
        sinks=runtime.config.notify_sinks,
        title="heartbeat",
        body=body,
        meta={"kind": "heartbeat", "when": now_local.isoformat()},
        dashboard_path=runtime.config.notifications_file,
        gateway_platform=runtime.config.notify_gateway_platform,
        gateway_chat_id=runtime.config.notify_gateway_chat_id,
    )
    return {
        "sent": True,
        "when": now_local.isoformat(),
        "sinks": [{"sink": r.sink, "ok": r.ok, "detail": r.detail} for r in results],
        "body": body,
    }


def next_top_of_hour(now_local: datetime) -> float:
    nxt = (now_local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (nxt - now_local).total_seconds()
