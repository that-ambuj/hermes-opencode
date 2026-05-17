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

logger = logging.getLogger("opencode_orchestrator.event_loop")

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
_agent_tasks: dict[str, asyncio.Task] = {}
_question_snapshot: dict[str, list[dict]] = {}
_permission_snapshot: dict[str, list[dict]] = {}
_snapshot_lock = threading.Lock()


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
    existing = _agent_tasks.get(agent_id)
    if existing and not existing.done():
        return
    task = asyncio.create_task(_agent_loop(agent_id))
    _agent_tasks[agent_id] = task


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
        if _runtime is not None:
            now = time.time()
            for agent in list(_runtime.agents.list()):
                if agent.phase == "DONE" and agent.done_at and (now - agent.done_at) > DONE_RETENTION_SEC:
                    _archive_done(agent)
                    _runtime.agents.remove(agent.agent_id)
                    logger.info("pruned DONE agent %s after %.0fs", agent.agent_id, now - agent.done_at)
        await asyncio.sleep(PRUNE_INTERVAL_SEC)


async def _heartbeat_loop() -> None:
    if _runtime is None or not _runtime.config.heartbeat_enabled:
        return
    while not _stop_flag.is_set():
        try:
            tz = heartbeat_mod._resolve_tz(_runtime.config.heartbeat_timezone)
            now_local = datetime.now(tz) if tz else datetime.now()
            wait_sec = heartbeat_mod.next_top_of_hour(now_local)
        except Exception as e:
            logger.warning("heartbeat scheduler failed to compute wait: %s", e)
            wait_sec = 600.0
        await asyncio.sleep(max(wait_sec, 5.0))
        if _stop_flag.is_set():
            return
        try:
            heartbeat_mod.send_heartbeat(_runtime)
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
            _agent_tasks.pop(agent_id, None)
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
        _runtime.agents.update(agent.agent_id, phase="FAILED", last_error="project gone")
        return
    executor_worktree = Path(agent.worktree_path)
    try:
        sister = reviewer_mod.stage_reviewer_worktree(project, agent, executor_worktree)
    except wt_mod.GitError as e:
        _runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"reviewer staging: {e}")
        return
    try:
        session_id, reviewer_text = await reviewer_mod.spawn_reviewer_session(
            _runtime.client, sister, agent, project.base_branch,
        )
    except OpencodeError as e:
        reviewer_mod.teardown_reviewer_worktree(project, executor_worktree)
        _runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"reviewer session: {e}")
        return
    _runtime.agents.update(
        agent.agent_id, phase="REVIEWING",
        reviewer_session_id=session_id,
        reviewer_worktree_path=str(sister),
    )
    refreshed = _runtime.agents.get(agent.agent_id)
    if refreshed:
        await _handle_review_text(refreshed, reviewer_text)


async def _phase_reviewing(agent: Agent) -> None:
    assert _runtime is not None
    if not agent.reviewer_session_id or not agent.reviewer_worktree_path:
        _runtime.agents.update(agent.agent_id, phase="FAILED", last_error="reviewer state lost")
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


async def _handle_review_text(agent: Agent, reviewer_text: str) -> None:
    assert _runtime is not None
    verdict = reviewer_mod.classify_review(reviewer_text)
    if verdict.kind == "lgtm":
        _runtime.agents.update(agent.agent_id, phase="COMMITTING")
        return
    if verdict.kind == "requests_changes":
        try:
            await reviewer_mod.send_addressing_to_executor(_runtime.client, agent, verdict.body)
        except OpencodeError as e:
            _runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"address dispatch: {e}")
            return
        _runtime.agents.update(agent.agent_id, phase="EXECUTOR_ADDRESSING")
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
        _runtime.agents.update(agent.agent_id, phase="FAILED", last_error="project gone")
        return
    if agent.reviewer_worktree_path:
        reviewer_mod.teardown_reviewer_worktree(project, Path(agent.worktree_path))
    try:
        info = await reviewer_mod.finalize_and_open_pr(project, agent, project.base_branch)
    except pr_mod.PrError as e:
        _runtime.agents.update(agent.agent_id, phase="FAILED", last_error=f"pr open: {e}")
        return
    _runtime.agents.update(
        agent.agent_id, phase="PR_OPEN",
        pr_url=info.url, pr_number=info.number,
    )


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
        _runtime.agents.update(
            agent.agent_id, phase="DONE",
            pr_merged_at=info.merged_at, done_at=time.time(),
        )
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
