from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from datetime import datetime
from . import heartbeat as heartbeat_mod
from . import pr as pr_mod
from . import reviewer as reviewer_mod
from . import worktree as wt_mod
from .state import Agent
from .transport import OpencodeError

if TYPE_CHECKING:
    from .tools import Runtime

logger = logging.getLogger("hermes_opencode.event_loop")

TERMINAL_PHASES = {"DONE", "KILLED", "FAILED"}
IDLE_DEBOUNCE_SEC = 30.0
PR_POLL_SEC = 300.0
PRUNE_INTERVAL_SEC = 60.0
DONE_RETENTION_SEC = 4 * 3600.0

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
_notified_phases: dict[str, set[str]] = {}
_notify_dedup_lock = threading.Lock()


_EVENT_GLYPH = {
    "pr_opened": "🔗",
    "done": "✓",
    "failed": "✗",
    "awaiting_human": "⏸",
    "review_started": "🔎",
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
    return f"{kind} for {agent.agent_id}"


def _maybe_notify_phase(agent: "Agent", kind: str, body: str = "") -> None:
    """Notify only once per (agent_id, kind) — prevents replay on tick re-entry."""
    with _notify_dedup_lock:
        seen = _notified_phases.setdefault(agent.agent_id, set())
        if kind in seen:
            return
        seen.add(kind)
    _notify_event(agent, kind, body)


def _maybe_notify_new_questions(agent: "Agent", pending_q: list[dict]) -> None:
    if not pending_q:
        return
    with _notify_dedup_lock:
        seen = _notified_questions.setdefault(agent.agent_id, set())
        new_ids = [q.get("id") for q in pending_q if q.get("id") and q.get("id") not in seen]
        for qid in new_ids:
            if qid:
                seen.add(qid)
    if not new_ids:
        return
    bodies: list[str] = []
    for q in pending_q:
        if q.get("id") not in new_ids:
            continue
        inner = (q.get("questions") or [{}])[0]
        text = (inner.get("question") or "").strip()
        opts = inner.get("options") or []
        bullet_lines = [f"  - {o.get('label')!r}: {o.get('description', '')}" for o in opts if isinstance(o, dict)]
        chunk = f"[{q.get('id')}] {text}"
        if bullet_lines:
            chunk += "\n" + "\n".join(bullet_lines)
        bodies.append(chunk)
    _notify_event(agent, "awaiting_human", "Pending questions:\n\n" + "\n\n".join(bodies))
_sse_text_buffers: dict[str, dict[str, str]] = {}
_sse_buffer_lock = threading.Lock()


def apply_delta(buffers: dict[str, str], part_id: str, delta: str) -> dict[str, str]:
    buffers[part_id] = buffers.get(part_id, "") + delta
    return buffers


def apply_snapshot(buffers: dict[str, str], part_id: str, text: str) -> dict[str, str]:
    buffers[part_id] = text
    return buffers


def get_text_buffer(agent_id: str) -> dict[str, str]:
    with _sse_buffer_lock:
        return dict(_sse_text_buffers.get(agent_id) or {})


def clear_text_buffer(agent_id: str) -> None:
    with _sse_buffer_lock:
        _sse_text_buffers.pop(agent_id, None)


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
    try:
        while not _stop_flag.is_set():
            if _runtime is not None:
                for agent in _runtime.agents.list():
                    if agent.phase not in TERMINAL_PHASES:
                        await _ensure_agent_task_async(agent.agent_id)
            await asyncio.sleep(2.0)
    finally:
        for t in (pruner, heartbeat):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


async def _pruner_loop() -> None:
    while not _stop_flag.is_set():
        if _runtime is None:
            await asyncio.sleep(5.0)
            continue
        try:
            now = time.time()
            for agent in list(_runtime.agents.list()):
                if agent.phase == "DONE" and agent.done_at and (now - agent.done_at) > DONE_RETENTION_SEC:
                    _archive_done(agent)
                    _runtime.agents.remove(agent.agent_id)
                    logger.info("pruned DONE agent %s after %.0fs", agent.agent_id, now - agent.done_at)
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
        except Exception as e:
            logger.exception("agent %s tick failed: %s", agent_id, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        else:
            await asyncio.sleep(0.5)


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
            if etype == "message.part.delta":
                if props.get("sessionID") != session_id:
                    continue
                if props.get("field") != "text":
                    continue
                part_id = props.get("partID") or props.get("part_id") or props.get("id")
                delta = props.get("delta")
                if not isinstance(part_id, str) or not isinstance(delta, str):
                    continue
                with _sse_buffer_lock:
                    buffers = _sse_text_buffers.setdefault(agent_id, {})
                    apply_delta(buffers, part_id, delta)
            elif etype == "message.part.updated":
                part = props.get("part") or {}
                if part.get("sessionID") != session_id:
                    continue
                if part.get("type") != "text":
                    continue
                part_id = part.get("id") or part.get("partID")
                text = part.get("text")
                if not isinstance(part_id, str) or not isinstance(text, str):
                    continue
                with _sse_buffer_lock:
                    buffers = _sse_text_buffers.setdefault(agent_id, {})
                    apply_snapshot(buffers, part_id, text)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("sse consumer %s ended: %s", agent_id, e)


async def _tick(agent: Agent) -> None:
    if _runtime is None:
        return
    phase = agent.phase
    handler = _PHASE_HANDLERS.get(phase)
    if handler is None:
        await asyncio.sleep(5.0)
        return
    await handler(agent)


async def _phase_executing(agent: Agent) -> None:
    assert _runtime is not None
    worktree = Path(agent.worktree_path)
    became_idle = False
    try:
        became_idle = await _runtime.client.wait_idle(agent.session_id, worktree, timeout=60.0)
    except OpencodeError:
        await asyncio.sleep(5.0)
        return
    if not became_idle:
        return
    questions = await _runtime.client.list_questions(worktree)
    permissions = await _runtime.client.list_permissions(worktree)
    pending_q = [q for q in questions if q.get("sessionID") == agent.session_id]
    pending_p = [p for p in permissions if p.get("sessionID") == agent.session_id]
    _update_snapshots(agent.agent_id, pending_q, pending_p)
    if pending_q or pending_p:
        _maybe_notify_new_questions(agent, pending_q)
        return
    await asyncio.sleep(IDLE_DEBOUNCE_SEC)
    confirm = await _runtime.client.wait_idle(agent.session_id, worktree, timeout=2.0)
    if not confirm:
        return
    if not _has_diff(worktree):
        return
    _runtime.agents.update(agent.agent_id, phase="IDLE_TASK_COMPLETE")


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
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"reviewer staging: {e}"), "failed")
        return
    try:
        session_id, reviewer_text = await reviewer_mod.spawn_reviewer_session(
            _runtime.client, sister, agent, project.base_branch,
        )
    except OpencodeError as e:
        reviewer_mod.teardown_reviewer_worktree(project, executor_worktree)
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"reviewer session: {e}"), "failed")
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
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error="reviewer state lost"), "failed")
        return
    sister = Path(agent.reviewer_worktree_path)
    became_idle = await _runtime.client.wait_idle(agent.reviewer_session_id, sister, timeout=60.0)
    if not became_idle:
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
    if verdict.kind == "requests_changes":
        action = decide_review_action(agent.review_cycle_count, _runtime.config.review_max_cycles)
        if action == "exhausted":
            logger.info(
                "agent %s: review cycles exhausted (count=%d, cap=%d) - proceeding to COMMITTING",
                agent.agent_id, agent.review_cycle_count, _runtime.config.review_max_cycles,
            )
            _runtime.agents.update(agent.agent_id, phase="COMMITTING", last_error=None)
            return
        try:
            await reviewer_mod.send_addressing_to_executor(_runtime.client, agent, verdict.body)
        except OpencodeError as e:
            _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"address dispatch: {e}"), "failed")
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
    became_idle = await _runtime.client.wait_idle(agent.session_id, worktree, timeout=60.0)
    if not became_idle:
        return
    questions = await _runtime.client.list_questions(worktree)
    permissions = await _runtime.client.list_permissions(worktree)
    pending_q = [q for q in questions if q.get("sessionID") == agent.session_id]
    pending_p = [p for p in permissions if p.get("sessionID") == agent.session_id]
    _update_snapshots(agent.agent_id, pending_q, pending_p)
    if pending_q or pending_p:
        return
    await asyncio.sleep(IDLE_DEBOUNCE_SEC)
    _runtime.agents.update(agent.agent_id, phase="COMMITTING")


