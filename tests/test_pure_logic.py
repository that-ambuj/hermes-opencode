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
plugin_mod = sys.modules["_oco_test_pkg"]
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


class TestServeRestartBackoff:
    def test_first_attempt_uses_base_delay(self):
        assert event_loop_mod._compute_serve_restart_delay(1) == 1.0

    def test_doubles_each_attempt(self):
        assert event_loop_mod._compute_serve_restart_delay(2) == 2.0
        assert event_loop_mod._compute_serve_restart_delay(3) == 4.0
        assert event_loop_mod._compute_serve_restart_delay(4) == 8.0
        assert event_loop_mod._compute_serve_restart_delay(5) == 16.0

    def test_zero_or_negative_attempt_clamped(self):
        assert event_loop_mod._compute_serve_restart_delay(0) == 1.0
        assert event_loop_mod._compute_serve_restart_delay(-3) == 1.0

    def test_respects_custom_base(self):
        assert event_loop_mod._compute_serve_restart_delay(3, base=0.5) == 2.0

    def test_max_attempts_is_five(self):
        assert event_loop_mod.SERVE_RESTART_MAX_ATTEMPTS == 5

    def test_notify_targets_all_three_sinks(self):
        assert set(event_loop_mod.SERVE_DOWN_NOTIFY_SINKS) == {"cli", "dashboard", "gateway"}


class TestServeDownNotificationBody:
    def _runtime(self, tmp_path: Path):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
            events_log=tmp_path / "events.log",
            server_url="http://127.0.0.1:9999",
        )
        cfg.ensure_dirs()

        class _Stub:
            def __init__(self):
                self.config = cfg

        return _Stub()

    def test_body_includes_server_url_and_attempt_count(self, tmp_path: Path):
        rt = self._runtime(tmp_path)
        with patch.object(event_loop_mod, "_runtime", rt):
            title, body, meta = event_loop_mod._build_serve_down_notification()
        assert "unreachable" in title.lower()
        assert "127.0.0.1:9999" in body
        assert str(event_loop_mod.SERVE_RESTART_MAX_ATTEMPTS) in body
        assert meta["kind"] == "serve_down"
        assert meta["server_url"] == "http://127.0.0.1:9999"
        assert meta["attempts"] == event_loop_mod.SERVE_RESTART_MAX_ATTEMPTS


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


class TestHomeChannelAutoDetect:
    def test_discovers_first_available_platform(self, monkeypatch):
        monkeypatch.delenv("BLUEBUBBLES_HOME_CHANNEL", raising=False)
        monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "@me")
        result = config_mod.discover_home_channel()
        assert result == ("telegram", "@me", "env:TELEGRAM_HOME_CHANNEL")

    def test_bluebubbles_takes_priority_over_telegram(self, monkeypatch):
        monkeypatch.setenv("BLUEBUBBLES_HOME_CHANNEL", "+1234")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "@me")
        result = config_mod.discover_home_channel()
        assert result[0] == "bluebubbles"

    def test_returns_none_when_no_env_set(self, monkeypatch):
        for plat in config_mod.HOME_CHANNEL_PLATFORMS:
            monkeypatch.delenv(f"{plat.upper()}_HOME_CHANNEL", raising=False)
        assert config_mod.discover_home_channel() is None


class TestConfigSmartSinks:
    def _clear_home_envs(self, monkeypatch):
        for plat in config_mod.HOME_CHANNEL_PLATFORMS:
            monkeypatch.delenv(f"{plat.upper()}_HOME_CHANNEL", raising=False)

    def test_default_falls_back_to_cli_dashboard_without_home_channel(self, monkeypatch):
        self._clear_home_envs(monkeypatch)
        cfg = config_mod.Config.from_plugin_entry({})
        assert cfg.notify_sinks == ["cli", "dashboard"]
        assert cfg.notify_gateway_platform is None
        assert cfg.notify_discovery_source is None

    def test_default_uses_gateway_when_home_channel_detected(self, monkeypatch):
        self._clear_home_envs(monkeypatch)
        monkeypatch.setenv("BLUEBUBBLES_HOME_CHANNEL", "+1234")
        cfg = config_mod.Config.from_plugin_entry({})
        assert cfg.notify_sinks == ["gateway", "dashboard"]
        assert cfg.notify_gateway_platform == "bluebubbles"
        assert cfg.notify_gateway_chat_id == "+1234"
        assert cfg.notify_discovery_source == "env:BLUEBUBBLES_HOME_CHANNEL"

    def test_explicit_sinks_override_default(self, monkeypatch):
        self._clear_home_envs(monkeypatch)
        monkeypatch.setenv("BLUEBUBBLES_HOME_CHANNEL", "+1234")
        cfg = config_mod.Config.from_plugin_entry({"notify": {"sinks": ["cli"]}})
        assert cfg.notify_sinks == ["cli"]
        assert cfg.notify_gateway_platform == "bluebubbles"

    def test_explicit_platform_overrides_auto_detect(self, monkeypatch):
        self._clear_home_envs(monkeypatch)
        monkeypatch.setenv("BLUEBUBBLES_HOME_CHANNEL", "+1234")
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "@me")
        cfg = config_mod.Config.from_plugin_entry({"notify": {"gateway": {"platform": "telegram"}}})
        assert cfg.notify_gateway_platform == "telegram"
        assert cfg.notify_gateway_chat_id == "@me"
        assert cfg.notify_discovery_source == "env:TELEGRAM_HOME_CHANNEL"

    def test_explicit_chat_id_marks_discovery_explicit(self, monkeypatch):
        self._clear_home_envs(monkeypatch)
        cfg = config_mod.Config.from_plugin_entry({
            "notify": {"gateway": {"platform": "telegram", "chat_id": "@boss"}}
        })
        assert cfg.notify_gateway_chat_id == "@boss"
        assert cfg.notify_discovery_source == "explicit"

    def test_cancelled_in_default_events(self, monkeypatch):
        self._clear_home_envs(monkeypatch)
        cfg = config_mod.Config.from_plugin_entry({})
        assert "cancelled" in cfg.notify_events


class TestPrOpenCancelOnClosed:
    def _setup(self, tmp_path: Path, pr_state_value: str, monkeypatch):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()

        store = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="ma/x", project_label="my-app", worktree_path=str(tmp_path / "wt-x"),
            session_id="s", branch="ma/x", initial_prompt="p", phase="PR_OPEN",
            pr_url="https://github.test/o/r/pull/9", pr_number=9,
        )
        store.add(agent)
        (tmp_path / "wt-x").mkdir(exist_ok=True)

        class _Rt:
            def __init__(self):
                self.config = cfg
                self.agents = store
                self.projects = None
                self.client = None

        rt = _Rt()
        monkeypatch.setattr(event_loop_mod, "_runtime", rt)

        pr_mod = sys.modules["_oco_test_pkg.pr"]
        merged_at = 12345.0 if pr_state_value == "MERGED" else None

        def _stub_state(worktree, number):
            return pr_mod.PrInfo(number=number, url="https://x", state=pr_state_value, merged_at=merged_at)
        monkeypatch.setattr(pr_mod, "pr_state", _stub_state)

        async def _stub_cleanup(_agent, _worktree):
            return None
        monkeypatch.setattr(event_loop_mod, "_cleanup_worktrees", _stub_cleanup)

        notified = []
        monkeypatch.setattr(event_loop_mod, "_maybe_notify_phase",
                            lambda agent, kind, body="": notified.append(kind))
        return store, agent, notified

    def test_closed_transitions_to_cancelled_with_reason(self, tmp_path: Path, monkeypatch):
        import asyncio
        store, agent, notified = self._setup(tmp_path, "CLOSED", monkeypatch)
        asyncio.run(event_loop_mod._phase_pr_open(agent))
        after = store.get("ma/x")
        assert after.phase == "CANCELLED"
        assert after.cancelled_at is not None
        assert "PR #9 closed without merge" in (after.cancellation_reason or "")
        assert notified == ["cancelled"]

    def test_merged_transitions_to_done(self, tmp_path: Path, monkeypatch):
        import asyncio
        store, agent, notified = self._setup(tmp_path, "MERGED", monkeypatch)
        asyncio.run(event_loop_mod._phase_pr_open(agent))
        after = store.get("ma/x")
        assert after.phase == "DONE"
        assert after.done_at is not None
        assert notified == ["done"]

    def test_open_does_not_transition(self, tmp_path: Path, monkeypatch):
        import asyncio
        store, agent, notified = self._setup(tmp_path, "OPEN", monkeypatch)

        async def _stub_sleep(_):
            return None
        monkeypatch.setattr(event_loop_mod.asyncio, "sleep", _stub_sleep)
        asyncio.run(event_loop_mod._phase_pr_open(agent))
        after = store.get("ma/x")
        assert after.phase == "PR_OPEN"
        assert notified == []


class TestPrunerArchivesCancelled:
    def test_archives_cancelled_after_threshold(self, tmp_path: Path, monkeypatch):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        store = state_mod.AgentStore(cfg.agents_file)
        old = time.time() - (13 * 3600)
        agent = state_mod.Agent(
            agent_id="ma/old", project_label="my-app", worktree_path="/t",
            session_id="s", branch="ma/old", initial_prompt="p", phase="CANCELLED",
            cancelled_at=old, cancellation_reason="user cancelled",
        )
        store.add(agent)

        class _Rt:
            def __init__(self):
                self.config = cfg
                self.agents = store
                self.projects = None

        rt = _Rt()
        monkeypatch.setattr(event_loop_mod, "_runtime", rt)
        monkeypatch.setattr(event_loop_mod, "_archive_done", lambda _a: None)

        for ag in list(store.list()):
            if ag.archived:
                continue
            done_ts = ag.done_at if ag.phase == "DONE" else (ag.cancelled_at if ag.phase == "CANCELLED" else None)
            if done_ts and (time.time() - done_ts) > event_loop_mod.ARCHIVE_AFTER_SEC:
                store.update(ag.agent_id, archived=True, archived_at=time.time())

        after = store.get("ma/old")
        assert after.archived is True
        assert after.archived_at is not None

    def test_recent_cancelled_not_archived(self, tmp_path: Path):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        store = state_mod.AgentStore(cfg.agents_file)
        recent = time.time() - 60
        agent = state_mod.Agent(
            agent_id="ma/recent", project_label="my-app", worktree_path="/t",
            session_id="s", branch="ma/recent", initial_prompt="p", phase="CANCELLED",
            cancelled_at=recent,
        )
        store.add(agent)
        for ag in list(store.list()):
            done_ts = ag.cancelled_at if ag.phase == "CANCELLED" else None
            assert done_ts is not None
            assert (time.time() - done_ts) < event_loop_mod.ARCHIVE_AFTER_SEC
        after = store.get("ma/recent")
        assert after.archived is False


