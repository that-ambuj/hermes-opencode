from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def _load_plugin_api(home: Path):
    root = Path(__file__).resolve().parent.parent
    api_path = root / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("_oco_dashboard_api", api_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_oco_dashboard_api"] = module
    spec.loader.exec_module(module)
    return module


class TestEventsWebsocket:
    def test_snapshot_and_agents_push(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_DASHBOARD_TOKEN", raising=False)
        module = _load_plugin_api(tmp_path)
        state_dir = tmp_path / "plugins" / "opencode-orchestrator"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "projects.json").write_text(json.dumps({}))
        agents_path = state_dir / "agents.json"
        agents_path.write_text(json.dumps({
            "dp/seed": {
                "agent_id": "dp/seed", "project_label": "dp", "worktree_path": "/t",
                "session_id": "s1", "branch": "dp/seed", "initial_prompt": "x",
                "phase": "EXECUTING", "last_activity_at": 1.0,
            }
        }))

        app = fastapi.FastAPI()
        app.include_router(module.router)
        client = TestClient(app)
        with client.websocket_connect("/events?token=anything") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert isinstance(snap["agents"], list)
            assert any(a["agent_id"] == "dp/seed" for a in snap["agents"])

            time.sleep(1.05)
            new_state = {
                "dp/seed": {
                    "agent_id": "dp/seed", "project_label": "dp", "worktree_path": "/t",
                    "session_id": "s1", "branch": "dp/seed", "initial_prompt": "x",
                    "phase": "REVIEWING", "last_activity_at": 2.0,
                }
            }
            agents_path.write_text(json.dumps(new_state))
            try:
                msg = ws.receive_json(timeout=5)
            except TypeError:
                msg = ws.receive_json()
            assert msg["type"] == "agents"
            assert msg["agents"][0]["phase"] == "REVIEWING"

    def test_token_rejected_when_env_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_DASHBOARD_TOKEN", "expected")
        module = _load_plugin_api(tmp_path)
        state_dir = tmp_path / "plugins" / "opencode-orchestrator"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agents.json").write_text(json.dumps({}))
        (state_dir / "projects.json").write_text(json.dumps({}))

        app = fastapi.FastAPI()
        app.include_router(module.router)
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect("/events?token=wrong"):
                pass

        with client.websocket_connect("/events?token=expected") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
