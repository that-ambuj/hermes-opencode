from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


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
bootstrap_mod = sys.modules["_oco_test_pkg.bootstrap"]
reviewer_mod = sys.modules["_oco_test_pkg.reviewer"]
heartbeat_mod = sys.modules["_oco_test_pkg.heartbeat"]
state_mod = sys.modules["_oco_test_pkg.state"]
config_mod = sys.modules["_oco_test_pkg.config"]


class TestExtractBash:
    def test_extracts_simple_block(self):
        md = "intro\n\n```bash\necho hi\nls -la\n```\n\nafter"
        assert bootstrap_mod._extract_bash(md) == "echo hi\nls -la"

    def test_returns_none_when_no_block(self):
        assert bootstrap_mod._extract_bash("# no code here") is None

    def test_returns_none_when_block_empty(self):
        assert bootstrap_mod._extract_bash("```bash\n\n```") is None


class TestClassifyReview:
    def test_lgtm(self):
        v = reviewer_mod.classify_review("All good.\nREVIEW: LGTM")
        assert v.kind == "lgtm"

    def test_requests_changes(self):
        v = reviewer_mod.classify_review("1. Fix this.\n2. Add tests.\nREVIEW: REQUESTS_CHANGES")
        assert v.kind == "requests_changes"

    def test_ambiguous_when_no_token(self):
        v = reviewer_mod.classify_review("Looks fine to me, no further comments.")
        assert v.kind == "ambiguous"

    def test_case_insensitive(self):
        assert reviewer_mod.classify_review("review: lgtm").kind == "lgtm"


class TestHeartbeatReport:
    def _runtime(self, tmp_path: Path, agents: list[state_mod.Agent]):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
            heartbeat_day_start=9,
            heartbeat_day_end=23,
        )
        cfg.ensure_dirs()
        store = state_mod.AgentStore(cfg.agents_file)
        for a in agents:
            store.add(a)

        class _Stub:
            def __init__(self):
                self.config = cfg
                self.agents = store

        return _Stub()

    def test_no_agents_outside_window_returns_false(self, tmp_path: Path):
        rt = self._runtime(tmp_path, [])
        ok, _body = heartbeat_mod.build_report(rt, datetime(2026, 5, 17, 3, 0))
        assert ok is False

    def test_inside_window_always_sends(self, tmp_path: Path):
        rt = self._runtime(tmp_path, [])
        ok, body = heartbeat_mod.build_report(rt, datetime(2026, 5, 17, 14, 0))
        assert ok is True
        assert "no agents" in body

    def test_outside_window_with_pending_sends(self, tmp_path: Path):
        agent = state_mod.Agent(
            agent_id="ma/x", project_label="my-app", worktree_path="/tmp/x",
            session_id="ses_x", branch="ma/x", initial_prompt="x",
            phase="EXECUTING",
        )
        rt = self._runtime(tmp_path, [agent])
        ok, body = heartbeat_mod.build_report(rt, datetime(2026, 5, 17, 3, 0))
        assert ok is True
        assert "ma/x" in body
        assert "EXECUTING" in body

    def test_phase_glyphs_visible(self, tmp_path: Path):
        agents = [
            state_mod.Agent(agent_id="ma/a", project_label="my-app", worktree_path="/t/a",
                            session_id="s1", branch="ma/a", initial_prompt="x", phase="EXECUTING"),
            state_mod.Agent(agent_id="ma/b", project_label="my-app", worktree_path="/t/b",
                            session_id="s2", branch="ma/b", initial_prompt="x", phase="REVIEWING"),
            state_mod.Agent(agent_id="ma/c", project_label="my-app", worktree_path="/t/c",
                            session_id="s3", branch="ma/c", initial_prompt="x", phase="PR_OPEN",
                            pr_url="https://example.test/pr/1"),
        ]
        rt = self._runtime(tmp_path, agents)
        ok, body = heartbeat_mod.build_report(rt, datetime(2026, 5, 17, 14, 0))
        assert ok is True
        assert "ma/a" in body and "ma/b" in body and "ma/c" in body
        assert "example.test/pr/1" in body


class TestHeartbeatRetention:
    def test_done_recent_visible(self):
        agent = state_mod.Agent(
            agent_id="ma/x", project_label="my-app", worktree_path="/t",
            session_id="s", branch="b", initial_prompt="p", phase="DONE",
            done_at=time.time() - 600,
        )
        assert heartbeat_mod._visible_done(agent, time.time()) is True

    def test_done_older_than_retention_hidden(self):
        agent = state_mod.Agent(
            agent_id="ma/x", project_label="my-app", worktree_path="/t",
            session_id="s", branch="b", initial_prompt="p", phase="DONE",
            done_at=time.time() - 5 * 3600,
        )
        assert heartbeat_mod._visible_done(agent, time.time()) is False


class TestNextTopOfHour:
    def test_returns_seconds_until_next_hour(self):
        d = datetime(2026, 5, 17, 14, 30, 0)
        wait = heartbeat_mod.next_top_of_hour(d)
        assert 1700 < wait <= 1800