class TestPreLlmCallHookDispatcherDirective:
    """v0.14.3: pre_llm_call hook injects DISPATCHER MODE directive so the
    hermes chat LLM forwards the human's task to opencode verbatim instead
    of planning/decomposing first. The same hook still carries the pending
    questions/permissions block when those exist.
    """

    def setup_method(self):
        self._saved_runtime = plugin_mod._runtime
        self._saved_snapshot = event_loop_mod.get_pending_snapshot

    def teardown_method(self):
        plugin_mod._runtime = self._saved_runtime
        event_loop_mod.get_pending_snapshot = self._saved_snapshot

    def test_no_runtime_returns_none(self):
        plugin_mod._runtime = None
        assert plugin_mod._build_pre_llm_context() is None
        assert plugin_mod._pre_llm_call_hook() is None

    def test_runtime_set_no_pending_returns_directive_only(self):
        plugin_mod._runtime = object()
        event_loop_mod.get_pending_snapshot = lambda: ({}, {})
        ctx = plugin_mod._build_pre_llm_context()
        assert ctx is not None
        assert "DISPATCHER MODE" in ctx
        assert "FULL authority" in ctx
        assert "VERBATIM" in ctx
        assert "Do NOT plan" in ctx
        assert "ASK THE HUMAN" in ctx
        assert "pending items" not in ctx
        # hook wraps context in {"context": ...}
        assert plugin_mod._pre_llm_call_hook() == {"context": ctx}

    def test_runtime_set_with_pending_directive_precedes_pending(self):
        plugin_mod._runtime = object()
        event_loop_mod.get_pending_snapshot = lambda: (
            {
                "oco/fix-x": [
                    {
                        "id": "q1",
                        "questions": [
                            {
                                "question": "Use option A or B?",
                                "options": [
                                    {"label": "A", "description": "first"},
                                    {"label": "B", "description": "second"},
                                ],
                                "multiple": False,
                                "custom": False,
                            }
                        ],
                    }
                ],
            },
            {
                "oco/fix-x": [
                    {"id": "p1", "permission": "bash", "patterns": ["rm -rf *"]}
                ],
            },
        )
        ctx = plugin_mod._build_pre_llm_context()
        assert ctx is not None
        assert "DISPATCHER MODE" in ctx
        assert "pending items" in ctx
        assert "q1" in ctx and "p1" in ctx
        assert "Use option A or B?" in ctx
        # directive must come FIRST so it's not buried under pending noise
        assert ctx.index("DISPATCHER MODE") < ctx.index("pending items")
        # blocks separated by blank line
        assert "\n\n[hermes-opencode] pending items" in ctx

    def test_directive_has_no_em_dash(self):
        # AGENTS.md anti-pattern: em-dashes in code. Blocking violation.
        assert "\u2014" not in plugin_mod._DISPATCHER_DIRECTIVE


class TestSpawnSchemaDispatcherWording:
    """v0.14.3: oc_spawn / oc_send tool descriptions explicitly forbid
    the hermes chat LLM from planning/paraphrasing/decomposing the prompt.
    """

    def setup_method(self):
        self._tools_mod = sys.modules["_oco_test_pkg.tools"]

    def test_spawn_schema_forbids_planning(self):
        desc = self._tools_mod.SPAWN_SCHEMA["description"]
        assert "VERBATIM" in desc
        assert "FULL authority" in desc
        assert "DISPATCHER" in desc
        for forbidden_verb in ["plan, analyze", "paraphrase", "improve"]:
            assert forbidden_verb in desc, f"missing forbidden-verb wording: {forbidden_verb}"

    def test_spawn_prompt_param_forbids_planning(self):
        pd = self._tools_mod.SPAWN_SCHEMA["parameters"]["properties"]["prompt"]["description"]
        assert "VERBATIM" in pd
        assert "No planning" in pd
        assert "literal words" in pd
        assert "ASK the human" in pd

    def test_send_schema_forbids_planning(self):
        desc = self._tools_mod.SEND_SCHEMA["description"]
        assert "VERBATIM" in desc
        assert "dispatcher" in desc
        assert "opencode owns the task" in desc

    def test_send_text_param_forbids_planning(self):
        td = self._tools_mod.SEND_SCHEMA["parameters"]["properties"]["text"]["description"]
        assert "VERBATIM" in td
        assert "No planning" in td


class TestSendIsAsyncFireAndForget:
    """v0.14.4: oc_send uses send_message_async (fire-and-forget) instead of
    the blocking send_message. Mirrors the v0.3.1 -> v0.3.2 fix for oc_spawn:
    sync send_message in a hermes tool dispatcher blocked the hermes main
    session for up to 600s while opencode streamed the full assistant turn,
    creating perceived message queuing. AGENTS.md rule: any code path called
    synchronously by a hermes tool dispatcher must use send_message_async.
    """

    def setup_method(self):
        self._tools_mod = sys.modules["_oco_test_pkg.tools"]
        self._state_mod = sys.modules["_oco_test_pkg.state"]

    def test_send_schema_has_no_timeout_param(self):
        props = self._tools_mod.SEND_SCHEMA["parameters"]["properties"]
        assert "timeout_sec" not in props, (
            "timeout_sec was meaningful only for the blocking send_message "
            "path; with send_message_async the queue POST itself has a "
            "30s ceiling inside transport.py and no per-call timeout is exposed."
        )

    def test_send_schema_documents_async_behavior(self):
        desc = self._tools_mod.SEND_SCHEMA["description"]
        for needle in [
            "queued asynchronously",
            "returns immediately",
            "NOT come back in the tool result",
            "oc_status or oc_wait",
        ]:
            assert needle in desc, f"missing async-behavior wording: {needle!r}"

    def test_send_schema_still_carries_dispatcher_discipline(self):
        desc = self._tools_mod.SEND_SCHEMA["description"]
        for needle in ["VERBATIM", "dispatcher", "Do NOT plan", "opencode owns the task"]:
            assert needle in desc, f"v0.14.3 dispatcher wording regressed: {needle!r}"

    def test_make_send_calls_send_message_async_and_returns_queued(self, tmp_path):
        import asyncio
        import json
        import time

        config_mod = sys.modules["_oco_test_pkg.config"]
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = self._state_mod.AgentStore(cfg.agents_file)
        agent = self._state_mod.Agent(
            agent_id="bck/fix-discount",
            project_label="bck",
            worktree_path=str(tmp_path / "wt-x"),
            session_id="sess-1",
            branch="bck/fix-discount",
            initial_prompt="p",
            phase="EXECUTING",
        )
        agents.add(agent)

        async_calls: list[tuple] = []
        sync_calls: list[tuple] = []

        class FakeClient:
            async def send_message_async(self, session_id, directory, text, timeout=30.0):
                async_calls.append((session_id, str(directory), text))
                return {"queued": True}

            async def send_message(self, *a, **kw):
                sync_calls.append((a, kw))
                return {"info": {"finish": "done"}}

        runtime = self._tools_mod.Runtime(
            config=cfg, client=FakeClient(), projects=None, agents=agents,
        )
        handler = self._tools_mod.make_send(runtime)
        result_str = asyncio.run(handler({
            "agent_id": "bck/fix-discount",
            "text": "see reptiles review on the PR",
        }))
        result = json.loads(result_str)

        assert result["ok"] is True, result
        data = result["data"]
        assert data["agent_id"] == "bck/fix-discount"
        assert data["queued"] is True
        assert "oc_status" in data["note"]
        assert "assistant_text" not in data, "v0.14.4 dropped assistant_text from oc_send result"
        assert len(async_calls) == 1
        assert async_calls[0] == ("sess-1", str(tmp_path / "wt-x"), "see reptiles review on the PR")
        assert sync_calls == [], "blocking send_message must NOT be called"

    def test_make_send_unknown_agent_returns_error(self, tmp_path):
        import asyncio
        import json
        config_mod = sys.modules["_oco_test_pkg.config"]
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = self._state_mod.AgentStore(cfg.agents_file)

        class FakeClient:
            async def send_message_async(self, *a, **kw):
                raise AssertionError("should not be called for unknown agent")

        runtime = self._tools_mod.Runtime(
            config=cfg, client=FakeClient(), projects=None, agents=agents,
        )
        handler = self._tools_mod.make_send(runtime)
        result = json.loads(asyncio.run(handler({"agent_id": "no/such", "text": "x"})))
        assert result["ok"] is False
        assert "unknown agent" in result["error"]


