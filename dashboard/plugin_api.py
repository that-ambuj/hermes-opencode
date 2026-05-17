"""opencode-orchestrator dashboard backend.

Mounted at /api/plugins/opencode-orchestrator/ by the hermes dashboard. Returns
read-only views over the plugin's on-disk state (projects.json, agents.json,
notifications.jsonl). Destructive actions stay in the main tool surface.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

try:
    from hermes_constants import get_hermes_home  # type: ignore
except ImportError:
    import os as _os

    def get_hermes_home() -> Path:
        val = (_os.environ.get("HERMES_HOME") or "").strip()
        return Path(val) if val else Path.home() / ".hermes"


try:
    from fastapi import APIRouter
except ImportError:
    class APIRouter:  # type: ignore[no-redef]
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn


PLUGIN_NAME = "opencode-orchestrator"


def _state_dir() -> Path:
    return get_hermes_home() / "plugins" / PLUGIN_NAME


def _read_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _tail_jsonl(path: Path, n: int = 50) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


router = APIRouter()


@router.get("/")
async def index() -> dict[str, Any]:
    return {
        "plugin": PLUGIN_NAME,
        "endpoints": [
            "GET  /agents",
            "GET  /agents/{agent_id}",
            "GET  /projects",
            "GET  /projects/{label}",
            "GET  /heartbeats",
            "GET  /history",
            "GET  /health",
        ],
        "state_dir": str(_state_dir()),
    }


@router.get("/health")
async def health() -> dict[str, Any]:
    sd = _state_dir()
    return {
        "ok": True,
        "now": time.time(),
        "state_dir_exists": sd.is_dir(),
        "projects_file_exists": (sd / "projects.json").exists(),
        "agents_file_exists": (sd / "agents.json").exists(),
    }


@router.get("/agents")
async def list_agents() -> dict[str, Any]:
    data = _read_json(_state_dir() / "agents.json")
    if not isinstance(data, dict):
        return {"agents": [], "count": 0}
    rows = list(data.values())
    rows.sort(key=lambda r: r.get("last_activity_at") or 0, reverse=True)
    return {"agents": rows, "count": len(rows)}


@router.get("/agents/{agent_id:path}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    data = _read_json(_state_dir() / "agents.json")
    if isinstance(data, dict) and agent_id in data:
        return {"agent": data[agent_id]}
    return {"agent": None, "error": "not_found"}


@router.get("/projects")
async def list_projects() -> dict[str, Any]:
    data = _read_json(_state_dir() / "projects.json")
    if not isinstance(data, dict):
        return {"projects": [], "count": 0}
    rows = list(data.values())
    for r in rows:
        repo_path = r.get("repo_path")
        if repo_path:
            r["repo_exists"] = Path(repo_path).is_dir()
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return {"projects": rows, "count": len(rows)}


@router.get("/projects/{label}")
async def get_project(label: str) -> dict[str, Any]:
    data = _read_json(_state_dir() / "projects.json")
    if isinstance(data, dict) and label in data:
        return {"project": data[label]}
    return {"project": None, "error": "not_found"}


@router.get("/heartbeats")
async def heartbeats(n: int = 20) -> dict[str, Any]:
    n = max(1, min(200, int(n)))
    records = _tail_jsonl(_state_dir() / "notifications.jsonl", n=n)
    records.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"items": records, "count": len(records)}


@router.get("/history")
async def history(n: int = 50) -> dict[str, Any]:
    n = max(1, min(500, int(n)))
    records = _tail_jsonl(_state_dir() / "history.jsonl", n=n)
    records.sort(key=lambda r: r.get("archived_at") or 0, reverse=True)
    return {"items": records, "count": len(records)}
