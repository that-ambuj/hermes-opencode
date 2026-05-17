"""hermes-opencode dashboard backend.

Mounted at /api/plugins/hermes-opencode/ by the hermes dashboard. Returns
read-only views over the plugin's on-disk state (projects.json, agents.json,
notifications.jsonl). Destructive actions stay in the main tool surface.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
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
    from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status as http_status
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

    class APIRouter:  # type: ignore[no-redef]
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn

        def websocket(self, *_a, **_k):
            return lambda fn: fn

    class WebSocket:  # type: ignore[no-redef]
        pass

    class WebSocketDisconnect(Exception):  # type: ignore[no-redef]
        pass

    class _StatusStub:
        WS_1008_POLICY_VIOLATION = 1008

    http_status = _StatusStub()  # type: ignore[assignment]


log = logging.getLogger("hermes_opencode.dashboard")


PLUGIN_NAME = "hermes-opencode"


def _state_dir() -> Path:
    return get_hermes_home() / "plugins" / PLUGIN_NAME


def _server_url_from_config() -> str:
    try:
        from .. import config as cfg_mod
        return cfg_mod.Config.from_plugin_entry(cfg_mod.load_entry_config()).connect_url
    except Exception:
        return ""


def _make_session_url(server_url: str, session_id: str | None) -> str | None:
    if not server_url or not session_id:
        return None
    return server_url.rstrip("/") + "/session/" + session_id + "/message"


def _inject_session_urls(rows: list[dict], server_url: str) -> None:
    for r in rows:
        r["session_url"] = _make_session_url(server_url, r.get("session_id"))
        if r.get("reviewer_session_id"):
            r["reviewer_session_url"] = _make_session_url(server_url, r.get("reviewer_session_id"))


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
            "GET  /config",
            "GET  /health",
        ],
        "state_dir": str(_state_dir()),
    }


@router.get("/config")
async def get_config() -> dict[str, Any]:
    return {
        "plugin": PLUGIN_NAME,
        "server_url": _server_url_from_config(),
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
async def list_agents(include_archived: int = 0) -> dict[str, Any]:
    data = _read_json(_state_dir() / "agents.json")
    if not isinstance(data, dict):
        return {"agents": [], "count": 0, "archived_hidden": 0, "server_url": ""}
    rows = list(data.values())
    hidden = 0
    if not include_archived:
        kept: list[dict] = []
        for r in rows:
            if r.get("archived"):
                hidden += 1
            else:
                kept.append(r)
        rows = kept
    rows.sort(key=lambda r: r.get("last_activity_at") or 0, reverse=True)
    server_url = _server_url_from_config()
    _inject_session_urls(rows, server_url)
    return {
        "agents": rows,
        "count": len(rows),
        "archived_hidden": hidden,
        "server_url": server_url,
    }


@router.get("/agents/{agent_id:path}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    data = _read_json(_state_dir() / "agents.json")
    if isinstance(data, dict) and agent_id in data:
        agent = dict(data[agent_id])
        _inject_session_urls([agent], _server_url_from_config())
        return {"agent": agent}
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


def _check_ws_token(provided: str | None) -> bool:
    expected = os.environ.get("HERMES_DASHBOARD_TOKEN")
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def _snapshot_payload(include_archived: bool = False) -> dict[str, Any]:
    sd = _state_dir()
    agents_data = _read_json(sd / "agents.json")
    projects_data = _read_json(sd / "projects.json")
    agents_rows = list(agents_data.values()) if isinstance(agents_data, dict) else []
    archived_hidden = 0
    if not include_archived:
        kept: list[dict] = []
        for r in agents_rows:
            if r.get("archived"):
                archived_hidden += 1
            else:
                kept.append(r)
        agents_rows = kept
    agents_rows.sort(key=lambda r: r.get("last_activity_at") or 0, reverse=True)
    projects_rows = list(projects_data.values()) if isinstance(projects_data, dict) else []
    for r in projects_rows:
        repo_path = r.get("repo_path")
        if repo_path:
            r["repo_exists"] = Path(repo_path).is_dir()
    projects_rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    server_url = _server_url_from_config()
    _inject_session_urls(agents_rows, server_url)
    return {
        "type": "snapshot",
        "agents": agents_rows,
        "projects": projects_rows,
        "archived_hidden": archived_hidden,
        "server_url": server_url,
    }


def _read_jsonl_since(path: Path, byte_offset: int) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    try:
        size = path.stat().st_size
        if size <= byte_offset:
            return [], size
        with path.open("rb") as f:
            f.seek(byte_offset)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        items: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items, size
    except OSError:
        return [], byte_offset


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@router.websocket("/events")
async def events_ws(websocket: "WebSocket") -> None:
    if not _FASTAPI_AVAILABLE:
        return
    token = websocket.query_params.get("token")
    if not _check_ws_token(token):
        await websocket.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    include_archived_q = websocket.query_params.get("include_archived")
    include_archived = bool(include_archived_q and include_archived_q not in ("0", "false", "False", ""))
    await websocket.accept()
    sd = _state_dir()
    agents_path = sd / "agents.json"
    notif_path = sd / "notifications.jsonl"
    try:
        await websocket.send_json(_snapshot_payload(include_archived))
    except Exception:
        return
    agents_mtime = _file_mtime(agents_path)
    notif_offset = notif_path.stat().st_size if notif_path.exists() else 0
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                return
            new_agents_mtime = _file_mtime(agents_path)
            if new_agents_mtime != agents_mtime:
                agents_mtime = new_agents_mtime
                payload = _snapshot_payload(include_archived)
                try:
                    await websocket.send_json({
                        "type": "agents",
                        "agents": payload["agents"],
                        "archived_hidden": payload.get("archived_hidden", 0),
                        "server_url": payload.get("server_url", ""),
                    })
                except Exception:
                    return
            items, new_offset = _read_jsonl_since(notif_path, notif_offset)
            if items:
                notif_offset = new_offset
                try:
                    await websocket.send_json({"type": "heartbeat", "items": items})
                except Exception:
                    return
            elif new_offset != notif_offset:
                notif_offset = new_offset
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.debug("events_ws error: %s", e)
        try:
            await websocket.close()
        except Exception:
            pass