class TestAtAgentDirectDispatch:
    """v0.14.4: `@<agent_id> <body>` shortcut in pre_gateway_dispatch routes
    a message directly to a live agent's opencode session, bypassing the
    hermes chat LLM. Eliminates both paraphrasing (no chat LLM in path) and
    queuing (uses send_message_async fire-and-forget).
    """

    def setup_method(self):
        self._tools_mod = sys.modules["_oco_test_pkg.tools"]
        self._state_mod = sys.modules["_oco_test_pkg.state"]
        self._config_mod = sys.modules["_oco_test_pkg.config"]
        self._saved_runtime = plugin_mod._runtime

    def teardown_method(self):
        plugin_mod._runtime = self._saved_runtime

    def _make_runtime(self, tmp_path, agents_to_add):
        cfg = self._config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        store = self._state_mod.AgentStore(cfg.agents_file)
        for a in agents_to_add:
            store.add(a)

        captured: list = []

        class FakeClient:
            async def send_message_async(self, session_id, directory, text, timeout=30.0):
                captured.append({
                    "session_id": session_id,
                    "directory": str(directory),
                    "text": text,
                })
                return {"queued": True}

        runtime = self._tools_mod.Runtime(
            config=cfg, client=FakeClient(), projects=None, agents=store,
        )
        plugin_mod._runtime = runtime
        return runtime, captured

    def _fake_event_gateway(self, text: str):
        sent: list[str] = []
        class Source:
            platform = "test"
            chat_id = "c1"
            thread_id = None
        class Event:
            pass
        ev = Event()
        ev.text = text
        ev.source = Source()
        gw = object()
        return ev, gw, sent

    def test_regex_matches_simple_id(self):
        m = plugin_mod._AT_AGENT_RE.match("@bck/fix-discount see reptiles review on the PR")
        assert m is not None
        assert m.group(1) == "bck/fix-discount"
        assert m.group(2) == "see reptiles review on the PR"

    def test_regex_matches_mixed_case_abbrev(self):
        m = plugin_mod._AT_AGENT_RE.match("@BCK/fix-discount hello")
        assert m is not None and m.group(1) == "BCK/fix-discount"

    def test_regex_matches_multiline_body(self):
        m = plugin_mod._AT_AGENT_RE.match("@bck/fix-discount line1\nline2\nline3")
        assert m is not None
        assert m.group(2) == "line1\nline2\nline3"

    def test_regex_rejects_no_slash(self):
        assert plugin_mod._AT_AGENT_RE.match("@username hi") is None

    def test_regex_rejects_not_at_start(self):
        assert plugin_mod._AT_AGENT_RE.match("hey @bck/fix-discount hi") is None

    def test_no_runtime_returns_none(self, tmp_path):
        plugin_mod._runtime = None
        ev, gw, _ = self._fake_event_gateway("@bck/fix-discount hi")
        assert plugin_mod._handle_at_agent_dispatch(ev, gw, ev.text) is None

    def test_non_at_message_falls_through(self, tmp_path):
        self._make_runtime(tmp_path, [])
        ev, gw, _ = self._fake_event_gateway("hello world")
        assert plugin_mod._handle_at_agent_dispatch(ev, gw, ev.text) is None

    def test_unknown_agent_falls_through_silently(self, tmp_path):
        self._make_runtime(tmp_path, [])
        ev, gw, _ = self._fake_event_gateway("@nobody/here hi")
        assert plugin_mod._handle_at_agent_dispatch(ev, gw, ev.text) is None, (
            "unresolvable agent_id must fall through to chat LLM so unrelated "
            "@mentions in group chats are not eaten by the orchestrator"
        )

    def test_terminal_phase_rejected_with_skip(self, tmp_path, monkeypatch):
        agent = self._state_mod.Agent(
            agent_id="bck/done", project_label="bck",
            worktree_path=str(tmp_path / "wt"), session_id="s1",
            branch="bck/done", initial_prompt="p", phase="DONE",
        )
        rt, captured = self._make_runtime(tmp_path, [agent])
        echoed = []
        monkeypatch.setattr(plugin_mod, "_gateway_send", lambda gw, ev, msg: echoed.append(msg))
        ev, gw, _ = self._fake_event_gateway("@bck/done try again")
        result = plugin_mod._handle_at_agent_dispatch(ev, gw, ev.text)
        assert result == {"action": "skip", "reason": "@bck/done terminal phase"}
        assert captured == []
        assert any("phase=DONE" in m for m in echoed)

    def test_empty_body_rejected_with_skip(self, tmp_path, monkeypatch):
        agent = self._state_mod.Agent(
            agent_id="bck/live", project_label="bck",
            worktree_path=str(tmp_path / "wt"), session_id="s1",
            branch="bck/live", initial_prompt="p", phase="EXECUTING",
        )
        rt, captured = self._make_runtime(tmp_path, [agent])
        echoed = []
        monkeypatch.setattr(plugin_mod, "_gateway_send", lambda gw, ev, msg: echoed.append(msg))
        ev, gw, _ = self._fake_event_gateway("@bck/live")
        result = plugin_mod._handle_at_agent_dispatch(ev, gw, ev.text)
        assert result == {"action": "skip", "reason": "@bck/live empty body"}
        assert captured == []
        assert any("empty message" in m for m in echoed)

    def test_valid_dispatch_calls_send_message_async_and_short_circuits(self, tmp_path, monkeypatch):
        agent = self._state_mod.Agent(
            agent_id="bck/fix-discount", project_label="bck",
            worktree_path=str(tmp_path / "wt-x"), session_id="sess-42",
            branch="bck/fix-discount", initial_prompt="p", phase="EXECUTING",
        )
        rt, captured = self._make_runtime(tmp_path, [agent])
        echoed = []
        monkeypatch.setattr(plugin_mod, "_gateway_send", lambda gw, ev, msg: echoed.append(msg))
        ev, gw, _ = self._fake_event_gateway(
            "@bck/fix-discount see reptiles review on the PR"
        )
        result = plugin_mod._handle_at_agent_dispatch(ev, gw, ev.text)
        assert result == {
            "action": "skip",
            "reason": "@bck/fix-discount dispatched",
        }
        assert len(captured) == 1
        assert captured[0]["session_id"] == "sess-42"
        assert captured[0]["text"] == "see reptiles review on the PR"
        assert captured[0]["directory"] == str(tmp_path / "wt-x")
        assert any("-> @bck/fix-discount" in m for m in echoed)

    def test_hook_dispatches_at_agent_before_slash_oc(self, tmp_path, monkeypatch):
        agent = self._state_mod.Agent(
            agent_id="bck/live", project_label="bck",
            worktree_path=str(tmp_path / "wt"), session_id="s1",
            branch="bck/live", initial_prompt="p", phase="EXECUTING",
        )
        rt, captured = self._make_runtime(tmp_path, [agent])
        monkeypatch.setattr(plugin_mod, "_gateway_send", lambda gw, ev, msg: None)
        monkeypatch.setattr(plugin_mod, "_oc_dispatcher_cache", lambda _raw: "should-not-run")
        ev, gw, _ = self._fake_event_gateway("@bck/live hi there")
        result = plugin_mod._pre_gateway_dispatch_hook(event=ev, gateway=gw)
        assert result == {"action": "skip", "reason": "@bck/live dispatched"}
        assert len(captured) == 1, "valid @ dispatch must take precedence over /oc parser"

    def test_hook_falls_back_to_slash_oc_when_no_at_match(self, tmp_path, monkeypatch):
        rt, captured = self._make_runtime(tmp_path, [])
        monkeypatch.setattr(plugin_mod, "_gateway_send", lambda gw, ev, msg: None)
        slash_called: list[str] = []
        def fake_dispatcher(raw):
            slash_called.append(raw)
            return "ok"
        monkeypatch.setattr(plugin_mod, "_oc_dispatcher_cache", fake_dispatcher)
        ev, gw, _ = self._fake_event_gateway("/oc list")
        result = plugin_mod._pre_gateway_dispatch_hook(event=ev, gateway=gw)
        assert result == {"action": "skip", "reason": "/oc handled inline"}
        assert slash_called == ["list"]
        assert captured == [], "send_message_async must NOT be called for /oc"


