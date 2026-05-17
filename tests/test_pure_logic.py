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
