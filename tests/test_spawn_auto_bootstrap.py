from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_oco_test_pkg", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = "_oco_test_pkg"
    pkg.__path__ = [str(root)]
    sys.modules.setdefault("_oco_test_pkg", pkg)
    spec.loader.exec_module(pkg)
    return pkg


_load_plugin()
config_mod = sys.modules["_oco_test_pkg.config"]
projects_mod = sys.modules["_oco_test_pkg.projects"]
state_mod = sys.modules["_oco_test_pkg.state"]
tools_mod = sys.modules["_oco_test_pkg.tools"]
bootstrap_mod = sys.modules["_oco_test_pkg.bootstrap"]
event_loop_mod = sys.modules["_oco_test_pkg.event_loop"]


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@l", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


class _StubClient:
    def __init__(self) -> None:
        self.created_sessions: list[Path] = []
        self.sent_messages: list[tuple[str, str]] = []

    def ensure_server(self) -> None:
        return None

    async def create_session(self, directory: Path, agent: str = "build") -> dict:
        self.created_sessions.append(Path(directory))
        return {"id": f"sess_{len(self.created_sessions)}"}

    async def send_message(self, session_id: str, directory: Path, text: str, timeout: float = 600.0) -> dict:
        self.sent_messages.append((session_id, text))
        return {"info": {"agent": "build", "finish": "stop"}, "parts": []}

    async def send_message_async(self, session_id: str, directory: Path, text: str, timeout: float = 30.0) -> dict:
        self.sent_messages.append((session_id, text))
        return {"queued": True}


class TestAutoBootstrapOnFirstSpawn:
    def _runtime(self, tmp_path: Path, git_repo: Path) -> tuple[object, _StubClient]:
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
            auto_spawn_server=False,
            notify_sinks=["dashboard"],
            auto_bootstrap_on_first_spawn=True,
        )
        cfg.ensure_dirs()
        projects = projects_mod.ProjectRegistry(cfg.projects_file)
        projects.add(label="my-app", repo_path=git_repo)
        agents = state_mod.AgentStore(cfg.agents_file)
        client = _StubClient()
        rt = tools_mod.Runtime(config=cfg, client=client, projects=projects, agents=agents)
        return rt, client

    def test_auto_bootstrap_fires_only_on_first_spawn(self, tmp_path: Path, git_repo: Path, monkeypatch):
        rt, client = self._runtime(tmp_path, git_repo)
        call_count = {"n": 0}

        async def _stub_generate(client_, project, throwaway, registry, *, agent="build", timeout_sec=900.0):
            call_count["n"] += 1
            registry.update(project.label, bootstrap_skill=f"opencode-orchestrator:{project.abbrev}-bootstrap")
            return bootstrap_mod.BootstrapResult(ok=True, method="opencode", skill_updated=True, detail="stub")

        async def _stub_run_project_bootstrap(client_, project, worktree, **kwargs):
            return bootstrap_mod.BootstrapResult(ok=True, method="skip", detail="stub")

        monkeypatch.setattr(bootstrap_mod, "generate_bootstrap_skill", _stub_generate)
        monkeypatch.setattr(bootstrap_mod, "run_project_bootstrap", _stub_run_project_bootstrap)
        monkeypatch.setattr(event_loop_mod, "start", lambda runtime: None)
        monkeypatch.setattr(event_loop_mod, "ensure_agent_task", lambda agent_id: None)

        handler = tools_mod.make_spawn(rt)

        loop = asyncio.new_event_loop()
        try:
            res1 = json.loads(loop.run_until_complete(handler({"project": "my-app", "task": "first", "prompt": "do thing"})))
        finally:
            loop.close()
        assert res1["ok"] is True, res1
        assert call_count["n"] == 1

        loop = asyncio.new_event_loop()
        try:
            res2 = json.loads(loop.run_until_complete(handler({"project": "my-app", "task": "second", "prompt": "do other"})))
        finally:
            loop.close()
        assert res2["ok"] is True, res2
        assert call_count["n"] == 1

    def test_disabled_flag_skips_auto_bootstrap(self, tmp_path: Path, git_repo: Path, monkeypatch):
        rt, _client = self._runtime(tmp_path, git_repo)
        rt.config.auto_bootstrap_on_first_spawn = False
        call_count = {"n": 0}

        async def _stub_generate(*args, **kwargs):
            call_count["n"] += 1
            return bootstrap_mod.BootstrapResult(ok=True, method="opencode")

        async def _stub_run_project_bootstrap(client_, project, worktree, **kwargs):
            return bootstrap_mod.BootstrapResult(ok=True, method="skip")

        monkeypatch.setattr(bootstrap_mod, "generate_bootstrap_skill", _stub_generate)
        monkeypatch.setattr(bootstrap_mod, "run_project_bootstrap", _stub_run_project_bootstrap)
        monkeypatch.setattr(event_loop_mod, "start", lambda runtime: None)
        monkeypatch.setattr(event_loop_mod, "ensure_agent_task", lambda agent_id: None)

        handler = tools_mod.make_spawn(rt)
        loop = asyncio.new_event_loop()
        try:
            res = json.loads(loop.run_until_complete(handler({"project": "my-app", "task": "no-boot", "prompt": "x"})))
        finally:
            loop.close()
        assert res["ok"] is True, res
        assert call_count["n"] == 0