class TestHostConfig:
    """v0.14.5 introduced a bind-host override for `opencode serve`; v0.16.0
    renamed the YAML / dataclass / kwarg knob `serve_hostname` -> `host`.
    The outgoing CLI flag stays `--hostname=` because that is the actual
    flag opencode's serve subcommand accepts (v0.16.0 briefly broke this
    by emitting `--host=`, fixed in v0.16.2). The override sets the bind
    address independently of the connect URL, so a user can expose
    opencode serve to other hosts (e.g. 0.0.0.0) while hermes itself
    still connects via loopback. The connect path never uses the bind
    host because 0.0.0.0 is bind-only.
    """

    def setup_method(self):
        self._config_mod = sys.modules["_oco_test_pkg.config"]
        self._transport_mod = sys.modules["_oco_test_pkg.transport"]

    def test_config_default_host_is_none(self):
        cfg = self._config_mod.Config.from_plugin_entry({})
        assert cfg.host is None

    def test_config_reads_yaml_host(self):
        cfg = self._config_mod.Config.from_plugin_entry({
            "opencode_server": {"url": "http://127.0.0.1:4096", "host": "0.0.0.0"},
        })
        assert cfg.host == "0.0.0.0"

    def test_config_reads_env_var_host(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_HOST", "192.168.1.10")
        cfg = self._config_mod.Config.from_plugin_entry({})
        assert cfg.host == "192.168.1.10"

    def test_config_yaml_overrides_env_var(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_HOST", "from-env")
        cfg = self._config_mod.Config.from_plugin_entry({
            "opencode_server": {"host": "from-yaml"},
        })
        assert cfg.host == "from-yaml"

    def test_client_default_host_falls_back_to_url_host(self):
        c = self._transport_mod.OpencodeClient("http://127.0.0.1:4096")
        assert c._connect_host == "127.0.0.1"
        assert c._bind_host == "127.0.0.1"

    def test_client_host_override_does_not_change_connect_host(self):
        c = self._transport_mod.OpencodeClient(
            "http://127.0.0.1:4096", None, host="0.0.0.0",
        )
        assert c._connect_host == "127.0.0.1", "connect host stays parsed from URL"
        assert c._bind_host == "0.0.0.0", "bind host honors override"

    def test_client_host_empty_falls_back_to_url_host(self):
        c = self._transport_mod.OpencodeClient(
            "http://10.0.0.5:9000", None, host=None,
        )
        assert c._connect_host == "10.0.0.5"
        assert c._bind_host == "10.0.0.5"


class TestParsePrOpenedAcceptsVariants:
    """v0.14.6: parse_pr_opened tries strict `PR_OPENED:` first, then a
    permissive variant regex (PR opened: / Opened PR: / PR_URL etc),
    then falls back to a bare github.com/.../pull/<N> match. Widening
    catches format drift in the executor's response.
    """

    def test_strict_pr_opened_prefix(self):
        url, n = reviewer_mod.parse_pr_opened(
            "Done.\nPR_OPENED: https://github.com/o/r/pull/42\n"
        )
        assert url == "https://github.com/o/r/pull/42"
        assert n == 42

    def test_strict_is_case_insensitive(self):
        url, n = reviewer_mod.parse_pr_opened(
            "pr_opened: https://github.com/o/r/pull/3"
        )
        assert n == 3

    def test_variant_pr_opened_with_space(self):
        url, n = reviewer_mod.parse_pr_opened(
            "PR opened at https://github.com/o/r/pull/7"
        )
        assert n == 7

    def test_variant_opened_pr(self):
        url, n = reviewer_mod.parse_pr_opened(
            "Opened PR: https://github.com/o/r/pull/11"
        )
        assert n == 11

    def test_variant_pr_url(self):
        url, n = reviewer_mod.parse_pr_opened(
            "PR url: https://github.com/o/r/pull/55"
        )
        assert n == 55

    def test_fallback_bare_github_url(self):
        url, n = reviewer_mod.parse_pr_opened(
            "I opened https://github.com/o/r/pull/99 for you."
        )
        assert url == "https://github.com/o/r/pull/99"
        assert n == 99

    def test_no_match_returns_none(self):
        assert reviewer_mod.parse_pr_opened("nothing to see here") is None
        assert reviewer_mod.parse_pr_opened("") is None
        assert reviewer_mod.parse_pr_opened("just a comment without url") is None


class TestExecutorOpenPrPromptHardening:
    """v0.14.6: the executor-driven PR-open prompt was strengthened with
    a concrete sentinel example, explicit `--fill` ban, and clearer
    instruction that the literal `PR_OPENED:` prefix is required.
    """

    def test_prompt_forbids_fill(self):
        p = reviewer_mod.executor_open_pr_prompt("feat/x", "main")
        assert "--fill" in p
        assert "Do NOT use `--fill`" in p

    def test_prompt_contains_concrete_example(self):
        p = reviewer_mod.executor_open_pr_prompt("feat/x", "main")
        assert "PR_OPENED: https://github.com/" in p
        assert "octocat/hello-world/pull/42" in p

    def test_prompt_emphasizes_required_prefix(self):
        p = reviewer_mod.executor_open_pr_prompt("feat/x", "main")
        assert "REQUIRED" in p
        assert "literal `PR_OPENED:` prefix" in p

    def test_prompt_mentions_amend_option(self):
        p = reviewer_mod.executor_open_pr_prompt("feat/x", "main")
        assert "amend" in p.lower()
        assert "chore: <slug>" in p


class TestMessageErrorExtraction:
    """v0.14.6: _message_error extracts opencode's structured `error`
    field from a messages-API item. Opencode places aborts at
    `message.error = { name, message }` (e.g. MessageAbortedError),
    NOT in any text part, so existing text-part readers miss it.
    """

    def test_no_error_returns_none(self):
        assert event_loop_mod._message_error({}) is None
        assert event_loop_mod._message_error({"message": {"role": "assistant"}}) is None

    def test_message_aborted_error(self):
        item = {
            "message": {
                "role": "assistant",
                "id": "msg_1",
                "error": {"name": "MessageAbortedError", "message": "Interrupted"},
            },
            "parts": [],
        }
        result = event_loop_mod._message_error(item)
        assert result == ("MessageAbortedError", "Interrupted")

    def test_error_without_message_string_ok(self):
        item = {"message": {"role": "assistant", "error": {"name": "ProviderError"}}}
        result = event_loop_mod._message_error(item)
        assert result == ("ProviderError", "")

    def test_error_at_item_level_also_works(self):
        item = {"error": {"name": "FooError", "message": "bar"}, "message": {"role": "assistant"}}
        result = event_loop_mod._message_error(item)
        assert result == ("FooError", "bar")

    def test_error_without_name_treated_as_none(self):
        item = {"message": {"error": {"message": "no name field"}}}
        assert event_loop_mod._message_error(item) is None


class TestRecordTickFailureEscalation:
    """v0.14.6: _record_tick_failure now notifies the user via the
    `tick_error` event on the FIRST failure of a streak (not on every
    consecutive failure to avoid spam), and escalates the agent to
    FAILED phase after TICK_FAILURE_ESCALATION_THRESHOLD (3) consecutive
    failures.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        self._saved_maybe_notify = event_loop_mod._maybe_notify_phase
        self._saved_cancel = event_loop_mod._cancel_agent_tasks
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, agent.agent_id, body))
        event_loop_mod._maybe_notify_phase = lambda agent, kind, body="": self._notified.append(("phase:" + kind, agent.agent_id, body))
        event_loop_mod._cancel_agent_tasks = lambda agent_id: None

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify
        event_loop_mod._maybe_notify_phase = self._saved_maybe_notify
        event_loop_mod._cancel_agent_tasks = self._saved_cancel

    def _setup_runtime(self, tmp_path, agent_phase: str = "EXECUTING"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p", phase=agent_phase,
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return rt, agents

    def test_first_failure_fires_tick_error_event(self, tmp_path: Path):
        rt, agents = self._setup_runtime(tmp_path)
        agent = agents.get("bck/test")
        event_loop_mod._record_tick_failure(agent, RuntimeError("boom"))
        kinds = [k for (k, _aid, _b) in self._notified]
        assert kinds == ["tick_error"]
        assert agents.get("bck/test").consecutive_tick_failures == 1
        assert agents.get("bck/test").last_tick_error == "RuntimeError: boom"

    def test_second_failure_does_not_re_notify(self, tmp_path: Path):
        rt, agents = self._setup_runtime(tmp_path)
        agent = agents.get("bck/test")
        event_loop_mod._record_tick_failure(agent, RuntimeError("first"))
        self._notified.clear()
        agent_after_first = agents.get("bck/test")
        event_loop_mod._record_tick_failure(agent_after_first, RuntimeError("second"))
        kinds = [k for (k, _aid, _b) in self._notified]
        assert kinds == [], (
            "second consecutive failure must NOT re-notify; only escalation at "
            "the threshold fires another event"
        )
        assert agents.get("bck/test").consecutive_tick_failures == 2

    def test_third_failure_escalates_to_failed(self, tmp_path: Path):
        rt, agents = self._setup_runtime(tmp_path)
        agent = agents.get("bck/test")
        event_loop_mod._record_tick_failure(agent, RuntimeError("e1"))
        agent2 = agents.get("bck/test")
        event_loop_mod._record_tick_failure(agent2, RuntimeError("e2"))
        agent3 = agents.get("bck/test")
        self._notified.clear()
        event_loop_mod._record_tick_failure(agent3, RuntimeError("e3"))
        kinds = [k for (k, _aid, _b) in self._notified]
        assert "phase:failed" in kinds, f"expected escalation, got {kinds}"
        final = agents.get("bck/test")
        assert final.phase == "FAILED"
        assert "stalled after 3" in (final.last_error or "")

    def test_terminal_agent_not_re_escalated(self, tmp_path: Path):
        rt, agents = self._setup_runtime(tmp_path, agent_phase="DONE")
        agent = agents.get("bck/test")
        agents.update("bck/test", consecutive_tick_failures=2)
        agent2 = agents.get("bck/test")
        self._notified.clear()
        event_loop_mod._record_tick_failure(agent2, RuntimeError("late tick"))
        kinds = [k for (k, _aid, _b) in self._notified]
        assert "phase:failed" not in kinds, (
            "agent already in terminal phase must not be re-escalated"
        )
        assert agents.get("bck/test").phase == "DONE"


class TestCheckExecutorAbort:
    """v0.14.6: _check_executor_abort detects opencode's structured
    abort errors on the latest assistant message, surfaces them via
    the `aborted` event, auto-sends a "continue" follow-up, and
    escalates to FAILED after ABORT_ESCALATION_THRESHOLD (3) distinct
    aborts. Same-message.id aborts are idempotent.
    """

    def setup_method(self):
        self._notified: list = []
        self._continue_sent: list = []
        self._messages_box: list[list[dict]] = [[]]
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        self._saved_maybe_notify = event_loop_mod._maybe_notify_phase
        self._saved_cancel = event_loop_mod._cancel_agent_tasks
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))
        event_loop_mod._maybe_notify_phase = lambda agent, kind, body="": self._notified.append(("phase:" + kind, body))
        event_loop_mod._cancel_agent_tasks = lambda agent_id: None

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify
        event_loop_mod._maybe_notify_phase = self._saved_maybe_notify
        event_loop_mod._cancel_agent_tasks = self._saved_cancel

    def _setup(self, tmp_path: Path, messages_items: list[dict]):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p", phase="EXECUTING",
        )
        agents.add(agent)
        self._messages_box[0] = messages_items
        captured = self._continue_sent
        box = self._messages_box

        class FakeClient:
            async def get_messages(self_inner, session_id, directory, cursor=None):
                return {"items": box[0]}

            async def send_message_async(self_inner, session_id, directory, text, timeout=30.0):
                captured.append({"session_id": session_id, "text": text})
                return {"queued": True}

        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def _run(self, agent):
        import asyncio
        return asyncio.run(event_loop_mod._check_executor_abort(agent))

    def test_no_error_returns_false_and_clears_streak(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {"role": "assistant", "id": "m1"},
            "parts": [{"type": "text", "text": "all good"}],
        }])
        agents.update("bck/test", consecutive_aborts=2, last_abort_msg_id="old")
        agent = agents.get("bck/test")
        assert self._run(agent) is False
        refreshed = agents.get("bck/test")
        assert refreshed.consecutive_aborts == 0
        assert refreshed.last_abort_msg_id is None

    def test_first_abort_notifies_and_sends_continue(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {
                "role": "assistant", "id": "msg_a",
                "error": {"name": "MessageAbortedError", "message": "Interrupted"},
            },
            "parts": [],
        }])
        agent = agents.get("bck/test")
        assert self._run(agent) is True
        kinds = [k for (k, _b) in self._notified]
        assert kinds == ["aborted"]
        assert len(self._continue_sent) == 1
        assert self._continue_sent[0]["text"] == event_loop_mod.ABORT_AUTO_CONTINUE_MESSAGE
        refreshed = agents.get("bck/test")
        assert refreshed.consecutive_aborts == 1
        assert refreshed.last_abort_msg_id == "msg_a"

    def test_same_message_id_is_idempotent(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {
                "role": "assistant", "id": "msg_a",
                "error": {"name": "MessageAbortedError", "message": "Interrupted"},
            },
            "parts": [],
        }])
        agent = agents.get("bck/test")
        self._run(agent)
        self._notified.clear()
        self._continue_sent.clear()
        agent2 = agents.get("bck/test")
        assert self._run(agent2) is True
        assert self._notified == [], "same msg_id must not re-notify"
        assert self._continue_sent == [], "same msg_id must not re-send 'continue'"
        assert agents.get("bck/test").consecutive_aborts == 1

    def test_third_distinct_abort_escalates_to_failed(self, tmp_path: Path):
        agents = self._setup(tmp_path, [])
        events_log: list = []
        for i in range(1, 4):
            self._messages_box[0] = [{
                "message": {
                    "role": "assistant", "id": f"msg_{i}",
                    "error": {"name": "MessageAbortedError", "message": f"abort {i}"},
                },
                "parts": [],
            }]
            self._notified.clear()
            agent = agents.get("bck/test")
            self._run(agent)
            events_log.append([k for (k, _b) in self._notified])
        assert events_log[0] == ["aborted"]
        assert events_log[1] == ["aborted"]
        assert "phase:failed" in events_log[2], f"expected failed escalation on 3rd, got {events_log[2]}"
        final = agents.get("bck/test")
        assert final.phase == "FAILED"
        assert "aborted 3" in (final.last_error or "")


class TestParseModelId:
    """v0.15.0: parse_model_id converts "provider/model[/variant]" config
    strings into opencode's POST /session model struct {id, providerID, variant?}.
    """

    def test_simple_two_parts(self):
        result = reviewer_mod.parse_model_id("openai/gpt-5.5")
        assert result == {"id": "gpt-5.5", "providerID": "openai"}

    def test_three_parts_treats_third_as_variant(self):
        result = reviewer_mod.parse_model_id("anthropic/claude-opus-4-7/max")
        assert result == {"id": "claude-opus-4-7", "providerID": "anthropic", "variant": "max"}

    def test_opencode_provider_id(self):
        result = reviewer_mod.parse_model_id("opencode/deepseek-v4-flash-free")
        assert result == {"id": "deepseek-v4-flash-free", "providerID": "opencode"}

    def test_missing_slash_returns_none(self):
        assert reviewer_mod.parse_model_id("gpt-5.5") is None

    def test_empty_returns_none(self):
        assert reviewer_mod.parse_model_id("") is None
        assert reviewer_mod.parse_model_id("   ") is None

    def test_only_provider_returns_none(self):
        assert reviewer_mod.parse_model_id("openai/") is None
        assert reviewer_mod.parse_model_id("/gpt-5.5") is None

    def test_strips_whitespace_segments(self):
        result = reviewer_mod.parse_model_id("  openai  / gpt-5.5 ")
        assert result == {"id": "gpt-5.5", "providerID": "openai"}


class TestPrFallbackModelsConfig:
    """v0.15.0: Config.pr_fallback_models supports YAML list, env var
    (comma-separated), or a sane default. YAML beats env.
    """

    def test_default_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PR_FALLBACK_MODELS", raising=False)
        cfg = config_mod.Config.from_plugin_entry({})
        assert cfg.pr_fallback_models == [
            "openai/gpt-5.5",
            "opencode/deepseek-v4-flash-free",
        ]

    def test_yaml_list_wins(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PR_FALLBACK_MODELS", "x/y,z/w")
        cfg = config_mod.Config.from_plugin_entry({
            "opencode_server": {
                "pr_fallback_models": ["a/b", "c/d/v"],
            },
        })
        assert cfg.pr_fallback_models == ["a/b", "c/d/v"]

    def test_env_var_comma_separated(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PR_FALLBACK_MODELS", "openai/gpt-5,deepseek/r2 ,foo/bar")
        cfg = config_mod.Config.from_plugin_entry({})
        assert cfg.pr_fallback_models == ["openai/gpt-5", "deepseek/r2", "foo/bar"]

    def test_empty_yaml_list_falls_back_to_env_then_default(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PR_FALLBACK_MODELS", raising=False)
        cfg = config_mod.Config.from_plugin_entry({
            "opencode_server": {"pr_fallback_models": []},
        })
        assert cfg.pr_fallback_models == [
            "openai/gpt-5.5", "opencode/deepseek-v4-flash-free",
        ]


class TestMessageIsRateLimited:
    """v0.15.0: _message_is_rate_limited extracts retry-after seconds
    from opencode's structured APIError on assistant messages.
    """

    def test_none_when_no_error(self):
        assert event_loop_mod._message_is_rate_limited({"message": {"role": "assistant"}}) is None

    def test_none_when_error_not_apierror(self):
        item = {"message": {"role": "assistant", "error": {"name": "MessageAbortedError"}}}
        assert event_loop_mod._message_is_rate_limited(item) is None

    def test_status_code_429_matches(self):
        item = {
            "message": {
                "role": "assistant",
                "error": {"name": "APIError", "statusCode": 429, "message": "rate limited", "isRetryable": True},
            }
        }
        result = event_loop_mod._message_is_rate_limited(item)
        assert result is not None
        assert result == 0.0

    def test_status_code_other_with_text_pattern_matches(self):
        item = {
            "message": {
                "role": "assistant",
                "error": {"name": "APIError", "statusCode": 500, "message": "quota exceeded for this account"},
            }
        }
        result = event_loop_mod._message_is_rate_limited(item)
        assert result is not None

    def test_retry_after_header_parsed(self):
        item = {
            "message": {
                "role": "assistant",
                "error": {
                    "name": "APIError", "statusCode": 429,
                    "message": "rate limited",
                    "responseHeaders": {"retry-after": "120"},
                },
            }
        }
        assert event_loop_mod._message_is_rate_limited(item) == 120.0

    def test_retry_after_ms_metadata_parsed(self):
        item = {
            "message": {
                "role": "assistant",
                "error": {
                    "name": "APIError", "statusCode": 429,
                    "message": "...",
                    "metadata": {"retryAfterMs": 60000},
                },
            }
        }
        assert event_loop_mod._message_is_rate_limited(item) == 60.0

    def test_no_429_no_text_pattern_returns_none(self):
        item = {
            "message": {
                "role": "assistant",
                "error": {"name": "APIError", "statusCode": 401, "message": "unauthorized"},
            }
        }
        assert event_loop_mod._message_is_rate_limited(item) is None


class TestCheckExecutorRateLimited:
    """v0.15.0: _check_executor_rate_limited transitions the agent to
    RATE_LIMITED on a provider 429 and saves the prior phase so the
    wait-and-resume path can restore it. Idempotent on already-RATE_LIMITED.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def _setup(self, tmp_path, messages_items, phase="EXECUTING"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p", phase=phase,
        )
        agents.add(agent)
        box = [messages_items]
        class FakeClient:
            async def get_messages(self_inner, session_id, directory, cursor=None):
                return {"items": box[0]}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def _run(self, agent):
        import asyncio
        return asyncio.run(event_loop_mod._check_executor_rate_limited(agent))

    def test_no_error_returns_false(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {"role": "assistant", "id": "m1"},
            "parts": [{"type": "text", "text": "all good"}],
        }])
        agent = agents.get("bck/test")
        assert self._run(agent) is False
        assert agents.get("bck/test").phase == "EXECUTING"

    def test_rate_limit_transitions_to_RATE_LIMITED(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {
                "role": "assistant", "id": "m1",
                "error": {
                    "name": "APIError", "statusCode": 429, "message": "rate limited",
                    "responseHeaders": {"retry-after": "90"},
                },
            },
        }])
        agent = agents.get("bck/test")
        assert self._run(agent) is True
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "RATE_LIMITED"
        assert refreshed.phase_before_rate_limit == "EXECUTING"
        assert refreshed.rate_limited_at is not None
        assert refreshed.rate_limit_retry_after_at is not None
        kinds = [k for (k, _b) in self._notified]
        assert "rate_limited" in kinds

    def test_already_RATE_LIMITED_is_noop(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {
                "role": "assistant", "id": "m1",
                "error": {"name": "APIError", "statusCode": 429, "message": "rate limited"},
            },
        }], phase="RATE_LIMITED")
        agent = agents.get("bck/test")
        assert self._run(agent) is False
        assert self._notified == []

    def test_min_wait_floor_applied_when_retry_after_missing(self, tmp_path: Path):
        agents = self._setup(tmp_path, [{
            "message": {
                "role": "assistant", "id": "m1",
                "error": {"name": "APIError", "statusCode": 429, "message": "rate limited"},
            },
        }])
        agent = agents.get("bck/test")
        self._run(agent)
        refreshed = agents.get("bck/test")
        wait = refreshed.rate_limit_retry_after_at - refreshed.rate_limited_at
        assert wait >= event_loop_mod.RATE_LIMIT_MIN_WAIT_SEC - 0.5


