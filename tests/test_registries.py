from __future__ import annotations

import importlib.util
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
projects_mod = sys.modules["_oco_test_pkg.projects"]
state_mod = sys.modules["_oco_test_pkg.state"]


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


class TestProjectRegistry:
    def test_add_and_get_roundtrip(self, tmp_path: Path, git_repo: Path):
        reg = projects_mod.ProjectRegistry(tmp_path / "projects.json")
        project = reg.add(label="my-app", repo_path=git_repo)
        assert project.label == "my-app"
        assert project.abbrev == "ma"
        assert project.repo_path == str(git_repo)
        assert reg.get("my-app").label == "my-app"

    def test_add_rejects_duplicate_label(self, tmp_path: Path, git_repo: Path):
        reg = projects_mod.ProjectRegistry(tmp_path / "projects.json")
        reg.add(label="my-app", repo_path=git_repo)
        with pytest.raises(projects_mod.ProjectExists):
            reg.add(label="my-app", repo_path=git_repo)

    def test_add_rejects_abbrev_collision(self, tmp_path: Path, git_repo: Path):
        other_repo = tmp_path / "other"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other_repo, check=True)
        (other_repo / "f").write_text("y")
        subprocess.run(["git", "add", "."], cwd=other_repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@l", "-c", "user.name=t",
             "commit", "-q", "-m", "init"], cwd=other_repo, check=True,
        )
        reg = projects_mod.ProjectRegistry(tmp_path / "projects.json")
        reg.add(label="my-app", repo_path=git_repo)
        with pytest.raises(ValueError, match="abbrev"):
            reg.add(label="my-active", repo_path=other_repo)

    def test_add_rejects_non_git_repo(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        reg = projects_mod.ProjectRegistry(tmp_path / "projects.json")
        with pytest.raises(ValueError, match="not a git"):
            reg.add(label="x", repo_path=plain)

    def test_remove_then_get_returns_none(self, tmp_path: Path, git_repo: Path):
        reg = projects_mod.ProjectRegistry(tmp_path / "projects.json")
        reg.add(label="my-app", repo_path=git_repo)
        removed = reg.remove("my-app")
        assert removed is not None and removed.label == "my-app"
        assert reg.get("my-app") is None

    def test_update_partial_fields(self, tmp_path: Path, git_repo: Path):
        reg = projects_mod.ProjectRegistry(tmp_path / "projects.json")
        reg.add(label="my-app", repo_path=git_repo)
        updated = reg.update("my-app", base_branch="develop")
        assert updated.base_branch == "develop"
        assert reg.get("my-app").base_branch == "develop"

    def test_atomic_write_survives_reload(self, tmp_path: Path, git_repo: Path):
        path = tmp_path / "projects.json"
        reg = projects_mod.ProjectRegistry(path)
        reg.add(label="my-app", repo_path=git_repo)
        del reg
        reg2 = projects_mod.ProjectRegistry(path)
        assert reg2.get("my-app") is not None


class TestAgentStore:
    def _agent(self, **overrides) -> "state_mod.Agent":
        defaults = dict(
            agent_id="ma/test",
            project_label="my-app",
            worktree_path="/tmp/wt-ma-test",
            session_id="ses_abc",
            branch="ma/test",
            initial_prompt="do thing",
            phase="EXECUTING",
        )
        defaults.update(overrides)
        return state_mod.Agent(**defaults)

    def test_add_and_get_roundtrip(self, tmp_path: Path):
        store = state_mod.AgentStore(tmp_path / "agents.json")
        store.add(self._agent())
        got = store.get("ma/test")
        assert got is not None and got.session_id == "ses_abc"

    def test_add_rejects_duplicate(self, tmp_path: Path):
        store = state_mod.AgentStore(tmp_path / "agents.json")
        store.add(self._agent())
        with pytest.raises(state_mod.AgentExists):
            store.add(self._agent())

    def test_remove_returns_agent(self, tmp_path: Path):
        store = state_mod.AgentStore(tmp_path / "agents.json")
        store.add(self._agent())
        removed = store.remove("ma/test")
        assert removed is not None
        assert store.get("ma/test") is None

    def test_update_changes_phase(self, tmp_path: Path):
        store = state_mod.AgentStore(tmp_path / "agents.json")
        store.add(self._agent())
        updated = store.update("ma/test", phase="REVIEWING")
        assert updated.phase == "REVIEWING"

    def test_update_rejects_invalid_phase(self, tmp_path: Path):
        store = state_mod.AgentStore(tmp_path / "agents.json")
        store.add(self._agent())
        with pytest.raises(ValueError):
            store.update("ma/test", phase="NOT_A_PHASE")

    def test_ids_returns_set(self, tmp_path: Path):
        store = state_mod.AgentStore(tmp_path / "agents.json")
        store.add(self._agent(agent_id="ma/a"))
        store.add(self._agent(agent_id="ma/b"))
        assert store.ids() == {"ma/a", "ma/b"}
