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
        state_dir = tmp_path / "plugins" / "hermes-opencode"
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
        state_dir = tmp_path / "plugins" / "hermes-opencode"
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


class TestDashboardServerUrlAndSessionUrls:
    """v0.14.5: dashboard surfaces (a) the configured opencode serve URL
    at the top of the page, and (b) per-agent session URLs constructed
    from server_url + session_id.
    """

    def test_make_session_url_basic(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        module = _load_plugin_api(tmp_path)
        assert module._make_session_url("http://127.0.0.1:4096", "ses_abc") == \
            "http://127.0.0.1:4096/session/ses_abc/message"

    def test_make_session_url_strips_trailing_slash(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        module = _load_plugin_api(tmp_path)
        assert module._make_session_url("http://h:9/", "s1") == "http://h:9/session/s1/message"

    def test_make_session_url_returns_none_when_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        module = _load_plugin_api(tmp_path)
        assert module._make_session_url("", "s1") is None
        assert module._make_session_url("http://h:9", None) is None
        assert module._make_session_url("http://h:9", "") is None

    def test_inject_session_urls_handles_reviewer(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        module = _load_plugin_api(tmp_path)
        rows = [
            {"agent_id": "a1", "session_id": "s1"},
            {"agent_id": "a2", "session_id": "s2", "reviewer_session_id": "rs2"},
            {"agent_id": "a3", "session_id": None},
        ]
        module._inject_session_urls(rows, "http://127.0.0.1:4096")
        assert rows[0]["session_url"] == "http://127.0.0.1:4096/session/s1/message"
        assert "reviewer_session_url" not in rows[0]
        assert rows[1]["session_url"] == "http://127.0.0.1:4096/session/s2/message"
        assert rows[1]["reviewer_session_url"] == "http://127.0.0.1:4096/session/rs2/message"
        assert rows[2]["session_url"] is None

    def test_agents_endpoint_returns_server_url_and_session_urls(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_DASHBOARD_TOKEN", raising=False)
        module = _load_plugin_api(tmp_path)
        monkeypatch.setattr(module, "_server_url_from_config", lambda: "http://hostA:9999")
        state_dir = tmp_path / "plugins" / "hermes-opencode"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agents.json").write_text(json.dumps({
            "bck/fix-discount": {
                "agent_id": "bck/fix-discount", "project_label": "bck",
                "worktree_path": "/t/wt", "session_id": "ses_42",
                "branch": "bck/fix-discount", "initial_prompt": "x",
                "phase": "EXECUTING", "last_activity_at": 1.0,
            }
        }))
        app = fastapi.FastAPI()
        app.include_router(module.router)
        client = TestClient(app)

        r = client.get("/agents")
        assert r.status_code == 200
        body = r.json()
        assert body["server_url"] == "http://hostA:9999"
        assert body["agents"][0]["session_url"] == "http://hostA:9999/session/ses_42/message"

    def test_get_agent_endpoint_returns_session_url(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_DASHBOARD_TOKEN", raising=False)
        module = _load_plugin_api(tmp_path)
        monkeypatch.setattr(module, "_server_url_from_config", lambda: "http://x:1")
        state_dir = tmp_path / "plugins" / "hermes-opencode"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agents.json").write_text(json.dumps({
            "a/b": {
                "agent_id": "a/b", "project_label": "a", "worktree_path": "/t",
                "session_id": "s", "branch": "a/b", "initial_prompt": "p",
                "phase": "EXECUTING", "last_activity_at": 1.0,
            }
        }))
        app = fastapi.FastAPI()
        app.include_router(module.router)
        r = TestClient(app).get("/agents/a/b")
        assert r.status_code == 200
        body = r.json()
        assert body["agent"]["session_url"] == "http://x:1/session/s/message"

    def test_config_endpoint(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_DASHBOARD_TOKEN", raising=False)
        module = _load_plugin_api(tmp_path)
        monkeypatch.setattr(module, "_server_url_from_config", lambda: "http://lhost:2222")
        app = fastapi.FastAPI()
        app.include_router(module.router)
        r = TestClient(app).get("/config")
        assert r.status_code == 200
        assert r.json() == {"plugin": "hermes-opencode", "server_url": "http://lhost:2222"}

    def test_websocket_snapshot_carries_server_url_and_session_urls(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_DASHBOARD_TOKEN", raising=False)
        module = _load_plugin_api(tmp_path)
        monkeypatch.setattr(module, "_server_url_from_config", lambda: "http://ws-host:7")
        state_dir = tmp_path / "plugins" / "hermes-opencode"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "projects.json").write_text(json.dumps({}))
        (state_dir / "agents.json").write_text(json.dumps({
            "a/b": {
                "agent_id": "a/b", "project_label": "a", "worktree_path": "/t",
                "session_id": "sess_ws", "branch": "a/b", "initial_prompt": "p",
                "phase": "EXECUTING", "last_activity_at": 1.0,
            }
        }))
        app = fastapi.FastAPI()
        app.include_router(module.router)
        client = TestClient(app)
        with client.websocket_connect("/events?token=anything") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert snap["server_url"] == "http://ws-host:7"
            assert snap["agents"][0]["session_url"] == "http://ws-host:7/session/sess_ws/message"