class TestPhaseRateLimited:
    """v0.15.0: _phase_rate_limited waits for retry_after_at then restores
    the saved phase_before_rate_limit, fires `rate_limit_cleared`.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def _setup(self, tmp_path, retry_after_at, prior_phase="EXECUTING"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p", phase="RATE_LIMITED",
            rate_limited_at=time.time(),
            rate_limit_retry_after_at=retry_after_at,
            phase_before_rate_limit=prior_phase,
            last_error="rate-limited",
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def test_restores_phase_after_retry_after_elapsed(self, tmp_path: Path):
        agents = self._setup(tmp_path, retry_after_at=time.time() - 1.0, prior_phase="COMMITTING")
        agent = agents.get("bck/test")
        import asyncio
        asyncio.run(event_loop_mod._phase_rate_limited(agent))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "COMMITTING"
        assert refreshed.rate_limited_at is None
        assert refreshed.rate_limit_retry_after_at is None
        assert refreshed.phase_before_rate_limit is None
        assert refreshed.last_error is None
        assert any(k == "rate_limit_cleared" for (k, _b) in self._notified)

    def test_defaults_to_EXECUTING_when_no_prior_saved(self, tmp_path: Path):
        agents = self._setup(tmp_path, retry_after_at=time.time() - 1.0, prior_phase=None)
        agent = agents.get("bck/test")
        import asyncio
        asyncio.run(event_loop_mod._phase_rate_limited(agent))
        assert agents.get("bck/test").phase == "EXECUTING"


class TestPhaseQueued:
    """v0.15.0: _phase_queued waits while any RATE_LIMITED agent exists,
    then sends the wrapped initial prompt and transitions to EXECUTING.
    """

    def setup_method(self):
        self._notified: list = []
        self._sent: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        self._saved_maybe_notify = event_loop_mod._maybe_notify_phase
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))
        event_loop_mod._maybe_notify_phase = lambda agent, kind, body="": self._notified.append(("phase:" + kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify
        event_loop_mod._maybe_notify_phase = self._saved_maybe_notify

    def _setup(self, tmp_path, other_agents: list):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        queued = state_mod.Agent(
            agent_id="bck/queued", project_label="bck",
            worktree_path=str(tmp_path), session_id="qs",
            branch="bck/queued", initial_prompt="please do the thing",
            phase="QUEUED", queued_blocked_by=[],
        )
        agents.add(queued)
        for a in other_agents:
            agents.add(a)
        captured = self._sent
        class FakeClient:
            async def send_message_async(self_inner, session_id, directory, text, timeout=30.0):
                captured.append({"session_id": session_id, "text": text})
                return {"queued": True}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def test_no_blockers_drains_and_transitions(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, other_agents=[])
        agent = agents.get("bck/queued")
        asyncio.run(event_loop_mod._phase_queued(agent))
        refreshed = agents.get("bck/queued")
        assert refreshed.phase == "EXECUTING"
        assert refreshed.queued_blocked_by == []
        assert len(self._sent) == 1
        assert "please do the thing" in self._sent[0]["text"]
        assert any(k == "queue_drained" for (k, _b) in self._notified)

    def test_other_RATE_LIMITED_blocks_drain(self, tmp_path: Path):
        import asyncio
        blocker = state_mod.Agent(
            agent_id="bck/blocker", project_label="bck",
            worktree_path=str(tmp_path / "x"), session_id="bs",
            branch="bck/blocker", initial_prompt="...", phase="RATE_LIMITED",
        )
        agents = self._setup(tmp_path, other_agents=[blocker])
        agent = agents.get("bck/queued")
        async def _run_with_short_sleep():
            event_loop_mod.QUEUE_POLL_SEC = 0.0
            await event_loop_mod._phase_queued(agent)
        asyncio.run(_run_with_short_sleep())
        refreshed = agents.get("bck/queued")
        assert refreshed.phase == "QUEUED"
        assert refreshed.queued_blocked_by == ["bck/blocker"]
        assert self._sent == []

    def test_blocked_by_list_updated_when_drifted(self, tmp_path: Path):
        import asyncio
        blocker = state_mod.Agent(
            agent_id="bck/blocker", project_label="bck",
            worktree_path=str(tmp_path / "x"), session_id="bs",
            branch="bck/blocker", initial_prompt="...", phase="RATE_LIMITED",
        )
        agents = self._setup(tmp_path, other_agents=[blocker])
        agents.update("bck/queued", queued_blocked_by=["stale-id"])
        agent = agents.get("bck/queued")
        async def _run():
            event_loop_mod.QUEUE_POLL_SEC = 0.0
            await event_loop_mod._phase_queued(agent)
        asyncio.run(_run())
        assert agents.get("bck/queued").queued_blocked_by == ["bck/blocker"]


class TestOneshotOpenPr:
    """v0.15.0: oneshot_open_pr iterates pr_fallback_models, creating a
    fresh session per attempt. First successful sentinel emit wins;
    returns None when all models exhausted.
    """

    def setup_method(self):
        self._created_sessions: list = []
        self._sends: list = []
        self._reviewer_mod = reviewer_mod
        self._state_mod = state_mod
        self._OpencodeClient = sys.modules["_oco_test_pkg.transport"].OpencodeClient

    def _agent(self, tmp_path: Path):
        wt = tmp_path / "wt"
        wt.mkdir(parents=True, exist_ok=True)
        return self._state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(wt), session_id="executor-s",
            branch="bck/test", initial_prompt="please do the thing",
            phase="COMMITTING",
        )

    def _build_client(self, responses: list):
        created = self._created_sessions
        sends = self._sends
        # responses[i] is the assistant text for the i-th model attempt;
        # use sentinel "PR_OPENED: https://github.com/o/r/pull/<N>" to succeed
        idx = [0]
        class FakeClient:
            async def create_session(self_inner, directory, agent="build", model=None):
                created.append({"agent": agent, "model": model, "directory": str(directory)})
                return {"id": f"oneshot-{len(created)}"}
            async def send_message(self_inner, session_id, directory, text, timeout=600.0):
                resp_text = responses[idx[0]] if idx[0] < len(responses) else ""
                idx[0] += 1
                sends.append({"session_id": session_id, "text": text})
                return {
                    "info": {},
                    "parts": [{"type": "text", "text": resp_text}],
                }
        # Patch the OpencodeClient.extract_assistant_text helper to handle our shape
        return FakeClient()

    def test_first_model_succeeds(self, tmp_path: Path, monkeypatch):
        import asyncio
        agent = self._agent(tmp_path)
        monkeypatch.setattr(
            self._OpencodeClient, "extract_assistant_text",
            staticmethod(lambda resp: resp["parts"][0]["text"]),
        )
        client = self._build_client([
            "Done. PR_OPENED: https://github.com/o/r/pull/7",
        ])
        pr_mod_test = sys.modules["_oco_test_pkg.pr"]
        monkeypatch.setattr(pr_mod_test, "pr_state", lambda *a, **kw: (_ for _ in ()).throw(pr_mod_test.PrError("no gh")))
        info, attempts = asyncio.run(self._reviewer_mod.oneshot_open_pr(
            client, agent, "main", ["openai/gpt-5.5", "opencode/deepseek-v4-flash-free"],
            timeout_sec=10.0,
        ))
        assert info is not None
        assert info.number == 7
        assert "openai" in attempts[0] and "ok PR #7" in attempts[0]
        assert len(self._created_sessions) == 1
        assert self._created_sessions[0]["model"] == {"id": "gpt-5.5", "providerID": "openai"}

    def test_first_fails_no_sentinel_second_succeeds(self, tmp_path: Path, monkeypatch):
        import asyncio
        agent = self._agent(tmp_path)
        monkeypatch.setattr(
            self._OpencodeClient, "extract_assistant_text",
            staticmethod(lambda resp: resp["parts"][0]["text"]),
        )
        client = self._build_client([
            "I tried but couldn't open the PR.",
            "Done. PR_OPENED: https://github.com/o/r/pull/42",
        ])
        pr_mod_test = sys.modules["_oco_test_pkg.pr"]
        monkeypatch.setattr(pr_mod_test, "pr_state", lambda *a, **kw: (_ for _ in ()).throw(pr_mod_test.PrError("no gh")))
        info, attempts = asyncio.run(self._reviewer_mod.oneshot_open_pr(
            client, agent, "main", ["openai/gpt-5.5", "opencode/deepseek-v4-flash-free"],
        ))
        assert info is not None
        assert info.number == 42
        assert len(self._created_sessions) == 2
        assert "no PR_OPENED sentinel" in attempts[0]
        assert "ok PR #42" in attempts[1]

    def test_all_models_exhausted_returns_none(self, tmp_path: Path, monkeypatch):
        import asyncio
        agent = self._agent(tmp_path)
        monkeypatch.setattr(
            self._OpencodeClient, "extract_assistant_text",
            staticmethod(lambda resp: resp["parts"][0]["text"]),
        )
        client = self._build_client(["nope", "still nope"])
        info, attempts = asyncio.run(self._reviewer_mod.oneshot_open_pr(
            client, agent, "main", ["a/b", "c/d"],
        ))
        assert info is None
        assert len(attempts) == 2
        assert all("no PR_OPENED sentinel" in a for a in attempts)

    def test_invalid_model_spec_is_skipped(self, tmp_path: Path, monkeypatch):
        import asyncio
        agent = self._agent(tmp_path)
        monkeypatch.setattr(
            self._OpencodeClient, "extract_assistant_text",
            staticmethod(lambda resp: resp["parts"][0]["text"]),
        )
        client = self._build_client([
            "Done. PR_OPENED: https://github.com/o/r/pull/99",
        ])
        pr_mod_test = sys.modules["_oco_test_pkg.pr"]
        monkeypatch.setattr(pr_mod_test, "pr_state", lambda *a, **kw: (_ for _ in ()).throw(pr_mod_test.PrError("no gh")))
        info, attempts = asyncio.run(self._reviewer_mod.oneshot_open_pr(
            client, agent, "main", ["no-slash", "openai/gpt-5.5"],
        ))
        assert info is not None
        assert info.number == 99
        assert "invalid spec" in attempts[0]
        assert "openai" in attempts[1]
        assert len(self._created_sessions) == 1


class TestCheckSessionRateLimited:
    """v0.15.1: _check_session_rate_limited is the generic detector that
    can run against ANY of the agent's sessions (executor OR reviewer).
    Closes the v0.15.0 known gap: previously _phase_reviewing did not
    detect rate-limits on the reviewer session.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def _setup(self, tmp_path, messages_by_session: dict, phase="REVIEWING"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        sister_path = tmp_path / "sister"
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="exec-s",
            reviewer_session_id="rev-s", reviewer_worktree_path=str(sister_path),
            branch="bck/test", initial_prompt="p", phase=phase,
        )
        agents.add(agent)
        store = messages_by_session
        class FakeClient:
            async def get_messages(self_inner, session_id, directory, cursor=None):
                return {"items": store.get(session_id, [])}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents, sister_path

    def test_reviewer_session_rate_limit_transitions_agent(self, tmp_path: Path):
        import asyncio
        agents, sister = self._setup(tmp_path, {
            "exec-s": [{
                "message": {"role": "assistant", "id": "e1"},
                "parts": [{"type": "text", "text": "executor was fine"}],
            }],
            "rev-s": [{
                "message": {
                    "role": "assistant", "id": "r1",
                    "error": {
                        "name": "APIError", "statusCode": 429,
                        "message": "rate limited",
                        "responseHeaders": {"retry-after": "45"},
                    },
                },
            }],
        }, phase="REVIEWING")
        agent = agents.get("bck/test")
        result = asyncio.run(event_loop_mod._check_session_rate_limited(
            agent, agent.reviewer_session_id, sister, session_label="reviewer",
        ))
        assert result is True
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "RATE_LIMITED"
        assert refreshed.phase_before_rate_limit == "REVIEWING"
        assert refreshed.rate_limit_retry_after_at is not None
        kinds = [k for (k, _b) in self._notified]
        assert "rate_limited" in kinds
        bodies = [b for (_k, b) in self._notified]
        assert any("reviewer session" in b for b in bodies), (
            "v0.15.1 body must mention the session_label so the user "
            "knows which session was rate-limited"
        )
        last_err = refreshed.last_error or ""
        assert "reviewer" in last_err

    def test_executor_session_label_in_body(self, tmp_path: Path):
        import asyncio
        agents, sister = self._setup(tmp_path, {
            "exec-s": [{
                "message": {
                    "role": "assistant", "id": "e1",
                    "error": {"name": "APIError", "statusCode": 429, "message": "rl"},
                },
            }],
        }, phase="EXECUTING")
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._check_session_rate_limited(
            agent, agent.session_id, Path(agent.worktree_path),
            session_label="executor",
        ))
        bodies = [b for (_k, b) in self._notified]
        assert any("executor session" in b for b in bodies)
        last_err = agents.get("bck/test").last_error or ""
        assert "executor" in last_err

    def test_no_rate_limit_returns_false(self, tmp_path: Path):
        import asyncio
        agents, sister = self._setup(tmp_path, {
            "rev-s": [{
                "message": {"role": "assistant", "id": "r1"},
                "parts": [{"type": "text", "text": "REVIEW: LGTM"}],
            }],
        }, phase="REVIEWING")
        agent = agents.get("bck/test")
        result = asyncio.run(event_loop_mod._check_session_rate_limited(
            agent, agent.reviewer_session_id, sister, session_label="reviewer",
        ))
        assert result is False
        assert agents.get("bck/test").phase == "REVIEWING"
        assert self._notified == []

    def test_executor_wrapper_still_works(self, tmp_path: Path):
        """v0.15.0 _check_executor_rate_limited is a back-compat shim;
        ensure it still routes through the generalized helper."""
        import asyncio
        agents, sister = self._setup(tmp_path, {
            "exec-s": [{
                "message": {
                    "role": "assistant", "id": "e1",
                    "error": {
                        "name": "APIError", "statusCode": 429,
                        "message": "rl", "responseHeaders": {"retry-after": "30"},
                    },
                },
            }],
        }, phase="EXECUTING")
        agent = agents.get("bck/test")
        result = asyncio.run(event_loop_mod._check_executor_rate_limited(agent))
        assert result is True
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "RATE_LIMITED"
        assert refreshed.phase_before_rate_limit == "EXECUTING"
        assert "executor" in (refreshed.last_error or "")

    def test_already_RATE_LIMITED_noop_on_either_session(self, tmp_path: Path):
        import asyncio
        agents, sister = self._setup(tmp_path, {
            "rev-s": [{
                "message": {
                    "role": "assistant", "id": "r1",
                    "error": {"name": "APIError", "statusCode": 429, "message": "rl"},
                },
            }],
        }, phase="RATE_LIMITED")
        agent = agents.get("bck/test")
        result = asyncio.run(event_loop_mod._check_session_rate_limited(
            agent, agent.reviewer_session_id, sister, session_label="reviewer",
        ))
        assert result is False
        assert self._notified == []


