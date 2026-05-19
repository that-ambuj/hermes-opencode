from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest


def _load_plugin():
    root = Path(__file__).resolve().parent.parent
    pkg_name = "_oco_test_pkg_resilience"
    spec = importlib.util.spec_from_file_location(
        pkg_name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = pkg_name
    pkg.__path__ = [str(root)]
    sys.modules.setdefault(pkg_name, pkg)
    spec.loader.exec_module(pkg)
    return pkg_name


_PKG = _load_plugin()
transport_mod = sys.modules[f"{_PKG}.transport"]
state_mod = sys.modules[f"{_PKG}.state"]
event_loop_mod = sys.modules[f"{_PKG}.event_loop"]
commands_mod = sys.modules[f"{_PKG}.commands"]


def _make_agent(**overrides):
    base = dict(
        agent_id="p/test",
        project_label="p",
        worktree_path="/tmp/wt/p__test",
        session_id="ses_test",
        branch="p/test",
        initial_prompt="do thing",
        phase="EXECUTING",
    )
    base.update(overrides)
    return state_mod.Agent(**base)


def test_wrap_transport_errors_converts_connect_error():
    @transport_mod._wrap_transport_errors
    async def boom():
        raise httpx.ConnectError("connection refused")

    with pytest.raises(transport_mod.OpencodeError) as exc_info:
        asyncio.run(boom())
    assert "ConnectError" in str(exc_info.value)
    assert "connection refused" in str(exc_info.value)


def test_wrap_transport_errors_passes_opencode_error_through():
    @transport_mod._wrap_transport_errors
    async def boom():
        raise transport_mod.OpencodeError("server returned 500")

    with pytest.raises(transport_mod.OpencodeError) as exc_info:
        asyncio.run(boom())
    assert str(exc_info.value) == "server returned 500"


def test_wrap_transport_errors_lets_non_httpx_exceptions_through():
    @transport_mod._wrap_transport_errors
    async def boom():
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        asyncio.run(boom())


def test_wrap_transport_errors_returns_normal_values():
    @transport_mod._wrap_transport_errors
    async def fine():
        return {"ok": True}

    result = asyncio.run(fine())
    assert result == {"ok": True}


def test_wait_idle_wraps_connect_error(monkeypatch):
    client = transport_mod.OpencodeClient("127.0.0.1", 0)

    class _StubV2:
        async def v2_session_wait(self, **_kwargs):
            raise httpx.ConnectError("refused")

    class _StubSDK:
        def __init__(self):
            self.v2 = _StubV2()

    monkeypatch.setattr(client, "_sdk", lambda *a, **kw: _StubSDK())

    with pytest.raises(transport_mod.OpencodeError) as exc_info:
        asyncio.run(client.wait_idle("ses_id", Path("/tmp")))
    assert "ConnectError" in str(exc_info.value)


def test_prepare_serve_log_path_creates_dir(tmp_path):
    log_dir = tmp_path / "logs"
    result = transport_mod.OpencodeClient._prepare_serve_log_path(log_dir)
    assert result is not None
    assert result.parent == log_dir
    assert log_dir.is_dir()
    assert result.name.startswith("opencode-serve.")
    assert result.name.endswith(".log")


def test_prepare_serve_log_path_returns_none_when_log_dir_none():
    result = transport_mod.OpencodeClient._prepare_serve_log_path(None)
    assert result is None


def test_tail_log_returns_last_n_lines(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("\n".join(f"line {i}" for i in range(20)))
    tail = transport_mod.OpencodeClient._tail_log(log, 5)
    assert tail.splitlines() == ["line 15", "line 16", "line 17", "line 18", "line 19"]


def test_tail_log_returns_empty_when_log_missing(tmp_path):
    assert transport_mod.OpencodeClient._tail_log(tmp_path / "nope.log", 5) == ""
    assert transport_mod.OpencodeClient._tail_log(None, 5) == ""


def test_agent_has_tick_failure_fields():
    agent = _make_agent()
    assert agent.last_tick_error is None
    assert agent.last_tick_error_at is None
    assert agent.consecutive_tick_failures == 0


def test_fmt_list_renders_tick_failure_glyph():
    now = 1_000_000.0
    agent = _make_agent(last_activity_at=now - 60, consecutive_tick_failures=5,
                        last_tick_error="ConnectError: refused")
    output = commands_mod._fmt_list([agent], now_ts=now)
    assert "↻ 5 tick fails" in output


def test_fmt_list_renders_tick_error_continuation_when_threshold_reached():
    now = 1_000_000.0
    agent = _make_agent(last_activity_at=now - 60, consecutive_tick_failures=10,
                        last_tick_error="ConnectError: refused")
    output = commands_mod._fmt_list([agent], now_ts=now)
    assert "tick error: ConnectError: refused" in output


def test_fmt_list_omits_tick_error_below_threshold():
    now = 1_000_000.0
    agent = _make_agent(last_activity_at=now - 60, consecutive_tick_failures=2,
                        last_tick_error="ConnectError: refused")
    output = commands_mod._fmt_list([agent], now_ts=now)
    assert "↻ 2 tick fails" in output
    assert "tick error:" not in output


def test_agent_store_update_can_clear_fields(tmp_path):
    agents_file = tmp_path / "agents.json"
    store = state_mod.AgentStore(agents_file)
    agent = _make_agent(last_tick_error="boom", consecutive_tick_failures=3)
    store.add(agent)
    store.update(agent.agent_id, last_tick_error=None, consecutive_tick_failures=0)
    refreshed = store.get(agent.agent_id)
    assert refreshed.last_tick_error is None
    assert refreshed.consecutive_tick_failures == 0


def test_last_assistant_text_helpers_have_distinct_signatures():
    items_fn = event_loop_mod._last_assistant_text
    agent_fn = event_loop_mod._fetch_last_assistant_text
    assert items_fn is not agent_fn
    assert not asyncio.iscoroutinefunction(items_fn)
    assert asyncio.iscoroutinefunction(agent_fn)
    sample = [
        {
            "message": {"role": "assistant", "id": "msg_1"},
            "parts": [{"type": "text", "text": "hello"}],
        }
    ]
    assert items_fn(sample) == "hello"
    user_only = [
        {
            "message": {"role": "user", "id": "msg_u"},
            "parts": [{"type": "text", "text": "user prompt"}],
        }
    ]
    assert items_fn(user_only) == ""
    reasoning_only = [
        {
            "message": {"role": "assistant", "id": "msg_r"},
            "parts": [{"type": "reasoning", "text": "thinking out loud"}],
        }
    ]
    assert items_fn(reasoning_only) == ""


def test_fetch_last_assistant_text_does_not_iterate_agent_directly(monkeypatch):
    agent = _make_agent()
    monkeypatch.setattr(event_loop_mod, "_runtime", None)
    monkeypatch.setattr(event_loop_mod, "get_text_buffer", lambda _: {})
    result = asyncio.run(event_loop_mod._fetch_last_assistant_text(agent))
    assert result == ""


def test_build_serve_recovered_notification_shape(monkeypatch):
    cfg = MagicMock()
    cfg.endpoint = "127.0.0.1:4096"
    runtime = MagicMock()
    runtime.config = cfg
    monkeypatch.setattr(event_loop_mod, "_runtime", runtime)

    title, body, meta = event_loop_mod._build_serve_recovered_notification()
    assert "recovered" in title.lower()
    assert "127.0.0.1:4096" in body
    assert meta["kind"] == "serve_recovered"
    assert meta["endpoint"] == "127.0.0.1:4096"
