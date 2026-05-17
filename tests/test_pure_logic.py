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
event_loop_mod = sys.modules["_oco_test_pkg.event_loop"]


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


class TestPrTitleFromAgentId:
    def test_refunds(self):
        assert reviewer_mod._pr_title_from_agent_id("dp/refunds") == "Refunds"

    def test_v04_polish(self):
        assert reviewer_mod._pr_title_from_agent_id("oco/v0.4-polish") == "V0.4 polish"

    def test_trailing_collision_suffix_stripped(self):
        assert reviewer_mod._pr_title_from_agent_id("oco/fix-css-2") == "Fix css"

    def test_leading_digits_preserved(self):
        assert reviewer_mod._pr_title_from_agent_id("oco/2fa-flow") == "2fa flow"

    def test_no_bracket_or_wip_artifacts(self):
        for agent_id in ("dp/refunds", "oco/v0.4-polish", "oco/fix-css-2", "oco/2fa-flow"):
            title = reviewer_mod._pr_title_from_agent_id(agent_id)
            assert "[" not in title and "]" not in title
            assert "wip" not in title.lower()
            assert "/" not in title


class TestSseBuffer:
    def test_apply_delta_appends_for_new_part(self):
        buffers: dict[str, str] = {}
        out = event_loop_mod.apply_delta(buffers, "p1", "hello")
        assert out is buffers
        assert buffers == {"p1": "hello"}

    def test_apply_delta_accumulates(self):
        buffers = {"p1": "hel"}
        event_loop_mod.apply_delta(buffers, "p1", "lo")
        event_loop_mod.apply_delta(buffers, "p1", " world")
        assert buffers == {"p1": "hello world"}

    def test_apply_snapshot_replaces(self):
        buffers = {"p1": "old"}
        event_loop_mod.apply_snapshot(buffers, "p1", "new value")
        assert buffers == {"p1": "new value"}

    def test_apply_snapshot_wins_over_partial_delta(self):
        buffers: dict[str, str] = {}
        event_loop_mod.apply_delta(buffers, "p1", "partial")
        event_loop_mod.apply_snapshot(buffers, "p1", "FULL")
        assert buffers["p1"] == "FULL"

    def test_separate_parts_are_independent(self):
        buffers: dict[str, str] = {}
        event_loop_mod.apply_delta(buffers, "p1", "alpha")
        event_loop_mod.apply_delta(buffers, "p2", "beta")
        assert buffers == {"p1": "alpha", "p2": "beta"}


class TestReviewCycleClassifier:
    def test_default_cap_one_allows_first_addressing_round(self):
        assert event_loop_mod.decide_review_action(0, 1) == "address"

    def test_at_cap_exhausts(self):
        assert event_loop_mod.decide_review_action(1, 1) == "exhausted"

    def test_higher_cap_allows_more_rounds(self):
        assert event_loop_mod.decide_review_action(1, 3) == "address"
        assert event_loop_mod.decide_review_action(3, 3) == "exhausted"


class TestExecutorOpenPrParse:
    def test_parses_canonical_pr_opened_line(self):
        text = "Did the thing.\n\nPR_OPENED: https://github.com/o/r/pull/42\n"
        url, num = reviewer_mod.parse_pr_opened(text)
        assert url == "https://github.com/o/r/pull/42"
        assert num == 42

    def test_parses_lowercase_marker(self):
        text = "pr_opened: https://github.com/o/r/pull/9"
        url, num = reviewer_mod.parse_pr_opened(text)
        assert num == 9

    def test_fallback_finds_pr_url_without_marker(self):
        text = "Opened the PR at https://github.com/o/r/pull/77 - it's there."
        parsed = reviewer_mod.parse_pr_opened(text)
        assert parsed is not None
        assert parsed[1] == 77

    def test_returns_none_when_no_pr_url(self):
        assert reviewer_mod.parse_pr_opened("Nothing here, just text.") is None
        assert reviewer_mod.parse_pr_opened("") is None
        assert reviewer_mod.parse_pr_opened(None) is None


class TestExecutorOpenPrPrompt:
    def test_includes_branch_and_base(self):
        prompt = reviewer_mod.executor_open_pr_prompt("oco/x", "main")
        assert "oco/x" in prompt
        assert "main" in prompt
        assert "PR_OPENED:" in prompt

    def test_does_not_force_identity_override(self):
        prompt = reviewer_mod.executor_open_pr_prompt("oco/x", "main")
        assert "hermes-opencode@local" not in prompt
        assert "do NOT pass" in prompt or "do not pass" in prompt.lower()