class TestAwaitingHumanPhase:
    """v0.16.0: awaiting_human is now a proper phase. Entry sets
    phase_before_awaiting; exit restores it via _phase_awaiting_human.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body, agent.phase))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def _setup(self, tmp_path, agent_phase="EXECUTING"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p", phase=agent_phase,
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def _attach_fake_client(self, agents, latest_message_id: str | None = "m-entry"):
        cfg = plugin_mod._runtime.config
        class FakeClient:
            async def get_messages(self_inner, session_id, directory, cursor=None):
                if latest_message_id is None:
                    return {"items": []}
                return {"items": [{
                    "message": {"role": "assistant", "id": latest_message_id},
                    "parts": [{"type": "text", "text": "entry text"}],
                }]}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt

    def test_enter_from_executing_saves_prior_phase(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, "EXECUTING")
        self._attach_fake_client(agents, "m-entry")
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._enter_awaiting_human(agent, "test body", had_pending_qp=True))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "AWAITING_HUMAN"
        assert refreshed.phase_before_awaiting == "EXECUTING"
        assert refreshed.awaiting_human_since is not None
        assert refreshed.last_awaiting_notify_at is not None
        assert refreshed.awaiting_entry_message_id == "m-entry", (
            "entry message id must be captured on first transition"
        )
        assert refreshed.awaiting_entry_had_pending_qp is True
        kinds = [k for (k, _b, _p) in self._notified]
        assert kinds == ["awaiting_human"]

    def test_enter_from_executor_addressing_saves_prior_phase(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, "EXECUTOR_ADDRESSING")
        self._attach_fake_client(agents, "m-entry")
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._enter_awaiting_human(agent, "...", had_pending_qp=False))
        refreshed = agents.get("bck/test")
        assert refreshed.phase_before_awaiting == "EXECUTOR_ADDRESSING"
        assert refreshed.awaiting_entry_had_pending_qp is False

    def test_re_enter_does_not_overwrite_prior_phase(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, "EXECUTING")
        self._attach_fake_client(agents, "m-entry")
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._enter_awaiting_human(agent, "first", had_pending_qp=True))
        first_since = agents.get("bck/test").awaiting_human_since
        agent_after_first = agents.get("bck/test")
        asyncio.run(event_loop_mod._enter_awaiting_human(agent_after_first, "reminder", had_pending_qp=False))
        refreshed = agents.get("bck/test")
        assert refreshed.phase_before_awaiting == "EXECUTING", (
            "re-entry from AWAITING_HUMAN must not overwrite phase_before"
        )
        assert refreshed.awaiting_human_since == first_since, (
            "awaiting_human_since must not reset on reminder fire"
        )
        assert refreshed.awaiting_entry_had_pending_qp is True, (
            "re-entry must preserve original had_pending_qp; first trigger wins"
        )
        kinds = [k for (k, _b, _p) in self._notified]
        assert kinds == ["awaiting_human", "awaiting_human"]


class TestPhaseAwaitingHumanHandler:
    """v0.16.0: _phase_awaiting_human polls list_questions/list_permissions
    and re-runs the classifier; restores phase_before_awaiting when both
    say not-awaiting.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        self._saved_check = sys.modules["_oco_test_pkg.awaiting_input"].check
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify
        sys.modules["_oco_test_pkg.awaiting_input"].check = self._saved_check

    def _setup(
        self,
        tmp_path,
        questions: list,
        permissions: list,
        latest_text="resumed work",
        latest_message_id: str = "m-current",
        entry_message_id: str | None = "m-entry",
        had_pending_qp: bool = True,
    ):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="exec-s",
            branch="bck/test", initial_prompt="p",
            phase="AWAITING_HUMAN",
            phase_before_awaiting="EXECUTING",
            awaiting_human_since=time.time() - 30.0,
            awaiting_entry_message_id=entry_message_id,
            awaiting_entry_had_pending_qp=had_pending_qp,
        )
        agents.add(agent)
        text_box = [latest_text]

        class FakeClient:
            async def list_questions(self_inner, directory):
                return questions
            async def list_permissions(self_inner, directory):
                return permissions
            async def get_messages(self_inner, session_id, directory, cursor=None):
                return {"items": [{
                    "message": {"role": "assistant", "id": latest_message_id},
                    "parts": [{"type": "text", "text": text_box[0]}],
                }]}

        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def test_pending_question_stays_awaiting(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, questions=[
            {"sessionID": "exec-s", "id": "q1", "questions": [{"question": "choose?"}]},
        ], permissions=[])
        agent = agents.get("bck/test")
        async def _fast():
            event_loop_mod.RATE_LIMIT_MAX_TICK_WAIT_SEC = 0
            await event_loop_mod._phase_awaiting_human(agent)
        asyncio.run(_fast())
        assert agents.get("bck/test").phase == "AWAITING_HUMAN"
        assert self._notified == []

    def test_pending_permission_stays_awaiting(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, questions=[], permissions=[
            {"sessionID": "exec-s", "id": "p1", "permission": "bash"},
        ])
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        assert agents.get("bck/test").phase == "AWAITING_HUMAN"

    def test_new_turn_classifier_still_awaiting_keeps_phase(self, tmp_path: Path):
        import asyncio
        from dataclasses import dataclass
        awaiting_mod = sys.modules["_oco_test_pkg.awaiting_input"]
        @dataclass
        class StubCheck:
            awaiting: bool = True
            source: str = "test"
            confidence: str = "high"
            reason: str = "still asking"
            last_assistant_text: str = "what should I do?"
        async def _stub_check(runtime, text):
            return StubCheck()
        awaiting_mod.check = _stub_check
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_text="what should I do?",
            latest_message_id="m-followup",
            entry_message_id="m-entry",
            had_pending_qp=False,
        )
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        assert agents.get("bck/test").phase == "AWAITING_HUMAN"

    def test_qp_resolved_path_exits_with_authoritative_body(self, tmp_path: Path):
        import asyncio
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_text="same text as entry",
            latest_message_id="m-entry",
            entry_message_id="m-entry",
            had_pending_qp=True,
        )
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "EXECUTING"
        assert refreshed.phase_before_awaiting is None
        assert refreshed.awaiting_human_since is None
        assert refreshed.awaiting_entry_message_id is None
        kinds = [k for (k, _b) in self._notified]
        assert "awaiting_human_resumed" in kinds
        body = next(b for (k, b) in self._notified if k == "awaiting_human_resumed")
        assert "Pending question/permission resolved" in body, body
        assert "Human reply received" not in body, (
            "must not claim human reply when only Q/P resolution is the signal"
        )

    def test_new_assistant_turn_path_exits_when_classifier_clears(self, tmp_path: Path):
        import asyncio
        from dataclasses import dataclass
        awaiting_mod = sys.modules["_oco_test_pkg.awaiting_input"]
        @dataclass
        class StubCheck:
            awaiting: bool = False
            source: str = "test"
            confidence: str = "high"
            reason: str = "moved on"
            last_assistant_text: str = "ok working on it"
        async def _stub_check(runtime, text):
            return StubCheck()
        awaiting_mod.check = _stub_check
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_text="ok working on it",
            latest_message_id="m-after-reply",
            entry_message_id="m-entry",
            had_pending_qp=False,
        )
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "EXECUTING"
        body = next(b for (k, b) in self._notified if k == "awaiting_human_resumed")
        assert "Executor produced new assistant turn" in body, body

    def test_classifier_flip_with_same_message_id_stays_awaiting(self, tmp_path: Path):
        """v0.16.2 regression guard. Reproduces the gateway-restart bug:
        agent in AWAITING_HUMAN via classifier-prose-only entry, Q/P
        empty, latest message id unchanged from entry, classifier
        flips and now says NOT awaiting. Must NOT exit AWAITING_HUMAN
        because no human input actually occurred.
        """
        import asyncio
        from dataclasses import dataclass
        awaiting_mod = sys.modules["_oco_test_pkg.awaiting_input"]
        @dataclass
        class StubCheck:
            awaiting: bool = False
            source: str = "test"
            confidence: str = "high"
            reason: str = "classifier non-determinism"
            last_assistant_text: str = "let me confirm scope."
        async def _stub_check(runtime, text):
            return StubCheck()
        awaiting_mod.check = _stub_check
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_text="let me confirm scope.",
            latest_message_id="m-entry",
            entry_message_id="m-entry",
            had_pending_qp=False,
        )
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "AWAITING_HUMAN", (
            "classifier flip alone must NOT exit AWAITING_HUMAN; "
            "requires authoritative forward-progress signal"
        )
        kinds = [k for (k, _b) in self._notified]
        assert "awaiting_human_resumed" not in kinds, (
            "must not fire awaiting_human_resumed without forward-progress signal"
        )

    def test_legacy_agent_with_null_entry_id_backfills_and_sleeps(self, tmp_path: Path):
        """v0.16.2 backward-compat: agents that entered AWAITING_HUMAN
        before this release have awaiting_entry_message_id=None. First
        tick after upgrade backfills the field, sleeps, and lets the
        next tick run the proper exit gate.
        """
        import asyncio
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_text="legacy",
            latest_message_id="m-legacy",
            entry_message_id=None,
            had_pending_qp=False,
        )
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "AWAITING_HUMAN"
        assert refreshed.awaiting_entry_message_id == "m-legacy", (
            "backfill must populate awaiting_entry_message_id from current latest"
        )
        kinds = [k for (k, _b) in self._notified]
        assert "awaiting_human_resumed" not in kinds

    def test_new_turn_that_also_asks_re_anchors_entry_id(self, tmp_path: Path):
        """Multi-turn questioning: executor produced a new turn after
        the human's reply, but that new turn is itself another
        question. Stay AWAITING_HUMAN, but re-anchor the entry message
        id to the new turn so the next tick's `new_turn_arrived` check
        does not spuriously re-trigger on this same new turn.
        """
        import asyncio
        from dataclasses import dataclass
        awaiting_mod = sys.modules["_oco_test_pkg.awaiting_input"]
        @dataclass
        class StubCheck:
            awaiting: bool = True
            source: str = "test"
            confidence: str = "high"
            reason: str = "still asking"
            last_assistant_text: str = "ok and one more thing?"
        async def _stub_check(runtime, text):
            return StubCheck()
        awaiting_mod.check = _stub_check
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_text="ok and one more thing?",
            latest_message_id="m-followup",
            entry_message_id="m-entry",
            had_pending_qp=False,
        )
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "AWAITING_HUMAN"
        assert refreshed.awaiting_entry_message_id == "m-followup", (
            "must re-anchor to the new turn so subsequent ticks don't "
            "re-flag this same message id as forward progress"
        )

    def test_qp_resolved_with_no_prior_phase_defaults_to_executing(self, tmp_path: Path):
        import asyncio
        agents = self._setup(
            tmp_path, questions=[], permissions=[],
            latest_message_id="m-entry",
            entry_message_id="m-entry",
            had_pending_qp=True,
        )
        agents.update("bck/test", phase_before_awaiting=None)
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._phase_awaiting_human(agent))
        assert agents.get("bck/test").phase == "EXECUTING"


