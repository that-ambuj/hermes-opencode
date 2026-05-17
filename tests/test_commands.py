from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path


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
commands_mod = sys.modules["_oco_test_pkg.commands"]
state_mod = sys.modules["_oco_test_pkg.state"]


def _agent(**overrides):
    defaults = dict(
        agent_id="ma/x",
        project_label="my-app",
        worktree_path="/tmp/wt-ma-x",
        session_id="ses_x",
        branch="ma/x",
        initial_prompt="do x",
        phase="EXECUTING",
        last_activity_at=time.time(),
        created_at=time.time(),
    )
    defaults.update(overrides)
    return state_mod.Agent(**defaults)


class TestFmtTable:
    def test_empty_returns_no_agents_tracked(self):
        assert commands_mod._fmt_table([]) == "no agents tracked"

    def test_header_row_present(self):
        body = commands_mod._fmt_table([_agent()])
        first = body.splitlines()[0]
        for col in ("agent_id", "project", "branch", "phase", "pr", "age"):
            assert col in first

    def test_separator_under_header(self):
        body = commands_mod._fmt_table([_agent()])
        lines = body.splitlines()
        assert lines[1].strip().startswith("-")
        assert "-" * 4 in lines[1]

    def test_columns_align_across_rows(self):
        a1 = _agent(agent_id="dp/x", project_label="dodo-payments", branch="dp/x", phase="PR_OPEN", pr_number=42)
        a2 = _agent(agent_id="ma/longer-name", project_label="my-app", branch="ma/longer-name", phase="EXECUTING")
        body = commands_mod._fmt_table([a1, a2], now_ts=a1.last_activity_at)
        lines = body.splitlines()
        assert len(lines) == 4
        widths = [len(line) for line in lines if line]
        assert max(widths) - min(widths) <= 2

    def test_pr_dash_when_no_pr(self):
        body = commands_mod._fmt_table([_agent()])
        rows = body.splitlines()[2:]
        assert any("  -  " in r or r.rstrip().endswith(" -") or " - " in r for r in rows)

    def test_pr_number_shown_when_present(self):
        body = commands_mod._fmt_table([_agent(pr_number=123)])
        assert "123" in body

    def test_age_seconds(self):
        a = _agent()
        a.last_activity_at = time.time() - 5
        body = commands_mod._fmt_table([a])
        assert "5s" in body or "4s" in body or "6s" in body

    def test_age_minutes(self):
        a = _agent()
        a.last_activity_at = time.time() - 125
        body = commands_mod._fmt_table([a], now_ts=a.last_activity_at + 125)
        assert "2m" in body

    def test_age_hours(self):
        a = _agent()
        now = time.time()
        a.last_activity_at = now - 3 * 3600 - 30 * 60
        body = commands_mod._fmt_table([a], now_ts=now)
        assert "3h" in body


class TestJoinBufferText:
    def test_empty_items_returns_empty_string(self):
        assert commands_mod._join_buffer_text([], 80) == ""

    def test_items_with_no_text_parts_returns_empty(self):
        items = [{"id": "msg_1", "parts": [{"type": "tool", "name": "foo"}]}]
        assert commands_mod._join_buffer_text(items, 80) == ""

    def test_ordering_preserves_item_then_part_order(self):
        items = [
            {"id": "m1", "parts": [{"type": "text", "text": "first"}]},
            {"id": "m2", "parts": [{"type": "text", "text": "second"}, {"type": "text", "text": "third"}]},
        ]
        out = commands_mod._join_buffer_text(items, 80)
        assert out == "first\nsecond\nthird"

    def test_lines_truncates_to_last_n(self):
        items = [{"id": f"m{i}", "parts": [{"type": "text", "text": f"line{i}"}]} for i in range(10)]
        out = commands_mod._join_buffer_text(items, 3)
        assert out.splitlines() == ["line7", "line8", "line9"]

    def test_lines_zero_returns_all(self):
        items = [{"id": "m1", "parts": [{"type": "text", "text": "a\nb\nc"}]}]
        out = commands_mod._join_buffer_text(items, 0)
        assert out == "a\nb\nc"

    def test_multiline_chunks_split_on_newline(self):
        items = [{"id": "m1", "parts": [{"type": "text", "text": "alpha\nbeta\ngamma\ndelta"}]}]
        out = commands_mod._join_buffer_text(items, 2)
        assert out == "gamma\ndelta"


class TestParseOcAttachArgs:
    def test_missing_agent_id_returns_error(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("")
        assert agent_id is None
        assert err is not None
        assert "usage" in err.lower()

    def test_only_agent_id_uses_default_lines(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("dp/refunds")
        assert agent_id == "dp/refunds"
        assert lines == commands_mod.DEFAULT_ATTACH_LINES
        assert err is None

    def test_explicit_lines_flag(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("dp/refunds --lines 200")
        assert agent_id == "dp/refunds"
        assert lines == 200
        assert err is None

    def test_lines_flag_without_value(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("dp/refunds --lines")
        assert err is not None
        assert "lines" in err

    def test_lines_flag_with_non_integer(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("dp/x --lines abc")
        assert err is not None

    def test_negative_lines_rejected(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("dp/x --lines -3")
        assert err is not None

    def test_unexpected_token(self):
        agent_id, lines, err = commands_mod._parse_oc_attach_args("dp/x --bogus")
        assert err is not None


class TestOcQuestionsHandler:
    def test_empty_snapshot_returns_no_pending(self, tmp_path: Path, monkeypatch):
        event_loop = sys.modules["_oco_test_pkg.event_loop"]
        monkeypatch.setattr(event_loop, "get_pending_snapshot", lambda: ({}, {}))

        class _Stub:
            agents = None
            client = None

        handler = commands_mod.make_oc_questions(_Stub())
        assert handler("") == "no pending questions"

    def test_question_block_includes_options(self, tmp_path: Path, monkeypatch):
        event_loop = sys.modules["_oco_test_pkg.event_loop"]
        snap = {
            "dp/x": [
                {
                    "id": "q_abc",
                    "questions": [{
                        "question": "Continue?",
                        "options": [
                            {"label": "yes", "description": "go ahead"},
                            {"label": "no", "description": "stop here"},
                        ],
                    }],
                }
            ]
        }
        monkeypatch.setattr(event_loop, "get_pending_snapshot", lambda: (snap, {}))

        class _Stub:
            agents = None
            client = None

        handler = commands_mod.make_oc_questions(_Stub())
        out = handler("")
        assert "[dp/x]" in out
        assert "q_abc" in out
        assert "Continue?" in out
        assert "'yes'" in out and "go ahead" in out
        assert "'no'" in out and "stop here" in out


class TestOcListHandler:
    def test_empty_returns_no_agents_tracked(self, tmp_path: Path):
        state = sys.modules["_oco_test_pkg.state"]
        store = state.AgentStore(tmp_path / "agents.json")

        class _Stub:
            def __init__(self):
                self.agents = store

        handler = commands_mod.make_oc_list(_Stub())
        assert handler("") == "no agents tracked"

    def test_lists_agents(self, tmp_path: Path):
        state = sys.modules["_oco_test_pkg.state"]
        store = state.AgentStore(tmp_path / "agents.json")
        store.add(_agent(agent_id="dp/refunds", project_label="dodo-payments", branch="dp/refunds"))
        store.add(_agent(agent_id="ma/x", project_label="my-app", branch="ma/x"))

        class _Stub:
            def __init__(self):
                self.agents = store

        out = commands_mod.make_oc_list(_Stub())("")
        assert "dp/refunds" in out
        assert "ma/x" in out
        assert "agent_id" in out.splitlines()[0]
