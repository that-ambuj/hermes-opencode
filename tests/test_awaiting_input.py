from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_plugin():
    root = Path(__file__).resolve().parent.parent
    pkg_name = "_oco_test_pkg_awaiting"
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
config_mod = sys.modules[f"{_PKG}.config"]
awaiting_input_mod = sys.modules[f"{_PKG}.awaiting_input"]
tools_mod = sys.modules[f"{_PKG}.tools"]


def _runtime(classifier_enabled: bool = True, **overrides):
    cfg = config_mod.Config()
    cfg.classifier_enabled = classifier_enabled
    for k, v in overrides.items():
        setattr(cfg, k, v)

    class _Stub:
        def __init__(self, c):
            self.config = c

    return _Stub(cfg)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("All done. PR opened.", False),
        ("I refactored the function. Test suite is green.", False),
        ("Which option would you prefer?", True),
        ("I see two paths.\n- Option A: refactor\n- Option B: rewrite\nWhich one?", True),
        ("Should I delete the old file?", True),
        ("Please confirm before I push.", True),
        ("y/n", True),
        ("Awaiting your input on the next step.", True),
        ("Let me know which path you want.", True),
        ("Option A: refactor X\nOption B: rewrite Y", True),
    ],
)
def test_regex_check_classification(text: str, expected: bool):
    awaiting, _reason = awaiting_input_mod.regex_check(text)
    assert awaiting is expected, f"regex_check({text!r}) -> {awaiting}, expected {expected}"


def test_check_returns_high_confidence_on_empty_text():
    runtime = _runtime()
    result = asyncio.run(awaiting_input_mod.check(runtime, ""))
    assert result.awaiting is False
    assert result.confidence == "high"
    assert result.source == "regex"


def test_check_uses_regex_when_classifier_disabled():
    runtime = _runtime(classifier_enabled=False)
    pos = asyncio.run(awaiting_input_mod.check(runtime, "Should I proceed?"))
    assert pos.awaiting is True
    assert pos.source == "regex-no-llm"
    neg = asyncio.run(awaiting_input_mod.check(runtime, "Refactor complete."))
    assert neg.awaiting is False
    assert neg.source == "regex-no-llm"


def test_check_falls_back_when_auxiliary_client_missing(monkeypatch):
    runtime = _runtime(classifier_enabled=True)
    monkeypatch.setitem(sys.modules, "agent", None)
    result = asyncio.run(awaiting_input_mod.check(runtime, "Should I proceed?"))
    assert result.source == "regex-fallback"
    assert result.awaiting is True


def test_check_falls_back_when_classifier_raises(monkeypatch):
    runtime = _runtime(classifier_enabled=True)

    async def boom(**_):
        raise RuntimeError("forced classifier failure")

    import types
    fake_module = types.ModuleType("agent.auxiliary_client")
    fake_module.async_call_llm = boom  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("agent")
    fake_parent.auxiliary_client = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_module)

    result = asyncio.run(awaiting_input_mod.check(runtime, "Should I proceed?"))
    assert result.source == "regex-fallback"
    assert result.awaiting is True
    assert "forced classifier failure" in result.reason


def test_check_consumes_llm_response_when_classifier_succeeds(monkeypatch):
    runtime = _runtime(classifier_enabled=True)

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    captured_kwargs = {}

    async def fake_call(**kwargs):
        captured_kwargs.update(kwargs)
        return _Resp('{"awaiting": true, "confidence": "high", "reason": "explicit options"}')

    import types
    fake_module = types.ModuleType("agent.auxiliary_client")
    fake_module.async_call_llm = fake_call  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("agent")
    fake_parent.auxiliary_client = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_module)

    text = "Should I deploy now?"
    result = asyncio.run(awaiting_input_mod.check(runtime, text))
    assert result.source == "llm"
    assert result.awaiting is True
    assert result.confidence == "high"
    assert result.reason == "explicit options"
    assert captured_kwargs["task"] == runtime.config.classifier_task_name
    assert captured_kwargs["temperature"] == 0.0