class TestAwaitingHumanReminderLoopScansNewPhase:
    """v0.16.0: _run_awaiting_input_reminders now scans AWAITING_HUMAN
    instead of EXECUTING/EXECUTOR_ADDRESSING.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def test_awaiting_human_agent_gets_reminder_after_interval(self, tmp_path: Path):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
            awaiting_input_reminder_interval_sec=0.0001,
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p",
            phase="AWAITING_HUMAN",
            last_awaiting_notify_at=time.time() - 10.0,
            last_classifier_verdict={
                "reason": "asking which option",
                "last_assistant_text": "A or B?",
            },
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        event_loop_mod._run_awaiting_input_reminders()
        kinds = [k for (k, _b) in self._notified]
        assert kinds == ["awaiting_human"]

    def test_executing_agent_no_longer_triggers_reminder(self, tmp_path: Path):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
            awaiting_input_reminder_interval_sec=0.0001,
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p",
            phase="EXECUTING",
            last_awaiting_notify_at=time.time() - 10.0,
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        event_loop_mod._run_awaiting_input_reminders()
        assert self._notified == []


class TestOcSpawnSlashCommand:
    """v0.16.0: /oc spawn slash command parses <project> <task> <prompt>
    and forwards to make_spawn via run_blocking.
    """

    def setup_method(self):
        self._commands_mod = sys.modules["_oco_test_pkg.commands"]

    def test_help_shown_on_no_args(self, tmp_path: Path):
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=None, client=None, projects=None, agents=None)
        h = self._commands_mod.make_oc_spawn(rt)
        out = h("")
        assert "usage:" in out
        assert "<project>" in out
        assert "<task>" in out
        assert "<prompt>" in out

    def test_help_flag(self, tmp_path: Path):
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=None, client=None, projects=None, agents=None)
        h = self._commands_mod.make_oc_spawn(rt)
        assert "usage:" in h("--help")
        assert "usage:" in h("help")

    def test_too_few_args(self, tmp_path: Path):
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=None, client=None, projects=None, agents=None)
        h = self._commands_mod.make_oc_spawn(rt)
        out = h("dodo fix-login")
        assert "needs 3 args" in out


class TestOcResumePrSlashCommand:
    """v0.16.0: /oc resume-pr slash command parses
    <project> <pr_number> [--skip-review] <prompt> and forwards to
    make_resume_pr via run_blocking.
    """

    def setup_method(self):
        self._commands_mod = sys.modules["_oco_test_pkg.commands"]

    def test_help_shown_on_no_args(self):
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=None, client=None, projects=None, agents=None)
        h = self._commands_mod.make_oc_resume_pr(rt)
        out = h("")
        assert "usage:" in out
        assert "--skip-review" in out

    def test_invalid_pr_number(self):
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=None, client=None, projects=None, agents=None)
        h = self._commands_mod.make_oc_resume_pr(rt)
        out = h("dodo abc address comments")
        assert "invalid pr_number" in out

    def test_skip_review_flag_parsed_anywhere_in_args(self, tmp_path: Path, monkeypatch):
        """--skip-review can appear at any position; it's stripped before
        positional parsing."""
        called = {}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        async def fake_handler(args):
            called.update(args)
            import json
            return json.dumps({"ok": True, "data": {
                "agent_id": "x/r", "pr_number": 42, "pr_url": "u",
                "branch": "b", "session_id": "s", "worktree_path": "w",
                "skip_review": args.get("skip_review", False),
            }})
        monkeypatch.setattr(tools_mod, "make_resume_pr", lambda rt: fake_handler)
        rt = tools_mod.Runtime(config=None, client=None, projects=None, agents=None)
        h = self._commands_mod.make_oc_resume_pr(rt)
        out = h("dodo 42 --skip-review address reptiles review")
        assert called["project"] == "dodo"
        assert called["pr_number"] == 42
        assert called["skip_review"] is True
        assert called["prompt"] == "address reptiles review"
        assert "resumed PR #42" in out


class TestResumePrHandler:
    """v0.16.0: oc_resume_pr handler covers gh pr view path, branch
    checkout, session creation, and agent record with pr_url + pr_number
    pre-populated.
    """

    def setup_method(self):
        self._tools_mod = sys.modules["_oco_test_pkg.tools"]
        self._wt_mod = sys.modules["_oco_test_pkg.worktree"]
        self._bootstrap_mod = sys.modules["_oco_test_pkg.bootstrap"]

    def _make_runtime(self, tmp_path, projects_dict, fake_client, gh_pr_view_response: dict, project_label="bck"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        projects = sys.modules["_oco_test_pkg.projects"]
        registry = projects.ProjectRegistry(cfg.projects_file)
        (tmp_path / "fake-repo" / ".git").mkdir(parents=True, exist_ok=True)
        registry.add(label=project_label, repo_path=tmp_path / "fake-repo",
                     base_branch="main", abbrev="bck")
        rt = self._tools_mod.Runtime(
            config=cfg, client=fake_client, projects=registry, agents=agents,
        )
        return rt, agents, registry

    def test_unknown_project_returns_error(self, tmp_path: Path):
        import asyncio, json
        rt, agents, _ = self._make_runtime(
            tmp_path, {}, None, gh_pr_view_response={},
            project_label="bck",
        )
        handler = self._tools_mod.make_resume_pr(rt)
        result = asyncio.run(handler({
            "project": "no-such", "pr_number": 1, "prompt": "x",
        }))
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "unknown project" in payload["error"]

    def test_pr_not_open_returns_error(self, tmp_path: Path, monkeypatch):
        import asyncio, json, subprocess as _sp
        pr_resp = {"state": "MERGED", "headRefName": "x", "url": "u", "number": 1}
        class FakeResult:
            returncode = 0
            stdout = json.dumps(pr_resp)
            stderr = ""
        monkeypatch.setattr(_sp, "run", lambda *a, **kw: FakeResult())
        rt, agents, _ = self._make_runtime(
            tmp_path, {}, None, gh_pr_view_response=pr_resp,
        )
        handler = self._tools_mod.make_resume_pr(rt)
        result = asyncio.run(handler({
            "project": "bck", "pr_number": 1, "prompt": "x",
        }))
        payload = json.loads(result)
        assert payload["ok"] is False
        assert "MERGED" in payload["error"]
        assert "OPEN" in payload["error"]


class TestResumeFromAwaitingHuman:
    """v0.16.1: human-input dispatch surfaces (oc_answer, oc_send,
    @<agent_id>) call _resume_from_awaiting_human immediately after a
    successful send so the agent transitions out of AWAITING_HUMAN
    without waiting for the next _phase_awaiting_human poll tick.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body, agent.phase))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def _setup(self, tmp_path, agent_phase="AWAITING_HUMAN", phase_before="EXECUTING"):
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="bck/test", project_label="bck",
            worktree_path=str(tmp_path), session_id="s",
            branch="bck/test", initial_prompt="p", phase=agent_phase,
            phase_before_awaiting=phase_before if agent_phase == "AWAITING_HUMAN" else None,
            awaiting_human_since=time.time() - 5.0 if agent_phase == "AWAITING_HUMAN" else None,
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agents

    def test_resume_helper_restores_prior_phase(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, "AWAITING_HUMAN", phase_before="EXECUTOR_ADDRESSING")
        agent = agents.get("bck/test")
        result = asyncio.run(event_loop_mod._resume_from_awaiting_human(agent, "test"))
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "EXECUTOR_ADDRESSING"
        assert refreshed.phase_before_awaiting is None
        assert refreshed.awaiting_human_since is None
        kinds = [k for (k, _b, _p) in self._notified]
        assert "awaiting_human_resumed" in kinds

    def test_resume_helper_defaults_to_executing(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, "AWAITING_HUMAN")
        agents.update("bck/test", phase_before_awaiting=None)
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._resume_from_awaiting_human(agent))
        assert agents.get("bck/test").phase == "EXECUTING"

    def test_resume_helper_noop_when_not_awaiting(self, tmp_path: Path):
        import asyncio
        agents = self._setup(tmp_path, "EXECUTING", phase_before=None)
        agent = agents.get("bck/test")
        asyncio.run(event_loop_mod._resume_from_awaiting_human(agent))
        assert agents.get("bck/test").phase == "EXECUTING"
        assert self._notified == []

    def test_oc_send_resumes_awaiting_agent(self, tmp_path: Path):
        import asyncio
        import json
        agents = self._setup(tmp_path, "AWAITING_HUMAN", phase_before="EXECUTING")
        sent = []

        class FakeClient:
            async def send_message_async(self_inner, session_id, directory, text, timeout=30.0):
                sent.append({"session_id": session_id, "text": text})
                return {"queued": True}

        tools_mod = sys.modules["_oco_test_pkg.tools"]
        cfg = plugin_mod._runtime.config
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt

        handler = tools_mod.make_send(rt)
        result_str = asyncio.run(handler({"agent_id": "bck/test", "text": "my answer"}))
        result = json.loads(result_str)
        assert result["ok"] is True
        assert len(sent) == 1
        refreshed = agents.get("bck/test")
        assert refreshed.phase == "EXECUTING", (
            "oc_send to an AWAITING_HUMAN agent must transition it back "
            "without waiting for the poll tick"
        )
        kinds = [k for (k, _b, _p) in self._notified]
        assert "awaiting_human_resumed" in kinds

    def test_oc_send_to_non_awaiting_does_not_emit_resumed(self, tmp_path: Path):
        import asyncio
        import json
        agents = self._setup(tmp_path, "EXECUTING", phase_before=None)
        class FakeClient:
            async def send_message_async(self_inner, session_id, directory, text, timeout=30.0):
                return {"queued": True}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        cfg = plugin_mod._runtime.config
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        handler = tools_mod.make_send(rt)
        asyncio.run(handler({"agent_id": "bck/test", "text": "chase-up"}))
        kinds = [k for (k, _b, _p) in self._notified]
        assert "awaiting_human_resumed" not in kinds, (
            "oc_send to a non-AWAITING agent must not spuriously fire resumed"
        )


