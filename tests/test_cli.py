from __future__ import annotations

import argparse
import importlib.util
import io
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
cli_mod = sys.modules["_oco_test_pkg.cli"]
state_mod = sys.modules["_oco_test_pkg.state"]
projects_mod = sys.modules["_oco_test_pkg.projects"]
config_mod = sys.modules["_oco_test_pkg.config"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes oco")
    cli_mod.setup(parser)
    return parser


class _StubClient:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.messages_payload: dict = {"items": []}

    async def delete_session(self, session_id, directory):
        self.deleted.append((session_id, str(directory)))
        return True

    async def get_messages(self, session_id, directory, cursor=None):
        return self.messages_payload


class _StubCtx:
    def __init__(self, tmp_path: Path, client=None):
        self.config = config_mod.Config(
            projects_file=tmp_path / "projects.json",
            agents_file=tmp_path / "agents.json",
            worktrees_root=tmp_path / "wt",
            logs_dir=tmp_path / "logs",
            notifications_file=tmp_path / "notifications.jsonl",
        )
        self.config.ensure_dirs()
        self.projects = projects_mod.ProjectRegistry(self.config.projects_file)
        self.agents = state_mod.AgentStore(self.config.agents_file)
        self.client = client or _StubClient()


def _agent(**overrides) -> state_mod.Agent:
    defaults = dict(
        agent_id="dp/refunds",
        project_label="dodo-payments",
        worktree_path="/tmp/wt-dp-refunds",
        session_id="ses_dp",
        branch="dp/refunds",
        initial_prompt="refund flow",
        phase="EXECUTING",
    )
    defaults.update(overrides)
    return state_mod.Agent(**defaults)


class TestOcoSetup:
    def test_registers_all_five_subcommands(self):
        parser = _build_parser()
        for sub in ("list", "status", "attach", "kill", "projects"):
            ns = parser.parse_args([sub] + (["agent"] if sub in ("attach", "kill") else []))
            assert ns.oco_command == sub

    def test_help_does_not_raise(self):
        parser = _build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--help"])
        assert excinfo.value.code == 0

    def test_attach_help_does_not_raise(self):
        parser = _build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["attach", "--help"])
        assert excinfo.value.code == 0

    def test_attach_requires_agent_id(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["attach"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "agent_id" in err

    def test_kill_requires_agent_id(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["kill"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "agent_id" in err

    def test_attach_default_lines(self):
        parser = _build_parser()
        ns = parser.parse_args(["attach", "dp/x"])
        commands_mod = sys.modules["_oco_test_pkg.commands"]
        assert ns.lines == commands_mod.DEFAULT_ATTACH_LINES

    def test_attach_lines_override(self):
        parser = _build_parser()
        ns = parser.parse_args(["attach", "dp/x", "--lines", "200"])
        assert ns.lines == 200

    def test_status_supports_optional_agent_and_json(self):
        parser = _build_parser()
        ns_all = parser.parse_args(["status"])
        assert ns_all.agent_id is None
        assert ns_all.as_json is False
        ns_one = parser.parse_args(["status", "dp/x", "--json"])
        assert ns_one.agent_id == "dp/x"
        assert ns_one.as_json is True

    def test_kill_force_and_keep_worktree(self):
        parser = _build_parser()
        ns = parser.parse_args(["kill", "dp/x", "--force", "--keep-worktree"])
        assert ns.force is True
        assert ns.keep_worktree is True


class TestOcoDispatch:
    def test_handler_returns_usage_when_no_subcommand(self, capsys):
        parser = _build_parser()
        ns = parser.parse_args([])
        code = cli_mod.handler(ns)
        assert code == 2
        err = capsys.readouterr().err
        assert "hermes oco" in err

    def test_list_dispatches_to_cmd_list(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        ctx.agents.add(_agent())
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["list"])
        code = cli_mod.handler(ns)
        out = capsys.readouterr().out
        assert code == 0
        assert "dp/refunds" in out

    def test_projects_dispatches_to_cmd_projects(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["projects"])
        code = cli_mod.handler(ns)
        assert code == 0
        assert "no projects registered" in capsys.readouterr().out

    def test_status_unknown_agent_returns_error(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["status", "missing/x"])
        code = cli_mod.handler(ns)
        assert code == 1
        assert "unknown agent" in capsys.readouterr().err

    def test_status_json_returns_agent_payload(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        ctx.agents.add(_agent())
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["status", "dp/refunds", "--json"])
        code = cli_mod.handler(ns)
        out = capsys.readouterr().out
        assert code == 0
        assert '"agent_id"' in out
        assert "dp/refunds" in out

    def test_attach_unknown_agent_returns_error(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["attach", "missing/x"])
        code = cli_mod.handler(ns)
        assert code == 1
        assert "unknown agent" in capsys.readouterr().err

    def test_attach_prints_buffer_text(self, monkeypatch, tmp_path, capsys):
        client = _StubClient()
        client.messages_payload = {
            "items": [{"id": "m1", "parts": [{"type": "text", "text": "hello\nworld"}]}]
        }
        ctx = _StubCtx(tmp_path, client=client)
        ctx.agents.add(_agent())
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["attach", "dp/refunds", "--lines", "5"])
        code = cli_mod.handler(ns)
        out = capsys.readouterr().out
        assert code == 0
        assert "hello" in out and "world" in out

    def test_attach_empty_items_prints_no_transcript(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        ctx.agents.add(_agent())
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["attach", "dp/refunds"])
        code = cli_mod.handler(ns)
        assert code == 0
        assert "no transcript yet" in capsys.readouterr().out

    def test_kill_force_skips_prompt_and_deletes_session(self, monkeypatch, tmp_path, capsys):
        client = _StubClient()
        ctx = _StubCtx(tmp_path, client=client)
        ctx.agents.add(_agent())
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["kill", "dp/refunds", "--force", "--keep-worktree"])
        code = cli_mod.handler(ns)
        assert code == 0
        out = capsys.readouterr().out
        assert "killed dp/refunds" in out
        assert ctx.agents.get("dp/refunds") is None
        assert client.deleted and client.deleted[0][0] == "ses_dp"

    def test_kill_aborts_when_user_declines(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        ctx.agents.add(_agent())
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "n")
        parser = _build_parser()
        ns = parser.parse_args(["kill", "dp/refunds"])
        code = cli_mod.handler(ns)
        assert code == 1
        assert "aborted" in capsys.readouterr().out
        assert ctx.agents.get("dp/refunds") is not None

    def test_kill_unknown_agent_returns_error(self, monkeypatch, tmp_path, capsys):
        ctx = _StubCtx(tmp_path)
        monkeypatch.setattr(cli_mod, "build_context", lambda: ctx)
        parser = _build_parser()
        ns = parser.parse_args(["kill", "missing/x", "--force"])
        code = cli_mod.handler(ns)
        assert code == 1
        assert "unknown agent" in capsys.readouterr().err
