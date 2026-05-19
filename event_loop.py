from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from datetime import datetime
from . import awaiting_input as awaiting_input_mod
from . import heartbeat as heartbeat_mod
from . import pr as pr_mod
from . import reviewer as reviewer_mod
from . import worktree as wt_mod
from .state import Agent
from .transport import OpencodeError

if TYPE_CHECKING:
    from .tools import Runtime

logger = logging.getLogger("hermes_opencode.event_loop")

from .state import TERMINAL_PHASES

IDLE_DEBOUNCE_SEC = 120.0
PR_POLL_SEC = 300.0
PRUNE_INTERVAL_SEC = 60.0
ARCHIVE_AFTER_SEC = 12 * 3600.0
CLEANUP_INTERVAL_SEC = 12 * 3600.0
EVENTS_LOG_MAX_LINES = 5000
NOTIFICATIONS_MAX_LINES = 1000
HISTORY_RETAIN_DAYS = 30.0
PR_OPEN_TIMEOUT_SEC = 900.0
PR_OPENED_RE_PATTERN = r"PR_OPENED:\s*(https?://[^\s]+/pull/\d+)"
PR_URL_FALLBACK_RE_PATTERN = r"https?://github\.com/[^\s]+/pull/(\d+)"
SERVE_WATCHDOG_INTERVAL_SEC = 60.0
SERVE_RESTART_MAX_ATTEMPTS = 5
SERVE_RESTART_BACKOFF_BASE_SEC = 1.0
SERVE_DOWN_NOTIFY_COOLDOWN_SEC = 600.0
SERVE_DOWN_NOTIFY_SINKS = ("cli", "dashboard", "gateway")
SERVE_HEALING_GRACE_SEC = 30.0
TICK_FAILURE_ESCALATION_THRESHOLD = 3
ABORT_ESCALATION_THRESHOLD = 3
ABORT_AUTO_CONTINUE_MESSAGE = "continue"
ABORT_NUDGE_PROMPTS = (
    "continue",
    "You stopped mid-task. Resume where you left off and finish the work.",
    "[SYSTEM DIRECTIVE: HERMES-OPENCODE - RESUME]\n"
    "Your previous turn aborted. The orchestrator is one strike away from "
    "escalating this agent to FAILED. Resume the task from where you left "
    "off and drive it to a clean stopping point (READY_FOR_REVIEW or a "
    "/question).\n"
    "[END SYSTEM DIRECTIVE]",
)
RATE_LIMIT_MIN_WAIT_SEC = 30.0
RATE_LIMIT_MAX_TICK_WAIT_SEC = 15.0
QUEUE_POLL_SEC = 5.0
PHASE_RETRY_CEILING = {
    "QUEUED": 5,
    "BOOTSTRAPPING": 3,
    "REVIEW_SPAWNING": 5,
    "REVIEWING": 3,
    "REVIEW_DELIVERED": 5,
    "COMMITTING": 3,
}
PHASE_RETRY_CEILING_DEFAULT = 3
STUCK_WARN_SEC = 600.0
STUCK_CHECK_INTERVAL_SEC = 60.0
STUCK_WATCHED_PHASES = frozenset({
    "CREATED",
    "BOOTSTRAPPING",
    "IDLE_TASK_COMPLETE",
    "REVIEW_SPAWNING",
    "REVIEW_DELIVERED",
    "IDLE_REVIEW_ADDRESSED",
    "COMMITTING",
})

_state_lock = threading.Lock()
_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_stop_flag = threading.Event()
_runtime: "Runtime | None" = None
_agent_tasks: dict[str, dict[str, asyncio.Task]] = {}
_sse_stop_events: dict[str, asyncio.Event] = {}
_question_snapshot: dict[str, list[dict]] = {}
_permission_snapshot: dict[str, list[dict]] = {}
_snapshot_lock = threading.Lock()

_notified_questions: dict[str, set[str]] = {}
_notified_permissions: dict[str, set[str]] = {}
_notified_phases: dict[str, set[str]] = {}
_notify_dedup_lock = threading.Lock()
AWAITING_INPUT_REMINDER_TICK_SEC = 60.0

_serve_seen_alive: bool = False
_serve_down_notified_at: float = 0.0
_serve_recovered_at: float = 0.0
_unhealthy_tick_notified_agents: set[str] = set()
_last_serve_crash_info: dict | None = None
_last_serve_alive_seen_at: float = 0.0


def _serve_is_unhealthy_or_healing() -> bool:
    if _serve_down_notified_at != 0.0:
        return True
    if _serve_recovered_at != 0.0:
        if time.time() - _serve_recovered_at < SERVE_HEALING_GRACE_SEC:
            return True
    return False


_EVENT_GLYPH = {
    "pr_opened": "🔗",
    "done": "✓",
    "failed": "✗",
    "awaiting_human": "⏸",
    "review_started": "🔎",
    "cancelled": "🚫",
    "tick_error": "⚠",
    "aborted": "⏹",
    "rate_limited": "⏳",
    "rate_limit_cleared": "▶",
    "queued": "⏳",
    "queue_drained": "▶",
    "awaiting_human_resumed": "▶",
    "needs_intervention": "🛟",
    "phase_stuck": "🐌",
    "progress_narration": "💭",
}


def _notify_event(agent: "Agent", kind: str, body: str = "") -> None:
    if _runtime is None:
        return
    from . import notify as notify_mod
    if kind not in _runtime.config.notify_events:
        return
    glyph = _EVENT_GLYPH.get(kind, "•")
    title = f"{glyph} {agent.agent_id}  {kind.replace('_', ' ')}"
    final_body = body or _default_event_body(agent, kind)
    try:
        _runtime.config.events_log.parent.mkdir(parents=True, exist_ok=True)
        import json
        line = json.dumps({
            "ts": time.time(),
            "kind": kind,
            "agent_id": agent.agent_id,
            "project": agent.project_label,
            "phase": agent.phase,
            "pr_url": agent.pr_url,
            "title": title,
            "body": final_body,
        }, default=str)
        with _runtime.config.events_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("events.log write failed: %s", e)
    try:
        notify_mod.fanout(
            sinks=_runtime.config.notify_sinks,
            title=title,
            body=final_body,
            meta={"kind": kind, "agent_id": agent.agent_id, "phase": agent.phase, "pr_url": agent.pr_url},
            dashboard_path=_runtime.config.notifications_file,
            gateway_platform=_runtime.config.notify_gateway_platform,
            gateway_chat_id=_runtime.config.notify_gateway_chat_id,
        )
        logger.info("notify event %s for %s dispatched to sinks=%s", kind, agent.agent_id, _runtime.config.notify_sinks)
    except Exception as e:
        logger.warning("notify event %s for %s failed: %s", kind, agent.agent_id, e)


def _default_event_body(agent: "Agent", kind: str) -> str:
    if kind == "pr_opened":
        return f"PR opened for `{agent.branch}`: {agent.pr_url or '(url unknown)'}"
    if kind == "done":
        return f"PR merged. Branch `{agent.branch}`. Worktree cleaned up."
    if kind == "failed":
        return f"Agent transitioned to FAILED. Last error: {agent.last_error or '(no detail)'}"
    if kind == "awaiting_human":
        return f"Agent paused, awaiting human input. Reply in chat to forward."
    if kind == "review_started":
        return f"Executor finished its turn. Reviewer session staged on `{agent.branch}` for a code-review pass."
    if kind == "cancelled":
        reason = agent.cancellation_reason or "(no reason recorded)"
        return f"Agent cancelled. Reason: {reason}. Worktree cleaned up."
    if kind == "tick_error":
        return f"Tick failed: {agent.last_tick_error or '(no detail)'}"
    if kind == "aborted":
        return f"Executor turn aborted. Last error: {agent.last_error or '(no detail)'}"
    if kind == "rate_limited":
        return f"Agent rate-limited by provider. New tasks queued until clear."
    if kind == "rate_limit_cleared":
        return f"Rate limit cleared; agent resumed."
    if kind == "queued":
        return f"Task queued; waiting for rate-limited agents to clear."
    if kind == "queue_drained":
        return f"Queue drained: agent started."
    if kind == "awaiting_human_resumed":
        return f"Human reply received; agent resumed."
    if kind == "needs_intervention":
        return (
            f"Agent needs operator intervention. Reason: {agent.intervention_reason or 'unknown'}. "
            f"Last error: {agent.last_error or '(no detail)'}. "
            f"Run `oc_retry {agent.agent_id}` once the underlying issue is resolved."
        )
    if kind == "phase_stuck":
        return (
            f"Agent stuck in phase={agent.phase}. Last error: "
            f"{agent.last_error or '(no detail)'}. Inspect with `oc_get` or "
            f"kick with `oc_retry {agent.agent_id}`."
        )
    if kind == "progress_narration":
        return f"Agent {agent.agent_id} is working in phase={agent.phase}."
    return f"{kind} for {agent.agent_id}"


def _maybe_notify_phase(agent: "Agent", kind: str, body: str = "") -> None:
    """Notify only once per (agent_id, kind) — prevents replay on tick re-entry."""
    with _notify_dedup_lock:
        seen = _notified_phases.setdefault(agent.agent_id, set())
        if kind in seen:
            return
        seen.add(kind)
    _notify_event(agent, kind, body)


def _format_question_block(q: dict) -> str:
    qid = q.get("id")
    inners = q.get("questions") or []
    if not inners:
        return f"[{qid}] (no questions)"
    total = len(inners)
    chunks: list[str] = []
    for idx, inner in enumerate(inners, start=1):
        text = (inner.get("question") or "").strip() or "(no body)"
        header = (inner.get("header") or "").strip()
        opts = inner.get("options") or []
        bullets = [
            f"  - {o.get('label')!r}: {o.get('description', '')}"
            for o in opts if isinstance(o, dict)
        ]
        prefix = f"[{qid}]" if total == 1 else f"[{qid} #{idx}/{total}]"
        head = f"{header}: " if header else ""
        chunk = f"{prefix} {head}{text}"
        if bullets:
            chunk += "\n" + "\n".join(bullets)
        chunks.append(chunk)
    return "\n\n".join(chunks)


def _format_permission_block(p: dict) -> str:
    pid = p.get("id")
    ptype = p.get("permission") or p.get("type") or "(unknown)"
    patterns = p.get("patterns") or []
    detail = f"type={ptype!r}"
    if patterns:
        detail += f" patterns={patterns}"
    return f"[{pid}] {detail}\n  Reply via the opencode CLI/web UI (`opencode permission reply {pid} <once|always|reject>` or the running TUI prompt)."