class TestServeCmdlineUsesOpencodeHostnameFlag:
    """v0.16.2 regression guard.

    Opencode's CLI accepts `--hostname=` for `opencode serve`, not
    `--host=`. v0.16.0 mistakenly renamed the YAML/dataclass/kwarg
    `serve_hostname` -> `host` AND the spawn flag `--hostname=` ->
    `--host=` in one go; the latter rename made opencode reject every
    spawn ("opencode serve exited during startup rc=1"). The YAML knob
    stays `host` (user-facing API), the outgoing CLI flag stays
    `--hostname=` (opencode requirement). This test pins the cmdline
    so the regression cannot recur.
    """

    def test_spawn_uses_hostname_flag(self, tmp_path, monkeypatch):
        import subprocess
        transport_mod = sys.modules["_oco_test_pkg.transport"]
        captured: dict = {}
        port_open_calls: list = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                self.pid = 12345
                self.returncode = None
            def poll(self):
                return None

        def _fake_port_open(host, port, timeout: float = 0.3) -> bool:
            port_open_calls.append((host, port))
            return len(port_open_calls) > 1

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(transport_mod.shutil, "which", lambda _name: "/fake/opencode")
        monkeypatch.setattr(transport_mod.OpencodeClient, "_port_open", staticmethod(_fake_port_open))
        client = transport_mod.OpencodeClient(
            "http://127.0.0.1:4096", None, host="0.0.0.0",
        )
        client.ensure_server(deadline_sec=1.0, log_dir=None)

        args = captured.get("args") or []
        assert args[:2] == ["/fake/opencode", "serve"], args
        joined = " ".join(args)
        assert "--hostname=0.0.0.0" in joined, (
            f"opencode CLI requires --hostname= (not --host=); got: {joined}"
        )
        assert "--host=" not in joined, (
            f"--host= is not a real opencode flag; got: {joined}"
        )
        assert "--port=4096" in joined


class TestAwaitingContextNotTruncated:
    """v0.16.2: the awaiting_human notification body must carry the
    FULL last assistant text (no head-truncation with ellipsis). The
    previous 500/800/600 char caps in
    `_maybe_notify_new_pending` / `_maybe_notify_awaiting_classified` /
    `_run_awaiting_input_reminders` were silently swallowing context the
    human needs to answer the executor's question.
    """

    def setup_method(self):
        self._notified: list = []
        self._saved_pkg_runtime = plugin_mod._runtime
        self._saved_evloop_runtime = event_loop_mod._runtime
        self._saved_notify = event_loop_mod._notify_event
        event_loop_mod._notify_event = lambda agent, kind, body="": self._notified.append((kind, body))

    def teardown_method(self):
        plugin_mod._runtime = self._saved_pkg_runtime
        event_loop_mod._runtime = self._saved_evloop_runtime
        event_loop_mod._notify_event = self._saved_notify

    def _agent(self, tmp_path: Path, phase: str = "EXECUTING") -> state_mod.Agent:
        cfg = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        cfg.ensure_dirs()
        agents = state_mod.AgentStore(cfg.agents_file)
        agent = state_mod.Agent(
            agent_id="oco/long-context-test", project_label="oco",
            worktree_path=str(tmp_path), session_id="s",
            branch="oco/long-context-test", initial_prompt="p", phase=phase,
        )
        agents.add(agent)
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=None, projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt
        return agent

    def _attach_fake_messages_client(self, agents, mid: str = "m-x"):
        cfg = plugin_mod._runtime.config
        class FakeClient:
            async def get_messages(self_inner, session_id, directory, cursor=None):
                return {"items": [{
                    "message": {"role": "assistant", "id": mid},
                    "parts": [{"type": "text", "text": "x"}],
                }]}
        tools_mod = sys.modules["_oco_test_pkg.tools"]
        rt = tools_mod.Runtime(config=cfg, client=FakeClient(), projects=None, agents=agents)
        plugin_mod._runtime = rt
        event_loop_mod._runtime = rt

    def test_pending_question_path_sends_full_context(self, tmp_path: Path):
        import asyncio
        agent = self._agent(tmp_path)
        agents = plugin_mod._runtime.agents
        self._attach_fake_messages_client(agents)
        long_text = "A" * 5000 + " mid " + "B" * 5000
        asyncio.run(event_loop_mod._maybe_notify_new_pending(
            agent,
            pending_q=[{"id": "q1", "text": "Pick A or B?"}],
            pending_p=[],
            context_text=long_text,
        ))
        assert any(k == "awaiting_human" for (k, _b) in self._notified)
        body = next(b for (k, b) in self._notified if k == "awaiting_human")
        assert long_text in body, "pending-question path must include full context_text"
        first_line = body.split("Context (last assistant text):")[1].splitlines()[1]
        assert not first_line.startswith("... "), (
            "must not prefix context with `... ` head-truncation marker"
        )

    def test_classifier_path_sends_full_last_assistant_text(self, tmp_path: Path):
        import asyncio
        agent = self._agent(tmp_path)
        agents = plugin_mod._runtime.agents
        self._attach_fake_messages_client(agents)
        long_text = "X" * 5000 + " question mark? " + "Y" * 3000
        awaiting_input_mod = sys.modules["_oco_test_pkg.awaiting_input"]
        check = awaiting_input_mod.AwaitingInputCheck(
            awaiting=True, confidence="high", reason="trailing question mark",
            source="regex", last_assistant_text=long_text,
        )
        asyncio.run(event_loop_mod._maybe_notify_awaiting_classified(agent, check))
        body = next(b for (k, b) in self._notified if k == "awaiting_human")
        assert long_text in body, "classifier path must include full last_assistant_text"
        head_line = body.split("Last assistant text:\n", 1)[1].splitlines()[0]
        assert not head_line.startswith("... "), (
            f"must not prefix last assistant text with `... `; got line: {head_line!r}"
        )

    def test_reminder_loop_sends_full_last_assistant_text(self, tmp_path: Path):
        agent = self._agent(tmp_path, phase="AWAITING_HUMAN")
        long_text = "Z" * 4000 + " reminder " + "W" * 4000
        now = time.time()
        rt = event_loop_mod._runtime
        rt.agents.update(
            "oco/long-context-test",
            last_awaiting_notify_at=now - 60 * 60,
            last_classifier_verdict={
                "last_assistant_text": long_text, "reason": "stalled awaiting",
            },
        )
        rt.config.awaiting_input_reminder_interval_sec = 1
        event_loop_mod._run_awaiting_input_reminders()
        body = next(b for (k, b) in self._notified if k == "awaiting_human")
        assert long_text in body, "reminder loop must include full last_assistant_text"