def test_check_parses_negative_llm_response(monkeypatch):
    runtime = _runtime(classifier_enabled=True)

    class _Resp:
        choices = [
            type("Choice", (), {
                "message": type("Msg", (), {
                    "content": '{"awaiting": false, "confidence": "high", "reason": "terminal completion"}',
                })(),
            })(),
        ]

    async def fake_call(**_):
        return _Resp()

    import types
    fake_module = types.ModuleType("agent.auxiliary_client")
    fake_module.async_call_llm = fake_call  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("agent")
    fake_parent.auxiliary_client = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_module)

    result = asyncio.run(awaiting_input_mod.check(runtime, "All done."))
    assert result.source == "llm"
    assert result.awaiting is False


def test_check_falls_back_on_unparseable_llm_response(monkeypatch):
    runtime = _runtime(classifier_enabled=True)

    class _Resp:
        choices = [
            type("Choice", (), {
                "message": type("Msg", (), {"content": "definitely not json"})(),
            })(),
        ]

    async def fake_call(**_):
        return _Resp()

    import types
    fake_module = types.ModuleType("agent.auxiliary_client")
    fake_module.async_call_llm = fake_call  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("agent")
    fake_parent.auxiliary_client = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_module)

    result = asyncio.run(awaiting_input_mod.check(runtime, "Should I proceed?"))
    assert result.source == "regex-fallback"
    assert result.awaiting is True


def test_parse_classifier_response_strict():
    awaiting, conf, reason = awaiting_input_mod.parse_classifier_response(
        '{"awaiting": true, "confidence": "medium", "reason": "asks for confirmation"}'
    )
    assert awaiting is True
    assert conf == "medium"
    assert reason == "asks for confirmation"


def test_parse_classifier_response_tolerates_surrounding_text():
    awaiting, conf, reason = awaiting_input_mod.parse_classifier_response(
        'preface...\n{"awaiting": false, "confidence": "high", "reason": "done"}\n...trailing'
    )
    assert awaiting is False
    assert conf == "high"
    assert reason == "done"


def test_parse_classifier_response_clamps_invalid_confidence():
    awaiting, conf, reason = awaiting_input_mod.parse_classifier_response(
        '{"awaiting": true, "confidence": "BOGUS", "reason": ""}'
    )
    assert awaiting is True
    assert conf == "low"
    assert reason == "(no reason)"


def test_parse_classifier_response_raises_on_no_json():
    with pytest.raises(ValueError):
        awaiting_input_mod.parse_classifier_response("just plain text")


def test_build_classifier_messages_truncates_long_input():
    text = "x" * 10000
    msgs = awaiting_input_mod.build_classifier_messages(text, max_chars=500)
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    user_content = msgs[1]["content"]
    assert "truncated head" in user_content
    assert len(user_content) <= 2 * 500 + 200


def test_to_dict_round_trips():
    check = awaiting_input_mod.AwaitingInputCheck(
        awaiting=True, confidence="medium",
        reason="example", source="llm", last_assistant_text="snippet",
    )
    d = awaiting_input_mod.to_dict(check)
    assert d["awaiting"] is True
    assert d["confidence"] == "medium"
    assert d["source"] == "llm"
    assert d["last_assistant_text"] == "snippet"


def test_wrap_initial_prompt_preserves_user_body():
    user_prompt = "implement feature X in module Y"
    wrapped = tools_mod.wrap_initial_prompt(user_prompt)
    assert wrapped.endswith(user_prompt)
    assert "[SYSTEM DIRECTIVE: HERMES-OPENCODE - ORCHESTRATOR RULES]" in wrapped
    assert "[END SYSTEM DIRECTIVE]" in wrapped
    assert "OH-MY-OPENCODE" not in wrapped