async def _resume_from_awaiting_human(agent: "Agent", reason: str = "human reply received") -> "Agent":
    """If `agent` is currently in AWAITING_HUMAN, restore the saved
    `phase_before_awaiting` (default EXECUTING), clear the awaiting
    bookkeeping fields, and fire `awaiting_human_resumed`. No-op when
    the agent is not in AWAITING_HUMAN. Designed to be called from
    every human-input dispatch surface (oc_answer, oc_send,
    @<agent_id>) so the dashboard sees forward progress immediately
    instead of waiting for the next _phase_awaiting_human poll tick.
    """
    if _runtime is None:
        return agent
    current = _runtime.agents.get(agent.agent_id) or agent
    if current.phase != "AWAITING_HUMAN":
        return current
    restored = current.phase_before_awaiting or "EXECUTING"
    duration = time.time() - (current.awaiting_human_since or time.time())
    refreshed = _runtime.agents.update(
        current.agent_id,
        phase=restored,
        phase_before_awaiting=None,
        awaiting_human_since=None,
        awaiting_entry_message_id=None,
        awaiting_entry_had_pending_qp=False,
    )
    _notify_event(
        refreshed, "awaiting_human_resumed",
        f"{reason} after {_humanize_seconds(duration)}; agent resumed at phase={restored}.",
    )
    return refreshed


async def _enter_awaiting_human(
    agent: "Agent",
    body: str,
    *,
    had_pending_qp: bool,
) -> "Agent":
    """Transition agent into the AWAITING_HUMAN phase and fire the
    `awaiting_human` notification with `body`. v0.16.0 promoted
    `awaiting_human` from a recurring event-only signal to a proper phase
    so dashboards can show it as a distinct state instead of leaving the
    agent in EXECUTING (which looks active).

    v0.16.2 captures `awaiting_entry_message_id` (latest assistant
    message.id at entry) and `awaiting_entry_had_pending_qp` (whether
    the trigger was an opencode /question or /permission, vs a pure
    prose-question classifier hit) so the poll exit logic can demand
    an authoritative forward-progress signal rather than trusting a
    classifier that may flip non-deterministically across ticks.

    Idempotent: if already in AWAITING_HUMAN, only re-fires the event
    (reminder-style) without resetting `awaiting_human_since`,
    `phase_before_awaiting`, or entry-tracking fields. The first
    trigger wins.
    """
    if _runtime is None:
        return agent
    now = time.time()
    if agent.phase == "AWAITING_HUMAN":
        updated = _runtime.agents.update(
            agent.agent_id, last_awaiting_notify_at=now,
        )
    else:
        entry_mid = await _fetch_last_assistant_message_id(agent)
        updated = _runtime.agents.update(
            agent.agent_id,
            phase="AWAITING_HUMAN",
            phase_before_awaiting=agent.phase,
            awaiting_human_since=now,
            last_awaiting_notify_at=now,
            awaiting_entry_message_id=entry_mid,
            awaiting_entry_had_pending_qp=had_pending_qp,
        )
    _notify_event(updated, "awaiting_human", body)
    return updated


async def _maybe_notify_new_pending(
    agent: "Agent",
    pending_q: list[dict],
    pending_p: list[dict],
    context_text: str | None = None,
) -> bool:
    with _notify_dedup_lock:
        q_seen = _notified_questions.setdefault(agent.agent_id, set())
        new_q_ids = [q.get("id") for q in pending_q if q.get("id") and q.get("id") not in q_seen]
        for qid in new_q_ids:
            if qid:
                q_seen.add(qid)
        p_seen = _notified_permissions.setdefault(agent.agent_id, set())
        new_p_ids = [p.get("id") for p in pending_p if p.get("id") and p.get("id") not in p_seen]
        for pid in new_p_ids:
            if pid:
                p_seen.add(pid)
    if not new_q_ids and not new_p_ids:
        return False

    sections: list[str] = []
    if context_text:
        sections.append(f"Context (last assistant text):\n{context_text}")
    q_blocks = [_format_question_block(q) for q in pending_q if q.get("id") in new_q_ids]
    if q_blocks:
        sections.append("Pending questions:\n\n" + "\n\n".join(q_blocks))
    p_blocks = [_format_permission_block(p) for p in pending_p if p.get("id") in new_p_ids]
    if p_blocks:
        sections.append("Pending permission requests:\n\n" + "\n\n".join(p_blocks))
    await _enter_awaiting_human(agent, "\n\n".join(sections), had_pending_qp=True)
    return True


async def _maybe_notify_awaiting_classified(
    agent: "Agent",
    check: "awaiting_input_mod.AwaitingInputCheck",
) -> None:
    body = (
        f"Detector: {check.source} (confidence={check.confidence})\n"
        f"Reason: {check.reason}\n\n"
        f"Last assistant text:\n{check.last_assistant_text}"
    )
    await _enter_awaiting_human(agent, body, had_pending_qp=False)


async def _fetch_last_assistant_text(agent: "Agent") -> str:
    sse = get_text_buffer(agent.agent_id)
    if sse:
        joined = "\n".join(v for v in sse.values() if v).strip()
        if joined:
            return joined
    if _runtime is None:
        return ""
    try:
        body = await _runtime.client.get_messages(agent.session_id, Path(agent.worktree_path))
    except OpencodeError:
        return ""
    items = body.get("items") or []
    parts_text: list[str] = []
    for item in reversed(items):
        message = item.get("message") or {}
        if message.get("role") != "assistant" and message.get("type") != "assistant":
            continue
        for part in reversed(item.get("parts") or []):
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts_text.append(text)
        if parts_text:
            break
    return "\n".join(reversed(parts_text)).strip()


async def _fetch_last_assistant_message_id(agent: "Agent") -> str | None:
    if _runtime is None:
        return None
    try:
        body = await _runtime.client.get_messages(agent.session_id, Path(agent.worktree_path))
    except OpencodeError:
        return None
    items = body.get("items") or []
    for item in reversed(items):
        message = item.get("message") or {}
        if message.get("role") != "assistant" and message.get("type") != "assistant":
            continue
        mid = message.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None


def _has_incomplete_todos(items: list[dict]) -> bool | None:
    for item in reversed(items):
        for part in reversed(item.get("parts") or []):
            if not isinstance(part, dict):
                continue
            if part.get("type") != "tool" or part.get("tool") != "todowrite":
                continue
            state = part.get("state") or {}
            status = state.get("status")
            if status in ("pending", "running"):
                return True
            if status != "completed":
                continue
            input_ = state.get("input") or {}
            todos = input_.get("todos")
            if not isinstance(todos, list):
                return False
            for todo in todos:
                if isinstance(todo, dict) and todo.get("status") != "completed":
                    return True
            return False
    return None


async def _fetch_incomplete_todos(agent: "Agent") -> bool | None:
    if _runtime is None:
        return None
    try:
        body = await _runtime.client.get_messages(agent.session_id, Path(agent.worktree_path))
    except OpencodeError:
        return None
    items = body.get("items") or []
    return _has_incomplete_todos(items)


async def _awaiting_input_blocks_review(agent: "Agent") -> bool:
    if _runtime is None:
        return False
    last_text = await _fetch_last_assistant_text(agent)
    if not last_text:
        return False
    incomplete = await _fetch_incomplete_todos(agent)
    check = await awaiting_input_mod.check(
        _runtime, last_text, has_incomplete_todos=incomplete,
    )
    _runtime.agents.update(
        agent.agent_id,
        last_classifier_verdict=awaiting_input_mod.to_dict(check),
    )
    if not check.awaiting:
        return False
    await _maybe_notify_awaiting_classified(agent, check)
    _runtime.agents.update(agent.agent_id, last_awaiting_notify_at=time.time())
    return True
_sse_text_buffers: dict[str, dict[str, str]] = {}
# Per-agent {part_id: type} map populated from `message.part.updated` events.
# Required because opencode emits `message.part.delta` with `field="text"` for
# BOTH text parts and reasoning parts; the only reliable way to tell them apart
# is the part type carried by the preceding `message.part.updated` event.
_sse_part_types: dict[str, dict[str, str]] = {}
# Per-agent {message_id: role} map populated from `message.updated` events.
# Required because the SSE stream emits part events for user messages too
# (their text parts arrive via `message.part.updated`), so without this map
# user prompts would leak into the assistant-text buffer.
_sse_message_roles: dict[str, dict[str, str]] = {}
_sse_buffer_lock = threading.Lock()


