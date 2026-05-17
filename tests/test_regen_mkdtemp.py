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
        "_oco_test_pkg_mkdtemp", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = "_oco_test_pkg_mkdtemp"
    pkg.__path__ = [str(root)]
    sys.modules.setdefault("_oco_test_pkg_mkdtemp", pkg)
    spec.loader.exec_module(pkg)
    return pkg


_load_plugin()
config_mod = sys.modules["_oco_test_pkg_mkdtemp.config"]
projects_mod = sys.modules["_oco_test_pkg_mkdtemp.projects"]
state_mod = sys.modules["_oco_test_pkg_mkdtemp.state"]
tools_mod = sys.modules["_oco_test_pkg_mkdtemp.tools"]
bootstrap_mod = sys.modules["_oco_test_pkg_mkdtemp.bootstrap"]
worktree_mod = sys.modules["_oco_test_pkg_mkdtemp.worktree"]


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
    def ensure_server(self, deadline_sec: float = 15.0, log_dir=None) -> None:
        return None


def _runtime(tmp_path: Path, git_repo: Path):
    cfg = config_mod.Config(
        projects_file=tmp_path / "projects.json",
        agents_file=tmp_path / "agents.json",
        worktrees_root=tmp_path / "wt",
        logs_dir=tmp_path / "logs",
        notifications_file=tmp_path / "notifications.jsonl",
        auto_spawn_server=False,
        notify_sinks=["dashboard"],
    )
    cfg.ensure_dirs()
    projects = projects_mod.ProjectRegistry(cfg.projects_file)
    projects.add(label="my-app", repo_path=git_repo)
    agents = state_mod.AgentStore(cfg.agents_file)
    return tools_mod.Runtime(config=cfg, client=_StubClient(), projects=projects, agents=agents)


class TestRegenMkdtempCleanup:
    def test_regen_bootstrap_removes_mkdtemp_dir_before_create_worktree(
        self, tmp_path: Path, git_repo: Path, monkeypatch
    ):
        rt = _runtime(tmp_path, git_repo)
        observed = {"called": False, "existed": None, "target": None}

        def _fake_create_worktree(repo_path, target, branch, base="main"):
            observed["called"] = True
            observed["target"] = Path(target)
            observed["existed"] = Path(target).exists()
            assert not Path(target).exists(), (
                f"create_worktree saw existing dir at {target}; mkdtemp dir not cleaned up"
            )
            Path(target).mkdir(parents=True, exist_ok=True)

        def _fake_remove_worktree(repo_path, worktree_path, force=True):
            import shutil as _shutil
            _shutil.rmtree(worktree_path, ignore_errors=True)

        async def _fake_generate(client_, project, throwaway, registry, **kw):
            return bootstrap_mod.BootstrapResult(
                ok=True, method="stub", skill_updated=True, detail="ok"
            )

        monkeypatch.setattr(worktree_mod, "create_worktree", _fake_create_worktree)
        monkeypatch.setattr(worktree_mod, "remove_worktree", _fake_remove_worktree)
        monkeypatch.setattr(bootstrap_mod, "generate_bootstrap_skill", _fake_generate)

        handler = tools_mod.make_regen_bootstrap(rt)
        loop = asyncio.new_event_loop()
        try:
            res = json.loads(loop.run_until_complete(handler({"label": "my-app"})))
        finally:
            loop.close()

        assert observed["called"] is True
        assert observed["existed"] is False
        assert res["ok"] is True, res

    def test_regen_cleanup_removes_mkdtemp_dir_before_create_worktree(
        self, tmp_path: Path, git_repo: Path, monkeypatch
    ):
        rt = _runtime(tmp_path, git_repo)
        observed = {"called": False, "existed": None, "target": None}

        def _fake_create_worktree(repo_path, target, branch, base="main"):
            observed["called"] = True
            observed["target"] = Path(target)
            observed["existed"] = Path(target).exists()
            assert not Path(target).exists(), (
                f"create_worktree saw existing dir at {target}; mkdtemp dir not cleaned up"
            )
            Path(target).mkdir(parents=True, exist_ok=True)

        def _fake_remove_worktree(repo_path, worktree_path, force=True):
            import shutil as _shutil
            _shutil.rmtree(worktree_path, ignore_errors=True)

        async def _fake_generate_cleanup(client_, project, throwaway, registry, **kw):
            return bootstrap_mod.BootstrapResult(
                ok=True, method="stub", skill_updated=True, detail="ok"
            )

        monkeypatch.setattr(worktree_mod, "create_worktree", _fake_create_worktree)
        monkeypatch.setattr(worktree_mod, "remove_worktree", _fake_remove_worktree)
        monkeypatch.setattr(bootstrap_mod, "generate_cleanup_skill", _fake_generate_cleanup)

        handler = tools_mod.make_regen_cleanup(rt)
        loop = asyncio.new_event_loop()
        try:
            res = json.loads(loop.run_until_complete(handler({"label": "my-app"})))
        finally:
            loop.close()

        assert observed["called"] is True
        assert observed["existed"] is False
        assert res["ok"] is True, res
