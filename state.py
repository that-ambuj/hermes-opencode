from __future__ import annotations

import json
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PHASES = {
    "CREATED",
    "BOOTSTRAPPING",
    "EXECUTING",
    "IDLE_TASK_COMPLETE",
    "REVIEW_SPAWNING",
    "REVIEWING",
    "REVIEW_DELIVERED",
    "EXECUTOR_ADDRESSING",
    "IDLE_REVIEW_ADDRESSED",
    "COMMITTING",
    "PR_OPEN",
    "DONE",
    "FAILED",
    "KILLED",
    "CANCELLED",
}


@dataclass
class Agent:
    agent_id: str
    project_label: str
    worktree_path: str
    session_id: str
    branch: str
    initial_prompt: str
    phase: str = "EXECUTING"
    last_cursor: str | None = None
    reviewer_session_id: str | None = None
    reviewer_worktree_path: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pr_merged_at: float | None = None
    done_at: float | None = None
    last_error: str | None = None
    review_cycle_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    archived: bool = False
    archived_at: float | None = None
    cancelled_at: float | None = None
    cancellation_reason: str | None = None
    last_progress_at: float = field(default_factory=time.time)
    last_awaiting_notify_at: float | None = None
    last_classifier_verdict: dict | None = None
    last_tick_error: str | None = None
    last_tick_error_at: float | None = None
    consecutive_tick_failures: int = 0


class AgentExists(ValueError):
    pass


class AgentNotFound(KeyError):
    pass


class AgentStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, prefix=self._path.name + ".",
            suffix=".tmp", delete=False, encoding="utf-8",
        ) as f:
            json.dump(data, f, indent=2, sort_keys=True)
            tmp = Path(f.name)
        tmp.replace(self._path)

    def list(self) -> list[Agent]:
        with self._lock:
            return [Agent(**v) for v in self._read().values()]

    def get(self, agent_id: str) -> Agent | None:
        with self._lock:
            raw = self._read().get(agent_id)
            return Agent(**raw) if raw else None

    def ids(self) -> set[str]:
        with self._lock:
            return set(self._read().keys())

    def add(self, agent: Agent) -> None:
        with self._lock:
            d = self._read()
            if agent.agent_id in d:
                raise AgentExists(f"agent already exists: {agent.agent_id}")
            d[agent.agent_id] = asdict(agent)
            self._write(d)

    def remove(self, agent_id: str) -> Agent | None:
        with self._lock:
            d = self._read()
            raw = d.pop(agent_id, None)
            if raw is None:
                return None
            self._write(d)
            return Agent(**raw)

    def update(self, agent_id: str, **fields_to_set: object) -> Agent:
        with self._lock:
            d = self._read()
            if agent_id not in d:
                raise AgentNotFound(agent_id)
            phase = fields_to_set.get("phase")
            if isinstance(phase, str) and phase not in PHASES:
                raise ValueError(f"invalid phase: {phase}")
            for k, v in fields_to_set.items():
                d[agent_id][k] = v
            d[agent_id]["last_activity_at"] = time.time()
            self._write(d)
            return Agent(**d[agent_id])