async def _phase_committing(agent: Agent) -> None:
    assert _runtime is not None
    project = _runtime.projects.get(agent.project_label)
    if project is None:
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error="project gone"), "failed")
        return
    if agent.reviewer_worktree_path:
        reviewer_mod.teardown_reviewer_worktree(project, Path(agent.worktree_path))
    try:
        info = await reviewer_mod.finalize_and_open_pr(project, agent, project.base_branch)
    except pr_mod.PrError as e:
        _maybe_notify_phase(_runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"pr open: {e}"), "failed")
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
        project = _runtime.projects.get(agent.project_label)
        if project:
            wt_mod.remove_worktree(Path(project.repo_path), worktree, force=True)
        return
    await asyncio.sleep(PR_POLL_SEC)


_PHASE_HANDLERS = {
    "EXECUTING": _phase_executing,
    "IDLE_TASK_COMPLETE": _phase_idle_task_complete,
    "REVIEW_SPAWNING": _phase_review_spawning,
    "REVIEWING": _phase_reviewing,
    "EXECUTOR_ADDRESSING": _phase_executor_addressing,
    "IDLE_REVIEW_ADDRESSED": _phase_idle_task_complete,
    "COMMITTING": _phase_committing,
    "PR_OPEN": _phase_pr_open,
}


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
        for p in item.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        if chunks:
            return "\n".join(reversed(chunks))
    return ""