class SessionStatusCache:
    """Single source of truth for the opencode session.status of each agent.

    Two authoritative writers:
      - SSE consumer: writes on every `session.status` event from opencode's
        `/event` stream. Sub-second latency when connected.
      - HTTP poller: writes `{"type": "idle"}` after every successful
        `wait_idle` call. Backstop for SSE drops (e.g. after a serve flap:
        opencode does not re-emit `session.status: idle` for sessions that
        were already idle before the disconnect, so the only way to refresh
        the cache after reconnect is to write through from the next
        authoritative `wait_idle`).

    One reader: `_session_status_is_idle`, which gates the EXECUTING ->
    IDLE_TASK_COMPLETE transition.

    Last write wins. Permissive on absence (`get` returns None;
    `_session_status_is_idle` then returns True). Source + timestamp are
    tracked for diagnostic surfaces (oc_status).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[str, dict] = {}
        self._sources: dict[str, str] = {}
        self._updated_at: dict[str, float] = {}

    def update(self, agent_id: str, status: dict, *, source: str) -> None:
        with self._lock:
            self._statuses[agent_id] = dict(status)
            self._sources[agent_id] = source
            self._updated_at[agent_id] = time.time()

    def get(self, agent_id: str) -> dict | None:
        with self._lock:
            s = self._statuses.get(agent_id)
            return dict(s) if s else None

    def get_full(self, agent_id: str) -> tuple[dict | None, str | None, float | None]:
        with self._lock:
            s = self._statuses.get(agent_id)
            return (
                dict(s) if s else None,
                self._sources.get(agent_id),
                self._updated_at.get(agent_id),
            )

    def clear(self, agent_id: str) -> None:
        with self._lock:
            self._statuses.pop(agent_id, None)
            self._sources.pop(agent_id, None)
            self._updated_at.pop(agent_id, None)


_session_status_cache = SessionStatusCache()

# Per-agent {agent_id: snippet} dedupe map for the progress-narration loop.
# Snippet is the first N chars of the latest SSE buffer; only fires a
# notification when it differs from the last fired snippet, so a slow
# executor doesn't spam the user with identical "still working" pings.
_last_narrated_snippets: dict[str, str] = {}


def apply_delta(buffers: dict[str, str], part_id: str, delta: str) -> dict[str, str]:
    buffers[part_id] = buffers.get(part_id, "") + delta
    return buffers


def apply_snapshot(buffers: dict[str, str], part_id: str, text: str) -> dict[str, str]:
    buffers[part_id] = text
    return buffers


def get_text_buffer(agent_id: str) -> dict[str, str]:
    with _sse_buffer_lock:
        return dict(_sse_text_buffers.get(agent_id) or {})


def get_session_status(agent_id: str) -> dict | None:
    return _session_status_cache.get(agent_id)


def get_session_status_full(agent_id: str) -> tuple[dict | None, str | None, float | None]:
    """Returns (status, source, updated_at) for diagnostic surfaces."""
    return _session_status_cache.get_full(agent_id)


def clear_text_buffer(agent_id: str) -> None:
    with _sse_buffer_lock:
        _sse_text_buffers.pop(agent_id, None)
        _sse_part_types.pop(agent_id, None)
        _sse_message_roles.pop(agent_id, None)
    _session_status_cache.clear(agent_id)


def get_pending_snapshot() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    with _snapshot_lock:
        return ({k: list(v) for k, v in _question_snapshot.items()},
                {k: list(v) for k, v in _permission_snapshot.items()})


def _update_snapshots(agent_id: str, questions: list[dict], permissions: list[dict]) -> None:
    with _snapshot_lock:
        if questions:
            _question_snapshot[agent_id] = questions
        else:
            _question_snapshot.pop(agent_id, None)
        if permissions:
            _permission_snapshot[agent_id] = permissions
        else:
            _permission_snapshot.pop(agent_id, None)


def _drop_snapshots(agent_id: str) -> None:
    with _snapshot_lock:
        _question_snapshot.pop(agent_id, None)
        _permission_snapshot.pop(agent_id, None)


def start(runtime: "Runtime") -> None:
    global _thread, _runtime
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            _runtime = runtime
            return
        _runtime = runtime
        _stop_flag.clear()
        _thread = threading.Thread(target=_thread_main, name="oc-orch-loop", daemon=True)
        _thread.start()


def stop(timeout: float = 5.0) -> None:
    global _thread, _loop
    with _state_lock:
        thread = _thread
        loop = _loop
    if thread is None:
        return
    _stop_flag.set()
    if loop is not None:
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
    thread.join(timeout=timeout)
    with _state_lock:
        _thread = None
        _loop = None
        _agent_tasks.clear()
        _sse_stop_events.clear()


def ensure_agent_task(agent_id: str) -> None:
    with _state_lock:
        loop = _loop
    if loop is None or not loop.is_running():
        return
    fut = asyncio.run_coroutine_threadsafe(_ensure_agent_task_async(agent_id), loop)
    try:
        fut.result(timeout=2.0)
    except Exception:
        pass


def schedule(coro_factory) -> asyncio.Future | None:
    with _state_lock:
        loop = _loop
    if loop is None or not loop.is_running():
        return None
    return asyncio.run_coroutine_threadsafe(coro_factory(), loop)


def run_blocking(coro_factory, *, timeout: float = 60.0):
    """Run a coroutine to completion from a sync context, returning its result.

    Used by sync slash-command and gateway-dispatch handlers that need to
    await an opencode HTTP call from a code path that may already be inside
    a running asyncio loop (e.g. hermes' main session loop dispatching a
    pre_gateway_dispatch hook or a registered slash command).

    Prefers the plugin's background event loop (always running once
    ``start(runtime)`` has been called) via ``run_coroutine_threadsafe`` so
    we never call ``asyncio.run`` from inside a running loop (that raises
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``), which is the bug class this helper exists to prevent.

    Falls back to ``asyncio.run`` only when (a) the bg loop is not running
    (e.g. the standalone ``hermes oco`` CLI path that builds its own
    Runtime without calling ``event_loop.start``) AND (b) the caller is
    not already inside a running event loop.

    ``coro_factory`` is a zero-arg callable returning a fresh awaitable;
    matches the ``schedule()`` API so a coroutine is never created in a
    context that won't consume it.

    Raises ``RuntimeError`` only when the caller is inside a running loop
    AND the bg loop is unavailable: an exceptional configuration that
    indicates the plugin was not registered correctly.
    """
    with _state_lock:
        loop = _loop
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return fut.result(timeout=timeout)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    raise RuntimeError(
        "run_blocking: caller is inside a running event loop and the "
        "hermes-opencode background loop is not running. Ensure "
        "event_loop.start(runtime) was called during plugin register()."
    )


async def _ensure_agent_task_async(agent_id: str) -> None:
    if _runtime is None:
        return
    existing = _agent_tasks.get(agent_id) or {}
    loop_task = existing.get("agent")
    sse_task = existing.get("sse")
    new_tasks: dict[str, asyncio.Task] = {}
    if loop_task and not loop_task.done():
        new_tasks["agent"] = loop_task
    else:
        new_tasks["agent"] = asyncio.create_task(_agent_loop(agent_id))
    if sse_task and not sse_task.done():
        new_tasks["sse"] = sse_task
    else:
        stop_event = asyncio.Event()
        _sse_stop_events[agent_id] = stop_event
        new_tasks["sse"] = asyncio.create_task(_sse_consumer_loop(agent_id, stop_event))
    _agent_tasks[agent_id] = new_tasks


def _cancel_agent_tasks(agent_id: str) -> None:
    tasks = _agent_tasks.pop(agent_id, None) or {}
    stop_event = _sse_stop_events.pop(agent_id, None)
    if stop_event is not None:
        stop_event.set()
    for t in tasks.values():
        if not t.done():
            t.cancel()
    clear_text_buffer(agent_id)


def _thread_main() -> None:
    global _loop
    loop = asyncio.new_event_loop()
    with _state_lock:
        _loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_supervisor())
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()


async def _supervisor() -> None:
    pruner = asyncio.create_task(_pruner_loop())
    heartbeat = asyncio.create_task(_heartbeat_loop())
    cleanup = asyncio.create_task(_cleanup_loop())
    watchdog = asyncio.create_task(_serve_watchdog_loop())
    awaiting_reminder = asyncio.create_task(_awaiting_input_reminder_loop())
    stuck_watchdog = asyncio.create_task(_phase_stuck_loop())
    narration = asyncio.create_task(_progress_narration_loop())
    try:
        while not _stop_flag.is_set():
            if _runtime is not None:
                for agent in _runtime.agents.list():
                    if agent.phase not in TERMINAL_PHASES:
                        await _ensure_agent_task_async(agent.agent_id)
            await asyncio.sleep(2.0)
    finally:
        for t in (pruner, heartbeat, cleanup, watchdog, awaiting_reminder, stuck_watchdog, narration):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


async def _cleanup_loop() -> None:
    while not _stop_flag.is_set():
        if _runtime is None:
            await asyncio.sleep(30.0)
            continue
        try:
            _run_cleanup()
        except Exception as e:
            logger.warning("cleanup tick failed: %s", e)
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


async def _awaiting_input_reminder_loop() -> None:
    while not _stop_flag.is_set():
        await asyncio.sleep(AWAITING_INPUT_REMINDER_TICK_SEC)
        if _runtime is None:
            continue
        try:
            _run_awaiting_input_reminders()
        except Exception as e:
            logger.warning("awaiting_input reminder tick failed: %s", e)


def _run_awaiting_input_reminders() -> None:
    assert _runtime is not None
    cfg = _runtime.config
    if cfg.awaiting_input_reminder_interval_sec <= 0:
        return
    now = time.time()
    interval = cfg.awaiting_input_reminder_interval_sec
    for agent in _runtime.agents.list():
        if agent.phase != "AWAITING_HUMAN":
            continue
        last_notify = agent.last_awaiting_notify_at
        if last_notify is None:
            continue
        if now - last_notify < interval:
            continue
        verdict = agent.last_classifier_verdict or {}
        snippet = str(verdict.get("last_assistant_text") or "")
        elapsed = _humanize_seconds(now - last_notify)
        reason = verdict.get("reason") or "(no reason recorded)"
        body = (
            f"Agent still awaiting human input ({elapsed} since last reminder). "
            f"Detector reason: {reason}\n\n"
            f"Last assistant text:\n{snippet}"
        ) if snippet else (
            f"Agent still awaiting human input ({elapsed} since last reminder). "
            f"Detector reason: {reason}"
        )
        _notify_event(agent, "awaiting_human", body)
        _runtime.agents.update(agent.agent_id, last_awaiting_notify_at=now)


def _humanize_seconds(s: float) -> str:
    s = max(0.0, float(s))
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return f"{h}h{m:02d}m"


def _run_cleanup() -> None:
    assert _runtime is not None
    cfg = _runtime.config
    summary: dict[str, int] = {}

    summary["events_truncated"] = _truncate_log(cfg.events_log, EVENTS_LOG_MAX_LINES)
    summary["notifications_truncated"] = _truncate_log(cfg.notifications_file, NOTIFICATIONS_MAX_LINES)
    summary["history_truncated"] = _truncate_history(cfg.logs_dir.parent / "history.jsonl", HISTORY_RETAIN_DAYS)
    summary["orphan_worktrees_removed"] = _remove_orphan_worktrees()
    summary["serve_logs_pruned"] = _prune_serve_logs()

    logger.info("cleanup tick: %s", summary)


def _truncate_log(path: Path, keep_last: int) -> int:
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    if len(lines) <= keep_last:
        return 0
    kept = lines[-keep_last:]
    try:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return len(lines) - keep_last
    except OSError:
        return 0


def _truncate_history(history_path: Path, retain_days: float) -> int:
    if not history_path.exists():
        return 0
    cutoff = time.time() - retain_days * 86400.0
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    import json as _json
    kept: list[str] = []
    dropped = 0
    for ln in lines:
        try:
            rec = _json.loads(ln)
            archived_at = rec.get("archived_at") or rec.get("done_at") or 0
            if float(archived_at) >= cutoff:
                kept.append(ln)
            else:
                dropped += 1
        except (ValueError, TypeError):
            kept.append(ln)
    if dropped == 0:
        return 0
    try:
        history_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return dropped
    except OSError:
        return 0


def _remove_orphan_worktrees() -> int:
    assert _runtime is not None
    wt_root = _runtime.config.worktrees_root
    if not wt_root.is_dir():
        return 0
    live_fs = set()
    for agent in _runtime.agents.list():
        live_fs.add(wt_mod.agent_id_to_fs(agent.agent_id))
        live_fs.add(wt_mod.agent_id_to_fs(agent.agent_id) + ".review")
    removed = 0
    for child in wt_root.iterdir():
        if not child.is_dir():
            continue
        if child.name in live_fs:
            continue
        project = None
        for a in _runtime.agents.list():
            if a.worktree_path == str(child):
                project = _runtime.projects.get(a.project_label)
                break
        try:
            if project:
                wt_mod.remove_worktree(Path(project.repo_path), child, force=True)
            else:
                import shutil as _sh
                _sh.rmtree(child, ignore_errors=True)
            removed += 1
            logger.info("cleanup: removed orphan worktree %s", child)
        except Exception as e:
            logger.warning("cleanup: could not remove orphan %s: %s", child, e)
    return removed


def _compute_serve_restart_delay(attempt: int, base: float = SERVE_RESTART_BACKOFF_BASE_SEC) -> float:
    if attempt < 1:
        attempt = 1
    return base * (2 ** (attempt - 1))


async def _serve_watchdog_loop() -> None:
    global _serve_seen_alive, _serve_down_notified_at, _last_serve_alive_seen_at
    while not _stop_flag.is_set():
        if _runtime is None:
            await asyncio.sleep(SERVE_WATCHDOG_INTERVAL_SEC)
            continue
        try:
            alive = await _runtime.client.ping()
        except Exception as e:
            logger.debug("serve watchdog ping errored: %s", e)
            alive = False
        if alive:
            was_down = _serve_down_notified_at != 0.0
            if not _serve_seen_alive:
                logger.info("opencode serve detected alive at %s; watchdog armed", _runtime.config.endpoint)
            _serve_seen_alive = True
            _last_serve_alive_seen_at = time.time()
            if was_down:
                _notify_serve_recovered()
            _serve_down_notified_at = 0.0
            await asyncio.sleep(SERVE_WATCHDOG_INTERVAL_SEC)
            continue
        now = time.time()
        cooldown_elapsed = now - _serve_down_notified_at >= SERVE_DOWN_NOTIFY_COOLDOWN_SEC
        if cooldown_elapsed:
            logger.warning("opencode serve at %s appears down", _runtime.config.endpoint)
            try:
                _record_serve_crash(observed_via="ping_failed")
            except Exception as e:
                logger.debug("crash record failed: %s", e)
            _notify_serve_down()
            _serve_down_notified_at = now
        if not _runtime.config.auto_spawn_server:
            await asyncio.sleep(SERVE_WATCHDOG_INTERVAL_SEC)
            continue
        logger.warning(
            "attempting exponential restart of opencode serve (max %d)",
            SERVE_RESTART_MAX_ATTEMPTS,
        )
        recovered = await _try_restart_serve_with_backoff()
        if recovered:
            logger.info("opencode serve recovered after watchdog restart")
            _serve_seen_alive = True
            _last_serve_alive_seen_at = time.time()
            _notify_serve_recovered()
            _serve_down_notified_at = 0.0
        await asyncio.sleep(SERVE_WATCHDOG_INTERVAL_SEC)


async def _try_restart_serve_with_backoff() -> bool:
    if _runtime is None:
        return False
    if not _runtime.config.auto_spawn_server:
        logger.warning("serve watchdog: auto_spawn_server disabled; skipping restart attempts")
        return False
    for attempt in range(1, SERVE_RESTART_MAX_ATTEMPTS + 1):
        delay = _compute_serve_restart_delay(attempt)
        logger.info(
            "serve restart attempt %d/%d: backing off %.1fs",
            attempt, SERVE_RESTART_MAX_ATTEMPTS, delay,
        )
        await asyncio.sleep(delay)
        if _stop_flag.is_set():
            return False
        try:
            await asyncio.to_thread(_runtime.client.ensure_server, 15.0, _runtime.config.logs_dir)
        except OpencodeError as e:
            logger.warning("serve restart attempt %d: ensure_server failed: %s", attempt, e)
            try:
                _record_serve_crash(
                    observed_via="restart_attempt_failed",
                    restart_attempt_n=attempt,
                    extra={"error": str(e)[:500]},
                )
            except Exception as rec_err:
                logger.debug("crash record on attempt %d failed: %s", attempt, rec_err)
            continue
        except Exception as e:
            logger.exception("serve restart attempt %d crashed: %s", attempt, e)
            try:
                _record_serve_crash(
                    observed_via="restart_attempt_exception",
                    restart_attempt_n=attempt,
                    extra={"error": f"{type(e).__name__}: {str(e)[:400]}"},
                )
            except Exception as rec_err:
                logger.debug("crash record on attempt %d failed: %s", attempt, rec_err)
            continue
        try:
            if await _runtime.client.ping():
                logger.info("serve restart attempt %d succeeded", attempt)
                return True
        except Exception as e:
            logger.debug("serve restart attempt %d post-spawn ping errored: %s", attempt, e)
        logger.warning("serve restart attempt %d: spawn returned but ping failed", attempt)
        try:
            _record_serve_crash(
                observed_via="restart_spawn_ping_failed",
                restart_attempt_n=attempt,
            )
        except Exception as rec_err:
            logger.debug("crash record on attempt %d post-ping failed: %s", attempt, rec_err)
    return False


def _record_serve_crash(
    *,
    observed_via: str,
    restart_attempt_n: int | None = None,
    exit_info: dict | None = None,
    log_tail_lines: int = 20,
    extra: dict | None = None,
) -> dict | None:
    if _runtime is None:
        return None
    import json as _json
    if exit_info is None:
        try:
            exit_info = _runtime.client.last_exit_info()
        except Exception as e:
            logger.debug("last_exit_info failed: %s", e)
            exit_info = None
    try:
        log_tail = _runtime.client.last_serve_log_tail(log_tail_lines)
    except Exception:
        log_tail = ""
    active_agents = []
    try:
        active_agents = [a.agent_id for a in _runtime.agents.list() if a.phase not in TERMINAL_PHASES]
    except Exception:
        pass
    uptime_at_alive: float | None = None
    if _last_serve_alive_seen_at:
        uptime_at_alive = max(0.0, time.time() - _last_serve_alive_seen_at)
    record = {
        "ts": time.time(),
        "endpoint": _runtime.config.endpoint,
        "observed_via": observed_via,
        "restart_attempt_n": restart_attempt_n,
        "pid": (exit_info or {}).get("pid"),
        "exit_code": (exit_info or {}).get("exit_code"),
        "signal_name": (exit_info or {}).get("signal_name"),
        "exit_kind": (exit_info or {}).get("exit_kind", "unknown_no_spawn_record"),
        "uptime_sec": (exit_info or {}).get("uptime_sec"),
        "log_path": (exit_info or {}).get("log_path"),
        "log_tail": log_tail,
        "sec_since_last_alive": uptime_at_alive,
        "agents_active": active_agents,
    }
    if extra:
        record["extra"] = extra
    path = _runtime.config.serve_crashes_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(record, default=str) + "\n")
    except OSError as e:
        logger.warning("serve_crashes.jsonl append failed: %s", e)
    global _last_serve_crash_info
    _last_serve_crash_info = record
    logger.info(
        "serve crash recorded: observed=%s exit_kind=%s exit_code=%s signal=%s",
        observed_via, record["exit_kind"], record["exit_code"], record["signal_name"],
    )
    return record


def _read_serve_crashes(limit: int = 20) -> list[dict]:
    if _runtime is None:
        return []
    import json as _json
    path = _runtime.config.serve_crashes_file
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max(limit * 2, 200):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json.loads(line))
        except (ValueError, TypeError):
            continue
    return out[-limit:]


def _prune_serve_logs() -> int:
    if _runtime is None:
        return 0
    keep = max(0, int(getattr(_runtime.config, "serve_log_retention_count", 50)))
    log_dir = _runtime.config.logs_dir
    if not log_dir.exists():
        return 0
    try:
        files = sorted(
            log_dir.glob("opencode-serve.*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        logger.info("pruned %d old serve logs (kept newest %d)", removed, keep)
    return removed


def _format_exit_for_notify(info: dict | None) -> str:
    if not info:
        return "(no exit info captured)"
    parts: list[str] = []
    kind = info.get("exit_kind")
    rc = info.get("exit_code")
    sig = info.get("signal_name")
    pid = info.get("pid")
    up = info.get("uptime_sec")
    if pid is not None:
        parts.append(f"pid={pid}")
    if kind:
        parts.append(f"exit_kind={kind}")
    if rc is not None:
        parts.append(f"rc={rc}")
    if sig:
        parts.append(f"signal={sig}")
    if isinstance(up, (int, float)):
        parts.append(f"uptime={_humanize_seconds(float(up))}")
    return " ".join(parts) if parts else "(no exit info captured)"


def _build_serve_down_notification() -> tuple[str, str, dict]:
    assert _runtime is not None
    title = "✗ opencode serve unreachable"
    body_lines = [
        f"`opencode serve` at {_runtime.config.endpoint} is down and "
        f"{SERVE_RESTART_MAX_ATTEMPTS} exponential restart attempts failed. "
        f"Agents will stall until the server is restored.",
    ]
    if _last_serve_crash_info:
        body_lines.append("")
        body_lines.append(
            f"Last detected exit: {_format_exit_for_notify(_last_serve_crash_info)}"
        )
        tail = (_last_serve_crash_info.get("log_tail") or "").strip()
        if tail:
            tail_short = "\n".join(tail.splitlines()[-5:])
            body_lines.append(f"Last log lines:\n{tail_short}")
    body_lines.append("")
    body_lines.append(
        f"Inspect history: `hermes oco serve-crashes` or "
        f"`{_runtime.config.serve_crashes_file}`. "
        f"Run `hermes oco doctor` for full diagnostics."
    )
    body = "\n".join(body_lines)
    meta = {
        "kind": "serve_down",
        "endpoint": _runtime.config.endpoint,
        "attempts": SERVE_RESTART_MAX_ATTEMPTS,
        "auto_spawn_server": _runtime.config.auto_spawn_server,
        "last_crash": _last_serve_crash_info,
    }
    return title, body, meta


def _notify_serve_down() -> None:
    _fanout_serve_event("serve_down", *_build_serve_down_notification())


def _build_serve_recovered_notification() -> tuple[str, str, dict]:
    assert _runtime is not None
    title = "✓ opencode serve recovered"
    body = (
        f"`opencode serve` at {_runtime.config.endpoint} is reachable again. "
        f"Agents will resume next tick."
    )
    meta = {
        "kind": "serve_recovered",
        "endpoint": _runtime.config.endpoint,
    }
    return title, body, meta


def _notify_serve_recovered() -> None:
    global _serve_recovered_at
    _serve_recovered_at = time.time()
    _fanout_serve_event("serve_recovered", *_build_serve_recovered_notification())


def _fanout_serve_event(kind: str, title: str, body: str, meta: dict) -> None:
    if _runtime is None:
        return
    from . import notify as notify_mod
    try:
        _runtime.config.events_log.parent.mkdir(parents=True, exist_ok=True)
        import json
        line = json.dumps({
            "ts": time.time(), "kind": kind, "agent_id": None,
            "title": title, "body": body, "meta": meta,
        }, default=str)
        with _runtime.config.events_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("events.log write failed during %s notify: %s", kind, e)
    try:
        results = notify_mod.fanout(
            sinks=list(SERVE_DOWN_NOTIFY_SINKS),
            title=title,
            body=body,
            meta=meta,
            dashboard_path=_runtime.config.notifications_file,
            gateway_platform=_runtime.config.notify_gateway_platform,
            gateway_chat_id=_runtime.config.notify_gateway_chat_id,
        )
        logger.info(
            "%s notified on sinks=%s results=%s",
            kind, list(SERVE_DOWN_NOTIFY_SINKS),
            [(r.sink, r.ok, r.detail) for r in results],
        )
    except Exception as e:
        logger.exception("%s fanout failed: %s", kind, e)


async def _pruner_loop() -> None:
    while not _stop_flag.is_set():
        if _runtime is None:
            await asyncio.sleep(5.0)
            continue
        try:
            now = time.time()
            for agent in list(_runtime.agents.list()):
                if agent.archived:
                    continue
                done_ts = agent.done_at if agent.phase == "DONE" else (agent.cancelled_at if agent.phase == "CANCELLED" else None)
                if done_ts and (now - done_ts) > ARCHIVE_AFTER_SEC:
                    _archive_done(agent)
                    _runtime.agents.update(agent.agent_id, archived=True, archived_at=now)
                    logger.info("archived %s agent %s after %.0fs", agent.phase, agent.agent_id, now - done_ts)
        except Exception as e:
            logger.warning("pruner tick failed: %s", e)
        await asyncio.sleep(PRUNE_INTERVAL_SEC)


async def _heartbeat_loop() -> None:
    while not _stop_flag.is_set():
        if _runtime is None or not _runtime.config.heartbeat_enabled:
            await asyncio.sleep(10.0)
            continue
        try:
            tz = heartbeat_mod._resolve_tz(_runtime.config.heartbeat_timezone)
            now_local = datetime.now(tz) if tz else datetime.now()
            wait_sec = heartbeat_mod.next_top_of_hour(now_local)
        except Exception as e:
            logger.warning("heartbeat scheduler failed to compute wait: %s", e)
            wait_sec = 600.0
        logger.info("heartbeat: next fire in %.0fs (top of hour)", wait_sec)
        await asyncio.sleep(max(wait_sec, 5.0))
        if _stop_flag.is_set():
            return
        try:
            result = heartbeat_mod.send_heartbeat(_runtime)
            logger.info("heartbeat fired: sent=%s sinks=%s", result.get("sent"), [s.get("sink") for s in result.get("sinks") or []])
        except Exception as e:
            logger.warning("heartbeat send failed: %s", e)


def _archive_done(agent: Agent) -> None:
    if _runtime is None:
        return
    history = _runtime.config.logs_dir.parent / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    import json
    from dataclasses import asdict
    line = json.dumps({**asdict(agent), "archived_at": time.time()}, default=str)
    with history.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def _agent_loop(agent_id: str) -> None:
    if _runtime is None:
        return
    backoff = 1.0
    while not _stop_flag.is_set():
        agent = _runtime.agents.get(agent_id)
        if agent is None or agent.phase in TERMINAL_PHASES:
            _cancel_agent_tasks(agent_id)
            return
        try:
            await _tick(agent)
            backoff = 1.0
            _clear_tick_failure(agent)
        except Exception as e:
            logger.warning(
                "agent %s tick failed: %s: %s",
                agent_id, type(e).__name__, e,
            )
            _record_tick_failure(agent, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        else:
            await asyncio.sleep(0.5)


def _record_tick_failure(agent: Agent, exc: BaseException) -> None:
    if _runtime is None:
        return
    summary = f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        current = _runtime.agents.get(agent.agent_id)
        previous = current.consecutive_tick_failures if current else 0
        serve_unhealthy = _serve_is_unhealthy_or_healing()
        if serve_unhealthy:
            updated = _runtime.agents.update(
                agent.agent_id,
                last_tick_error=summary,
                last_tick_error_at=time.time(),
            )
            if agent.agent_id not in _unhealthy_tick_notified_agents:
                _unhealthy_tick_notified_agents.add(agent.agent_id)
                _notify_event(
                    updated, "tick_error",
                    f"Tick failed (opencode serve unhealthy, not counted toward "
                    f"escalation): {summary}",
                )
            return
        consecutive = previous + 1
        updated = _runtime.agents.update(
            agent.agent_id,
            last_tick_error=summary,
            last_tick_error_at=time.time(),
            consecutive_tick_failures=consecutive,
        )
        if previous == 0:
            _notify_event(updated, "tick_error", f"Tick failed: {summary}")
        if consecutive >= TICK_FAILURE_ESCALATION_THRESHOLD and updated.phase not in TERMINAL_PHASES:
            escalated = _runtime.agents.update(
                agent.agent_id,
                phase="FAILED",
                phase_before_failed=updated.phase,
                last_error=f"stalled after {consecutive} consecutive tick failures: {summary}",
            )
            _maybe_notify_phase(escalated, "failed")
            _cancel_agent_tasks(agent.agent_id)
    except Exception as e:
        logger.debug("could not record tick failure for %s: %s", agent.agent_id, e)


def _clear_tick_failure(agent: Agent) -> None:
    if _runtime is None:
        return
    _unhealthy_tick_notified_agents.discard(agent.agent_id)
    try:
        current = _runtime.agents.get(agent.agent_id)
        if current and current.consecutive_tick_failures > 0:
            _runtime.agents.update(
                agent.agent_id,
                last_tick_error=None,
                last_tick_error_at=None,
                consecutive_tick_failures=0,
            )
    except Exception as e:
        logger.debug("could not clear tick failure for %s: %s", agent.agent_id, e)


def _phase_retry_ceiling(phase: str) -> int:
    return PHASE_RETRY_CEILING.get(phase, PHASE_RETRY_CEILING_DEFAULT)


def _handle_phase_failure(
    agent: Agent, phase: str, summary: str,
    *, on_exhausted_intervene: bool = False,
) -> bool:
    if _runtime is None:
        return True
    ceiling = _phase_retry_ceiling(phase)
    current = _runtime.agents.get(agent.agent_id)
    if current is None:
        return True
    attempts = (current.phase_retry_count or 0) + 1
    logger.warning(
        "agent %s phase=%s attempt %d/%d failed: %s",
        agent.agent_id, phase, attempts, ceiling, summary,
    )
    if attempts < ceiling:
        _runtime.agents.update(
            agent.agent_id,
            phase_retry_count=attempts,
            last_error=f"{phase} attempt {attempts}/{ceiling}: {summary}",
        )
        return False
    if on_exhausted_intervene:
        _enter_needs_intervention(
            current,
            reason=f"{phase}_exhausted",
            body=(
                f"Phase {phase} exhausted retry budget ({ceiling}). "
                f"Last error: {summary}\n\n"
                f"Resolve the underlying issue and run `oc_retry {agent.agent_id}`."
            ),
        )
        return True
    escalated = _runtime.agents.update(
        agent.agent_id,
        phase="FAILED",
        phase_before_failed=phase,
        last_error=f"{phase} exhausted retry budget ({ceiling}): {summary}",
    )
    _maybe_notify_phase(escalated, "failed")
    _cancel_agent_tasks(agent.agent_id)
    return True


def _enter_needs_intervention(agent: Agent, reason: str, body: str) -> None:
    if _runtime is None:
        return
    current = _runtime.agents.get(agent.agent_id)
    if current is None or current.phase in TERMINAL_PHASES:
        return
    if current.phase == "NEEDS_INTERVENTION":
        return
    updated = _runtime.agents.update(
        agent.agent_id,
        phase="NEEDS_INTERVENTION",
        phase_before_intervention=current.phase,
        intervention_reason=reason,
        intervention_since=time.time(),
        last_error=body[:500],
    )
    _notify_event(updated, "needs_intervention", body)


async def _phase_needs_intervention(agent: Agent) -> None:
    await asyncio.sleep(30.0)


def tail_recent_events(since_ts: float, limit: int = 50) -> list[dict]:
    import json as _json
    if _runtime is None:
        return []
    path = _runtime.config.events_log
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max(limit * 4, 200):]:
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except (ValueError, TypeError):
            continue
        try:
            ts = float(row.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts <= since_ts:
            continue
        out.append(row)
    return out[-limit:]


def _build_narration_snippet(agent_id: str, max_chars: int) -> str:
    buffer = get_text_buffer(agent_id)
    if not buffer:
        return ""
    text = "\n".join(buffer[k] for k in sorted(buffer.keys()) if isinstance(buffer[k], str))
    text = text.strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:].lstrip()


async def _progress_narration_loop() -> None:
    while not _stop_flag.is_set():
        try:
            if _runtime is None or not _runtime.config.progress_narration_enabled:
                await asyncio.sleep(_runtime.config.progress_narration_interval_sec if _runtime else 60.0)
                continue
            cfg = _runtime.config
            skip = TERMINAL_PHASES | {
                "AWAITING_HUMAN", "NEEDS_INTERVENTION", "RATE_LIMITED",
                "QUEUED", "PR_OPEN", "CREATED",
            }
            for a in _runtime.agents.list():
                if a.phase in skip:
                    continue
                snippet = _build_narration_snippet(a.agent_id, cfg.progress_narration_snippet_chars)
                if not snippet:
                    continue
                prev = _last_narrated_snippets.get(a.agent_id, "")
                if snippet == prev:
                    continue
                _last_narrated_snippets[a.agent_id] = snippet
                body = (
                    f"phase={a.phase}. Recent text:\n"
                    f"---\n{snippet}\n---\n"
                    f"Use `oc_output {a.agent_id}` for the full message."
                )
                _notify_event(a, "progress_narration", body)
        except Exception as e:
            logger.debug("progress-narration loop iteration failed: %s", e)
        interval = _runtime.config.progress_narration_interval_sec if _runtime else 300.0
        await asyncio.sleep(max(30.0, interval))


async def _phase_stuck_loop() -> None:
    while not _stop_flag.is_set():
        try:
            if _runtime is not None:
                now = time.time()
                for a in _runtime.agents.list():
                    if a.phase not in STUCK_WATCHED_PHASES:
                        continue
                    entered = a.phase_entered_at or 0.0
                    age = now - entered
                    if age < STUCK_WARN_SEC:
                        continue
                    last = a.last_stuck_notify_at or 0.0
                    if last >= entered:
                        continue
                    refreshed = _runtime.agents.update(
                        a.agent_id, last_stuck_notify_at=now,
                    )
                    _notify_event(
                        refreshed, "phase_stuck",
                        f"Agent {a.agent_id} has been in phase={a.phase} for "
                        f"{_humanize_seconds(age)} with no transition. Last "
                        f"error: {a.last_error or '(none)'}. Inspect with "
                        f"`oc_get {a.agent_id}` or kick with "
                        f"`oc_retry {a.agent_id}`.",
                    )
        except Exception as e:
            logger.debug("phase-stuck loop iteration failed: %s", e)
        await asyncio.sleep(STUCK_CHECK_INTERVAL_SEC)


async def _sse_consumer_loop(agent_id: str, stop_event: asyncio.Event) -> None:
    if _runtime is None:
        return
    agent = _runtime.agents.get(agent_id)
    if agent is None:
        return
    worktree = Path(agent.worktree_path)
    session_id = agent.session_id
    try:
        async for event in _runtime.client.stream_events(worktree, stop_event):
            if stop_event.is_set():
                return
            etype = event.get("type")
            props = event.get("properties") or {}
            if etype == "session.status":
                if props.get("sessionID") != session_id:
                    continue
                status = props.get("status")
                if isinstance(status, dict) and isinstance(status.get("type"), str):
                    _session_status_cache.update(agent_id, status, source="sse")
            elif etype == "message.updated":
                info = props.get("info") or {}
                if info.get("sessionID") != session_id:
                    continue
                msg_id = info.get("id")
                role = info.get("role")
                if isinstance(msg_id, str) and isinstance(role, str):
                    with _sse_buffer_lock:
                        roles = _sse_message_roles.setdefault(agent_id, {})
                        roles[msg_id] = role
            elif etype == "message.part.delta":
                if props.get("sessionID") != session_id:
                    continue
                if props.get("field") != "text":
                    continue
                part_id = props.get("partID") or props.get("part_id") or props.get("id")
                message_id = props.get("messageID") or props.get("message_id")
                delta = props.get("delta")
                if not isinstance(part_id, str) or not isinstance(delta, str):
                    continue
                with _sse_buffer_lock:
                    part_type = (_sse_part_types.get(agent_id) or {}).get(part_id)
                    if part_type is not None and part_type != "text":
                        continue
                    role = None
                    if isinstance(message_id, str):
                        role = (_sse_message_roles.get(agent_id) or {}).get(message_id)
                    if role is not None and role != "assistant":
                        continue
                    buffers = _sse_text_buffers.setdefault(agent_id, {})
                    apply_delta(buffers, part_id, delta)
            elif etype == "message.part.updated":
                part = props.get("part") or {}
                if part.get("sessionID") != session_id:
                    continue
                part_id = part.get("id") or part.get("partID")
                part_type = part.get("type")
                message_id = part.get("messageID") or part.get("message_id")
                if not isinstance(part_id, str):
                    continue
                if isinstance(part_type, str):
                    with _sse_buffer_lock:
                        types = _sse_part_types.setdefault(agent_id, {})
                        types[part_id] = part_type
                if part_type != "text":
                    with _sse_buffer_lock:
                        _sse_text_buffers.get(agent_id, {}).pop(part_id, None)
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                with _sse_buffer_lock:
                    role = None
                    if isinstance(message_id, str):
                        role = (_sse_message_roles.get(agent_id) or {}).get(message_id)
                    if role is not None and role != "assistant":
                        continue
                    buffers = _sse_text_buffers.setdefault(agent_id, {})
                    apply_snapshot(buffers, part_id, text)
            elif etype == "question.asked" or etype == "permission.asked":
                if props.get("sessionID") != session_id:
                    continue
                await _sse_surface_pending(agent_id, session_id, worktree)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("sse consumer %s ended: %s", agent_id, e)


async def _sse_surface_pending(agent_id: str, session_id: str, worktree: Path) -> None:
    if _runtime is None:
        return
    agent_now = _runtime.agents.get(agent_id)
    if agent_now is None or agent_now.phase in TERMINAL_PHASES:
        return
    try:
        questions = await _runtime.client.list_questions(worktree)
        permissions = await _runtime.client.list_permissions(worktree)
    except OpencodeError:
        return
    pending_q = [q for q in questions if q.get("sessionID") == session_id]
    pending_p = [p for p in permissions if p.get("sessionID") == session_id]
    _update_snapshots(agent_id, pending_q, pending_p)
    if not pending_q and not pending_p:
        return
    last_text = await _fetch_last_assistant_text(agent_now)
    await _maybe_notify_new_pending(agent_now, pending_q, pending_p, context_text=last_text)


async def _tick(agent: Agent) -> None:
    if _runtime is None:
        return
    phase = agent.phase
    handler = _PHASE_HANDLERS.get(phase)
    if handler is None:
        await asyncio.sleep(5.0)
        return
    await handler(agent)


def _message_error(item: dict) -> tuple[str, str] | None:
    """Extract opencode's structured error from a messages-API item.

    Opencode marks aborted assistant turns by setting `message.error =
    { name, message }` (e.g. `MessageAbortedError`). The error lives on
    the message itself, NOT as a text part, so the existing text-part
    readers in this module miss it entirely. This helper bridges that gap
    so the orchestrator can surface aborts as events.
    """
    message = item.get("message") or {}
    err = message.get("error") or item.get("error")
    if not isinstance(err, dict):
        return None
    name = err.get("name") or ""
    if not name:
        return None
    return name, err.get("message") or ""


def _message_is_rate_limited(item: dict) -> float | None:
    """Return retry-after seconds when opencode's structured error on this
    message indicates a provider rate-limit (HTTP 429 or textual marker).
    Returns None when not rate-limited.

    Opencode wraps provider 429s as `message.error = { name: "APIError",
    statusCode: 429, isRetryable, responseHeaders, metadata, ... }` per
    opencode/packages/opencode/src/session/message-v2.ts. We also accept
    textual patterns as a defensive fallback when statusCode is absent.
    """
    message = item.get("message") or {}
    err = message.get("error") or item.get("error")
    if not isinstance(err, dict):
        return None
    if err.get("name") != "APIError":
        return None
    matched = err.get("statusCode") == 429
    if not matched:
        text = (err.get("message") or "").lower()
        for pat in (
            "rate limit", "rate_limit", "too many requests",
            "quota exceeded", "quota_exceeded",
            "insufficient quota", "insufficient_quota", "429",
        ):
            if pat in text:
                matched = True
                break
    if not matched:
        return None
    retry_after_sec = 0.0
    headers = err.get("responseHeaders") or {}
    if isinstance(headers, dict):
        for header_name in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
            raw = headers.get(header_name)
            if raw:
                try:
                    retry_after_sec = max(retry_after_sec, float(raw))
                except (TypeError, ValueError):
                    pass
    metadata = err.get("metadata") or {}
    if isinstance(metadata, dict):
        for k in ("retryAfterMs", "retry_after_ms"):
            raw = metadata.get(k)
            if raw:
                try:
                    retry_after_sec = max(retry_after_sec, float(raw) / 1000.0)
                except (TypeError, ValueError):
                    pass
    return retry_after_sec


async def _check_session_rate_limited(
    agent: Agent, session_id: str, worktree: Path, *, session_label: str = "executor",
) -> bool:
    """Generic rate-limit check against any opencode session belonging to
    this agent. Caller passes the session_id + worktree of the session to
    inspect (executor or reviewer). On hit: transitions the agent to
    `RATE_LIMITED`, saves `phase_before_rate_limit`, records the retry
    window, fires the `rate_limited` notification. Returns True when the
    caller MUST NOT continue this tick's normal flow.

    The wait-and-resume path is intentional per v0.15.0 design: review
    is NOT bypassed; the original task continues through its own flow
    once the limit clears. v0.15.1 extends this coverage to the reviewer
    session (previously only the executor was instrumented).
    """
    if _runtime is None or agent.phase == "RATE_LIMITED":
        return False
    try:
        body = await _runtime.client.get_messages(session_id, worktree)
    except OpencodeError:
        return False
    items = body.get("items") or []
    for item in reversed(items):
        message = item.get("message") or {}
        if message.get("role") != "assistant" and message.get("type") != "assistant":
            continue
        retry_after = _message_is_rate_limited(item)
        if retry_after is None:
            return False
        wait_for = max(retry_after, RATE_LIMIT_MIN_WAIT_SEC)
        retry_at = time.time() + wait_for
        updated = _runtime.agents.update(
            agent.agent_id,
            phase="RATE_LIMITED",
            phase_before_rate_limit=agent.phase,
            rate_limited_at=time.time(),
            rate_limit_retry_after_at=retry_at,
            last_error=f"rate-limited on {session_label} session; retry after ~{int(wait_for)}s",
        )
        body_text = (
            f"Agent rate-limited by provider on {session_label} session "
            f"(phase={agent.phase}). Will retry in ~{int(wait_for)}s. "
            f"New tasks queued until clear."
        )
        _notify_event(updated, "rate_limited", body_text)
        return True
    return False


async def _check_executor_rate_limited(agent: Agent) -> bool:
    """v0.15.0 entry point. Thin back-compat wrapper around
    `_check_session_rate_limited` for the executor session, retained so
    every executor-touching phase handler in this module can call the
    same one-arg helper without threading the session_id through.
    """
    return await _check_session_rate_limited(
        agent, agent.session_id, Path(agent.worktree_path),
        session_label="executor",
    )


async def _check_executor_abort(agent: Agent) -> bool:
    """Detect a structured error on the executor's latest assistant turn.

    Returns True when an abort was observed (caller should NOT transition
    the agent this tick). On a newly-observed abort: notifies the user via
    `"aborted"` event and queues a "continue" follow-up to the executor.
    Same-`message.id` aborts are idempotent: we record it once, surface it
    once, and let the executor's own response stream advance state. After
    `ABORT_ESCALATION_THRESHOLD` distinct aborts the agent is escalated to
    FAILED. When no abort is observed and the agent has a non-empty abort
    streak, the streak is cleared as forward progress.
    """
    if _runtime is None:
        return False
    worktree = Path(agent.worktree_path)
    try:
        body = await _runtime.client.get_messages(agent.session_id, worktree)
    except OpencodeError:
        return False
    items = body.get("items") or []
    latest_err: tuple[str, str] | None = None
    latest_id: str | None = None
    for item in reversed(items):
        message = item.get("message") or {}
        if message.get("role") != "assistant" and message.get("type") != "assistant":
            continue
        latest_err = _message_error(item)
        latest_id = message.get("id") or item.get("id")
        break
    if latest_err is None:
        if agent.consecutive_aborts > 0 or agent.last_abort_msg_id:
            _runtime.agents.update(
                agent.agent_id,
                consecutive_aborts=0,
                last_abort_msg_id=None,
            )
        return False
    if latest_id and agent.last_abort_msg_id == latest_id:
        return True
    name, err_msg = latest_err
    consecutive = (agent.consecutive_aborts or 0) + 1
    updated = _runtime.agents.update(
        agent.agent_id,
        last_abort_msg_id=latest_id,
        consecutive_aborts=consecutive,
        last_error=f"{name}: {err_msg}" if err_msg else name,
    )
    if consecutive >= ABORT_ESCALATION_THRESHOLD and updated.phase not in TERMINAL_PHASES:
        escalated = _runtime.agents.update(
            agent.agent_id,
            phase="FAILED",
            phase_before_failed=updated.phase,
            last_error=f"executor aborted {consecutive} consecutive times: {name}: {err_msg}",
        )
        _maybe_notify_phase(escalated, "failed")
        _cancel_agent_tasks(agent.agent_id)
        return True
    nudge_idx = min(consecutive - 1, len(ABORT_NUDGE_PROMPTS) - 1)
    nudge = ABORT_NUDGE_PROMPTS[nudge_idx]
    nudge_preview = nudge.splitlines()[0][:60]
    body_text = (
        f"Executor turn aborted (attempt {consecutive}/{ABORT_ESCALATION_THRESHOLD}): "
        f"{name}"
        + (f": {err_msg}" if err_msg else "")
        + f". Auto-sending nudge: {nudge_preview!r}"
    )
    _notify_event(updated, "aborted", body_text)
    try:
        await _runtime.client.send_message_async(
            agent.session_id, worktree, nudge,
        )
    except OpencodeError as e:
        logger.warning(
            "aborted: send nudge to %s failed: %s",
            agent.agent_id, e,
        )
    return True


async def _phase_rate_limited(agent: Agent) -> None:
    assert _runtime is not None
    now = time.time()
    if agent.rate_limit_retry_after_at and now < agent.rate_limit_retry_after_at:
        wait_remaining = agent.rate_limit_retry_after_at - now
        await asyncio.sleep(min(RATE_LIMIT_MAX_TICK_WAIT_SEC, max(1.0, wait_remaining)))
        return
    restored = agent.phase_before_rate_limit or "EXECUTING"
    duration = now - (agent.rate_limited_at or now)
    refreshed = _runtime.agents.update(
        agent.agent_id, phase=restored,
        rate_limited_at=None,
        rate_limit_retry_after_at=None,
        phase_before_rate_limit=None,
        last_error=None,
    )
    _notify_event(
        refreshed, "rate_limit_cleared",
        f"Rate limit cleared after {int(duration)}s; agent resumed at phase={restored}.",
    )


async def _phase_queued(agent: Agent) -> None:
    assert _runtime is not None
    rate_limited = [
        a for a in _runtime.agents.list()
        if a.phase == "RATE_LIMITED" and a.agent_id != agent.agent_id
    ]
    if rate_limited:
        new_blocked = [a.agent_id for a in rate_limited]
        if new_blocked != (agent.queued_blocked_by or []):
            _runtime.agents.update(agent.agent_id, queued_blocked_by=new_blocked)
        await asyncio.sleep(QUEUE_POLL_SEC)
        return
    worktree = Path(agent.worktree_path)
    from .tools import wrap_initial_prompt
    prompt = wrap_initial_prompt(agent.initial_prompt)
    try:
        await _runtime.client.send_message_async(agent.session_id, worktree, prompt)
    except OpencodeError as e:
        if not _handle_phase_failure(agent, "QUEUED", str(e)):
            return
        return
    refreshed = _runtime.agents.update(agent.agent_id, phase="EXECUTING", queued_blocked_by=[])
    _notify_event(
        refreshed, "queue_drained",
        f"Queue drained: {agent.agent_id} resumed.",
    )


async def _phase_awaiting_human(agent: Agent) -> None:
    """Tick handler for AWAITING_HUMAN agents.

    v0.16.2 fix: never exit AWAITING_HUMAN on classifier verdict alone.
    The awaiting-input classifier is an LLM heuristic and can flip its
    mind across ticks on borderline prose (e.g. a soft `let me confirm
    scope.` ending). Pre-v0.16.2 a flip on the first tick after a
    process restart would fire a misleading `Human reply received`
    notification even though no human input occurred.

    Exit gate is now an authoritative server-side signal of forward
    progress:

      1. Entry was triggered by a pending `/question` or `/permission`
         (`awaiting_entry_had_pending_qp=True`) and the pending set
         is now empty for this session. The opencode server is the
         source of truth on whether a question/permission has been
         resolved.

      2. Entry was triggered by the prose-question classifier
         (`awaiting_entry_had_pending_qp=False`) AND a NEW assistant
         message has arrived since entry (the latest assistant
         `message.id` differs from `awaiting_entry_message_id`). A
         new assistant turn proves the executor moved past the
         awaiting state. The only way the executor produces a new
         turn while paused is for a human to type a reply via the
         opencode CLI / web UI / `/question` answer.

    In case (2) the classifier is re-run on the NEW text; if it
    flags the new turn as also awaiting (executor asked again), the
    entry message-id is re-anchored to the new turn and we keep
    sleeping. This keeps the resume-only-once invariant while
    accommodating multi-turn questioning.

    Legacy agents (pre-v0.16.2) entered AWAITING_HUMAN without
    capturing `awaiting_entry_message_id`. On the first tick after
    upgrade those agents backfill the field from the current latest
    assistant message and sleep; subsequent ticks use the proper
    gate.
    """
    assert _runtime is not None
    worktree = Path(agent.worktree_path)
    try:
        questions = await _runtime.client.list_questions(worktree)
        permissions = await _runtime.client.list_permissions(worktree)
    except OpencodeError:
        await asyncio.sleep(5.0)
        return
    pending_q = [q for q in questions if q.get("sessionID") == agent.session_id]
    pending_p = [p for p in permissions if p.get("sessionID") == agent.session_id]
    _update_snapshots(agent.agent_id, pending_q, pending_p)
    if pending_q or pending_p:
        last_text = await _fetch_last_assistant_text(agent)
        await _maybe_notify_new_pending(agent, pending_q, pending_p, context_text=last_text)
        await asyncio.sleep(5.0)
        return

    if agent.awaiting_entry_message_id is None and not agent.awaiting_entry_had_pending_qp:
        backfilled_mid = await _fetch_last_assistant_message_id(agent)
        _runtime.agents.update(
            agent.agent_id, awaiting_entry_message_id=backfilled_mid,
        )
        await asyncio.sleep(5.0)
        return

    current_mid = await _fetch_last_assistant_message_id(agent)
    new_turn_arrived = (
        current_mid is not None
        and current_mid != agent.awaiting_entry_message_id
    )

    if agent.awaiting_entry_had_pending_qp:
        exit_reason = "Pending question/permission resolved"
    elif new_turn_arrived:
        last_text = await _fetch_last_assistant_text(agent)
        if last_text:
            incomplete = await _fetch_incomplete_todos(agent)
            check = await awaiting_input_mod.check(
                _runtime, last_text, has_incomplete_todos=incomplete,
            )
            _runtime.agents.update(
                agent.agent_id,
                last_classifier_verdict=awaiting_input_mod.to_dict(check),
            )
            if check.awaiting:
                _runtime.agents.update(
                    agent.agent_id,
                    awaiting_entry_message_id=current_mid,
                    awaiting_entry_had_pending_qp=False,
                )
                await asyncio.sleep(5.0)
                return
        exit_reason = "Executor produced new assistant turn"
    else:
        await asyncio.sleep(5.0)
        return

    restored = agent.phase_before_awaiting or "EXECUTING"
    duration = time.time() - (agent.awaiting_human_since or time.time())
    refreshed = _runtime.agents.update(
        agent.agent_id,
        phase=restored,
        phase_before_awaiting=None,
        awaiting_human_since=None,
        awaiting_entry_message_id=None,
        awaiting_entry_had_pending_qp=False,
    )
    _notify_event(
        refreshed, "awaiting_human_resumed",
        f"{exit_reason} after {_humanize_seconds(duration)}; "
        f"agent resumed at phase={restored}.",
    )


async def _phase_executing(agent: Agent) -> None:
    assert _runtime is not None
    worktree = Path(agent.worktree_path)
    became_idle = False
    try:
        became_idle = await _wait_idle_through_cache(
            agent.agent_id, agent.session_id, worktree, timeout=60.0,
        )
    except OpencodeError:
        await asyncio.sleep(5.0)
        return
    if not became_idle:
        _reset_idle_since(agent)
        return
    if await _check_executor_rate_limited(agent):
        _reset_idle_since(agent)
        return
    if await _check_executor_abort(agent):
        _reset_idle_since(agent)
        return
    questions = await _runtime.client.list_questions(worktree)
    permissions = await _runtime.client.list_permissions(worktree)
    pending_q = [q for q in questions if q.get("sessionID") == agent.session_id]
    pending_p = [p for p in permissions if p.get("sessionID") == agent.session_id]
    _update_snapshots(agent.agent_id, pending_q, pending_p)
    if pending_q or pending_p:
        last_text = await _fetch_last_assistant_text(agent)
        if await _maybe_notify_new_pending(agent, pending_q, pending_p, context_text=last_text):
            _runtime.agents.update(agent.agent_id, last_awaiting_notify_at=time.time())
        _reset_idle_since(agent)
        return
    if not await _session_status_is_idle(agent):
        _reset_idle_since(agent)
        return
    last_text = await _fetch_last_assistant_text(agent)
    if last_text and reviewer_mod.parse_ready_for_review(last_text):
        if not _has_diff(worktree):
            logger.warning(
                "agent %s: READY_FOR_REVIEW emitted but worktree has no diff; ignoring",
                agent.agent_id,
            )
            return
        if await _awaiting_input_blocks_review(agent):
            return
        _runtime.agents.update(
            agent.agent_id,
            phase="IDLE_TASK_COMPLETE",
            idle_since=None,
            ready_for_review_at=time.time(),
        )
        return
    if not _idle_debounce_elapsed(agent):
        return
    confirm = await _wait_idle_through_cache(
        agent.agent_id, agent.session_id, worktree, timeout=2.0,
    )
    if not confirm:
        _reset_idle_since(agent)
        return
    if not _has_diff(worktree):
        return
    if await _awaiting_input_blocks_review(agent):
        return
    _runtime.agents.update(agent.agent_id, phase="IDLE_TASK_COMPLETE", idle_since=None)


async def _phase_idle_task_complete(agent: Agent) -> None:
    assert _runtime is not None
    _runtime.agents.update(agent.agent_id, phase="REVIEW_SPAWNING")


async def _phase_review_spawning(agent: Agent) -> None:
    assert _runtime is not None
    project = _runtime.projects.get(agent.project_label)
    if project is None:
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error="project gone"), "failed")
        return
    executor_worktree = Path(agent.worktree_path)
    try:
        sister = reviewer_mod.stage_reviewer_worktree(project, agent, executor_worktree)
    except wt_mod.GitError as e:
        if not _handle_phase_failure(agent, "REVIEW_SPAWNING", f"staging: {e}"):
            return
        return
    try:
        session_id, reviewer_text = await reviewer_mod.spawn_reviewer_session(
            _runtime.client, sister, agent, project.base_branch,
        )
    except OpencodeError as e:
        reviewer_mod.teardown_reviewer_worktree(project, executor_worktree)
        if not _handle_phase_failure(agent, "REVIEW_SPAWNING", f"session: {e}"):
            return
        return
    refreshed_for_event = _runtime.agents.update(
        agent.agent_id, phase="REVIEWING",
        reviewer_session_id=session_id,
        reviewer_worktree_path=str(sister),
    )
    _maybe_notify_phase(refreshed_for_event, "review_started")
    refreshed = _runtime.agents.get(agent.agent_id)
    if refreshed:
        await _handle_review_text(refreshed, reviewer_text)


async def _phase_reviewing(agent: Agent) -> None:
    assert _runtime is not None
    if not agent.reviewer_session_id or not agent.reviewer_worktree_path:
        _runtime.agents.update(
            agent.agent_id, phase="REVIEW_SPAWNING",
            reviewer_session_id=None, reviewer_worktree_path=None,
            last_error="reviewer state lost; re-staging",
        )
        return
    sister = Path(agent.reviewer_worktree_path)
    became_idle = await _runtime.client.wait_idle(agent.reviewer_session_id, sister, timeout=60.0)
    if not became_idle:
        return
    if await _check_session_rate_limited(
        agent, agent.reviewer_session_id, sister, session_label="reviewer",
    ):
        return
    try:
        body = await _runtime.client.get_messages(agent.reviewer_session_id, sister)
    except OpencodeError:
        return
    items = body.get("items") or []
    reviewer_text = _last_assistant_text(items)
    await _handle_review_text(agent, reviewer_text)


def decide_review_action(current_cycle: int, max_cycles: int) -> str:
    if current_cycle < max_cycles:
        return "address"
    return "exhausted"


async def _handle_review_text(agent: Agent, reviewer_text: str) -> None:
    assert _runtime is not None
    verdict = reviewer_mod.classify_review(reviewer_text)
    if verdict.kind == "lgtm":
        _runtime.agents.update(agent.agent_id, phase="COMMITTING")
        return
    if verdict.kind in ("requests_changes", "ambiguous"):
        action = decide_review_action(agent.review_cycle_count, _runtime.config.review_max_cycles)
        if action == "exhausted":
            logger.info(
                "agent %s: review cycles exhausted (count=%d, cap=%d, verdict=%s) - proceeding to COMMITTING",
                agent.agent_id, agent.review_cycle_count, _runtime.config.review_max_cycles, verdict.kind,
            )
            _runtime.agents.update(agent.agent_id, phase="COMMITTING", last_error=None)
            return
        try:
            await reviewer_mod.send_addressing_to_executor(_runtime.client, agent, verdict.body)
        except OpencodeError as e:
            if not _handle_phase_failure(agent, "REVIEW_DELIVERED", f"address dispatch: {e}"):
                return
            return
        _runtime.agents.update(
            agent.agent_id,
            phase="EXECUTOR_ADDRESSING",
            review_cycle_count=agent.review_cycle_count + 1,
        )
        return


async def _phase_executor_addressing(agent: Agent) -> None:
    assert _runtime is not None
    worktree = Path(agent.worktree_path)
    became_idle = await _wait_idle_through_cache(
        agent.agent_id, agent.session_id, worktree, timeout=60.0,
    )
    if not became_idle:
        _reset_idle_since(agent)
        return
    if await _check_executor_rate_limited(agent):
        _reset_idle_since(agent)
        return
    if await _check_executor_abort(agent):
        _reset_idle_since(agent)
        return
    questions = await _runtime.client.list_questions(worktree)
    permissions = await _runtime.client.list_permissions(worktree)
    pending_q = [q for q in questions if q.get("sessionID") == agent.session_id]
    pending_p = [p for p in permissions if p.get("sessionID") == agent.session_id]
    _update_snapshots(agent.agent_id, pending_q, pending_p)
    if pending_q or pending_p:
        last_text = await _fetch_last_assistant_text(agent)
        if await _maybe_notify_new_pending(agent, pending_q, pending_p, context_text=last_text):
            _runtime.agents.update(agent.agent_id, last_awaiting_notify_at=time.time())
        _reset_idle_since(agent)
        return
    if not await _session_status_is_idle(agent):
        _reset_idle_since(agent)
        return
    last_text = await _fetch_last_assistant_text(agent)
    if last_text and reviewer_mod.parse_ready_for_review(last_text):
        if await _awaiting_input_blocks_review(agent):
            return
        _runtime.agents.update(
            agent.agent_id,
            phase="COMMITTING",
            idle_since=None,
            ready_for_review_at=time.time(),
        )
        return
    if not _idle_debounce_elapsed(agent):
        return
    if await _awaiting_input_blocks_review(agent):
        return
    _runtime.agents.update(agent.agent_id, phase="COMMITTING", idle_since=None)


async def _phase_committing(agent: Agent) -> None:
    assert _runtime is not None
    project = _runtime.projects.get(agent.project_label)
    if project is None:
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error="project gone"), "failed")
        return
    if agent.reviewer_worktree_path:
        reviewer_mod.teardown_reviewer_worktree(project, Path(agent.worktree_path))

    if await _check_executor_rate_limited(agent):
        return

    info = await reviewer_mod.executor_open_pr(
        _runtime.client, agent, project.base_branch, timeout_sec=PR_OPEN_TIMEOUT_SEC,
    )
    if info is None:
        if await _check_executor_rate_limited(agent):
            return
        info, attempts = await reviewer_mod.oneshot_open_pr(
            _runtime.client, agent, project.base_branch,
            _runtime.config.pr_fallback_models,
        )
        if info is None:
            attempts_str = "; ".join(attempts) if attempts else "(no attempts)"
            _enter_needs_intervention(
                agent,
                reason="pr_fallback_exhausted",
                body=(
                    f"All PR-fallback models exhausted. Attempts: {attempts_str}\n\n"
                    f"Check `gh auth status` and network connectivity, then run "
                    f"`oc_retry {agent.agent_id}` to retry the COMMITTING phase. "
                    f"Use `oc_resume_pr {agent.agent_id}` to manually open the PR "
                    f"if you already pushed it."
                ),
            )
            return
    refreshed = _runtime.agents.update(
        agent.agent_id, phase="PR_OPEN",
        pr_url=info.url, pr_number=info.number,
    )
    _maybe_notify_phase(refreshed, "pr_opened")


async def _phase_pr_open(agent: Agent) -> None:
    assert _runtime is not None
    if not agent.pr_number:
        return
    worktree = Path(agent.worktree_path)
    try:
        info = pr_mod.pr_state(worktree, agent.pr_number)
    except pr_mod.PrError as e:
        logger.warning("pr_state %s: %s", agent.pr_number, e)
        await asyncio.sleep(PR_POLL_SEC)
        return
    if info.state == "MERGED" and info.merged_at:
        refreshed = _runtime.agents.update(
            agent.agent_id, phase="DONE",
            pr_merged_at=info.merged_at, done_at=time.time(),
        )
        _maybe_notify_phase(refreshed, "done")
        await _cleanup_worktrees(refreshed, worktree)
        return
    if info.state == "CLOSED":
        reason = f"PR #{agent.pr_number} closed without merge"
        refreshed = _runtime.agents.update(
            agent.agent_id, phase="CANCELLED",
            cancelled_at=time.time(), cancellation_reason=reason,
        )
        _maybe_notify_phase(refreshed, "cancelled")
        await _cleanup_worktrees(refreshed, worktree)
        return
    await asyncio.sleep(PR_POLL_SEC)


async def _cleanup_worktrees(agent: Agent, worktree: Path) -> None:
    assert _runtime is not None
    project = _runtime.projects.get(agent.project_label)
    if not project:
        return
    from . import bootstrap as bootstrap_mod
    if agent.reviewer_worktree_path:
        try:
            reviewer_mod.teardown_reviewer_worktree(project, worktree)
        except Exception as e:
            logger.warning("reviewer worktree teardown failed for %s: %s", agent.agent_id, e)
    try:
        cleanup_result = await bootstrap_mod.run_project_cleanup(_runtime.client, project, worktree)
        if not cleanup_result.ok:
            logger.warning("cleanup skill failed for %s: %s", agent.agent_id, cleanup_result.detail)
    except Exception as e:
        logger.warning("cleanup skill exception for %s: %s", agent.agent_id, e)
    try:
        wt_mod.remove_worktree(Path(project.repo_path), worktree, force=True)
    except Exception as e:
        logger.warning("remove_worktree failed for %s: %s", agent.agent_id, e)


_PHASE_HANDLERS = {
    "QUEUED": _phase_queued,
    "EXECUTING": _phase_executing,
    "AWAITING_HUMAN": _phase_awaiting_human,
    "NEEDS_INTERVENTION": _phase_needs_intervention,
    "IDLE_TASK_COMPLETE": _phase_idle_task_complete,
    "REVIEW_SPAWNING": _phase_review_spawning,
    "REVIEWING": _phase_reviewing,
    "EXECUTOR_ADDRESSING": _phase_executor_addressing,
    "IDLE_REVIEW_ADDRESSED": _phase_idle_task_complete,
    "COMMITTING": _phase_committing,
    "PR_OPEN": _phase_pr_open,
    "RATE_LIMITED": _phase_rate_limited,
}


def _reset_idle_since(agent: "Agent") -> None:
    if _runtime is None or agent.idle_since is None:
        return
    try:
        _runtime.agents.update(agent.agent_id, idle_since=None)
    except Exception as e:
        logger.debug("could not reset idle_since for %s: %s", agent.agent_id, e)


async def _wait_idle_through_cache(
    agent_id: str, session_id: str, worktree: Path, timeout: float = 60.0,
) -> bool:
    assert _runtime is not None
    became_idle = await _runtime.client.wait_idle(session_id, worktree, timeout=timeout)
    if became_idle:
        _session_status_cache.update(agent_id, {"type": "idle"}, source="poll")
    return became_idle


async def _session_status_is_idle(agent: "Agent") -> bool:
    status = get_session_status(agent.agent_id)
    if status is None:
        return True
    return status.get("type") == "idle"


def _idle_debounce_elapsed(agent: "Agent") -> bool:
    if _runtime is None:
        return False
    now = time.time()
    if agent.idle_since is None:
        try:
            _runtime.agents.update(agent.agent_id, idle_since=now)
        except Exception as e:
            logger.debug("could not set idle_since for %s: %s", agent.agent_id, e)
        return False
    return (now - agent.idle_since) >= IDLE_DEBOUNCE_SEC


def _has_diff(worktree: Path) -> bool:
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"], cwd=worktree, capture_output=True,
            text=True, timeout=10, check=False,
        )
        if res.stdout.strip():
            return True
        log = subprocess.run(
            ["git", "log", "--oneline", "-n", "1", "@{upstream}..HEAD"],
            cwd=worktree, capture_output=True, text=True, timeout=10, check=False,
        )
        return bool(log.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def _last_assistant_text(items: list[dict]) -> str:
    chunks: list[str] = []
    for item in reversed(items):
        message = item.get("message") or {}
        if message.get("role") != "assistant" and message.get("type") != "assistant":
            continue
        for p in item.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        if chunks:
            return "\n".join(reversed(chunks))
    return ""
