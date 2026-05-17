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


class TestFmtList:
    def test_empty_returns_no_agents_tracked(self):
        assert commands_mod._fmt_list([]) == "no agents tracked"

    def test_no_header_row(self):
        body = commands_mod._fmt_list([_agent()])
        assert "agent_id" not in body
        assert not body.startswith("-")

    def test_phase_glyph_present(self):
        body = commands_mod._fmt_list([_agent(phase="EXECUTING")])
        first = body.splitlines()[0]
        assert "▶" in first

    def test_primary_line_per_agent(self):
        a1 = _agent(agent_id="dp/x", project_label="dodo-payments", branch="dp/x", phase="PR_OPEN", pr_number=42, pr_url="https://example.test/pr/42")
        a2 = _agent(agent_id="ma/longer-name", project_label="my-app", branch="ma/longer-name", phase="EXECUTING")
        body = commands_mod._fmt_list([a1, a2], now_ts=a1.last_activity_at)
        assert "dp/x" in body and "ma/longer-name" in body
        assert "PR_OPEN" in body and "EXECUTING" in body
        assert "example.test/pr/42" in body

    def test_pr_omitted_when_no_pr(self):
        body = commands_mod._fmt_list([_agent()])
        assert "PR #" not in body
        assert "/pull/" not in body

    def test_pr_url_promoted_to_primary_line(self):
        body = commands_mod._fmt_list([_agent(pr_number=123, phase="PR_OPEN", pr_url="https://x.test/pr/123")])
        first_line = body.splitlines()[0]
        assert "x.test/pr/123" in first_line

    def test_pr_number_shown_when_url_missing(self):
        body = commands_mod._fmt_list([_agent(pr_number=99, phase="PR_OPEN")])
        assert "PR #99" in body

    def test_archived_hidden_by_default(self):
        live = _agent(agent_id="ma/a")
        archived = _agent(agent_id="ma/b", phase="DONE", archived=True)
        body = commands_mod._fmt_list([live, archived])
        assert "ma/a" in body
        assert "ma/b" not in body

    def test_archived_shown_with_include_archived(self):
        live = _agent(agent_id="ma/a")
        archived = _agent(agent_id="ma/b", phase="DONE", archived=True, pr_url="https://x.test/pr/9")
        body = commands_mod._fmt_list([live, archived], include_archived=True)
        assert "ma/a" in body
        assert "ma/b" in body
        assert "archived" in body

    def test_only_archived_hidden_returns_hint(self):
        archived = _agent(agent_id="ma/old", phase="DONE", archived=True)
        body = commands_mod._fmt_list([archived])
        assert "--archived" in body

    def test_failed_includes_error_continuation(self):
        body = commands_mod._fmt_list([_agent(phase="FAILED", last_error="reviewer staging: not a git repo")])
        assert "FAILED" in body
        assert "reviewer staging" in body
        assert body.splitlines()[1].startswith("    error:")

    def test_age_seconds(self):
        a = _agent()
        a.last_activity_at = time.time() - 5
        body = commands_mod._fmt_list([a])
        assert "5s" in body or "4s" in body or "6s" in body

    def test_age_minutes(self):
        a = _agent()
        a.last_activity_at = time.time() - 125
        body = commands_mod._fmt_list([a], now_ts=a.last_activity_at + 125)
        assert "2m" in body

    def test_age_hours(self):
        a = _agent()
        now = time.time()
        a.last_activity_at = now - 3 * 3600 - 30 * 60
        body = commands_mod._fmt_list([a], now_ts=now)
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
        assert "EXECUTING" in out or "DONE" in out or "FAILED" in out

    def test_archived_flag_includes_archived(self, tmp_path: Path):
        state = sys.modules["_oco_test_pkg.state"]
        store = state.AgentStore(tmp_path / "agents.json")
        store.add(_agent(agent_id="dp/live", project_label="dodo-payments", branch="dp/live"))
        store.add(_agent(agent_id="dp/old", project_label="dodo-payments", branch="dp/old", phase="DONE", archived=True))

        class _Stub:
            def __init__(self):
                self.agents = store

        out_default = commands_mod.make_oc_list(_Stub())("")
        assert "dp/live" in out_default
        assert "dp/old" not in out_default

        for flag in ("--archived", "--all", "-a"):
            out = commands_mod.make_oc_list(_Stub())(flag)
            assert "dp/live" in out, f"flag={flag}"
            assert "dp/old" in out, f"flag={flag}"

    def test_unknown_list_arg_returns_usage(self, tmp_path: Path):
        state = sys.modules["_oco_test_pkg.state"]
        store = state.AgentStore(tmp_path / "agents.json")

        class _Stub:
            def __init__(self):
                self.agents = store

        out = commands_mod.make_oc_list(_Stub())("--bogus")
        assert "unknown arg" in out
        assert "/oc list" in out


class TestOcDispatcher:
    def _stub_runtime(self, tmp_path: Path):
        state = sys.modules["_oco_test_pkg.state"]
        store = state.AgentStore(tmp_path / "agents.json")
        store.add(_agent(agent_id="dp/refunds", project_label="dodo-payments", branch="dp/refunds"))

        class _Stub:
            agents = store
            client = None

        return _Stub()

    def test_no_args_prints_help(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("")
        assert out.startswith("/oc")
        assert "list" in out and "attach" in out and "questions" in out

    def test_help_subcommand_prints_help(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("help")
        assert "subcommands" in out

    def test_dash_help_flag_prints_help(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("--help")
        assert "subcommands" in out

    def test_list_routes_to_oc_list(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("list")
        assert "dp/refunds" in out

    def test_attach_routes_with_remaining_args(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("attach")
        assert "usage:" in out and "/oc attach" in out

    def test_questions_routes_to_oc_questions(self, tmp_path: Path, monkeypatch):
        event_loop = sys.modules["_oco_test_pkg.event_loop"]
        monkeypatch.setattr(event_loop, "get_pending_snapshot", lambda: ({}, {}))
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("questions")
        assert out == "no pending questions"

    def test_unknown_subcommand_includes_help(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("frobnicate")
        assert "unknown" in out.lower()
        assert "subcommands" in out

    def test_subcommand_is_case_insensitive(self, tmp_path: Path):
        rt = self._stub_runtime(tmp_path)
        out = commands_mod.make_oc_dispatcher(rt)("LIST")
        assert "dp/refunds" in out
