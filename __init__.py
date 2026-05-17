"""Hermes plugin: hermes-opencode.

Registers a tool catalog, lifecycle hooks, and a background event loop that
orchestrate multiple opencode agents running in git worktrees. Drives the
executor -> reviewer -> commit -> PR -> merge cycle, surfaces opencode
questions/permissions to the user, and runs project bootstrap with opencode-
driven recovery.
"""
from __future__ import annotations

from typing import Any

from . import cli as cli_mod
from . import commands as commands_mod
from . import event_loop
from . import notify
from .config import Config, load_entry_config
from .projects import ProjectRegistry
from .state import AgentStore
from .tools import Runtime, all_tool_specs
from .transport import OpencodeClient


_runtime: Runtime | None = None


def _build_pre_llm_context() -> str | None:
    if _runtime is None:
        return None
    questions, permissions = event_loop.get_pending_snapshot()
    if not questions and not permissions:
        return None
    lines: list[str] = ["[hermes-opencode] pending items awaiting the user:"]
    for agent_id, qs in questions.items():
        for entry in qs:
            qid = entry.get("id")
            inner = entry.get("questions") or []
            for q in inner:
                body = (q.get("question") or "").strip()
                opts = q.get("options") or []
                opt_lines = [
                    f"      - {o.get('label')!r}: {o.get('description', '')}"
                    for o in opts if isinstance(o, dict)
                ]
                multi = " (multi-select)" if q.get("multiple") else ""
                custom = " (custom answer allowed)" if q.get("custom") is not False else ""
                block = [
                    f"  • agent={agent_id}  question_id={qid}{multi}{custom}",
                    f"    {body}",
                ]
                if opt_lines:
                    block.append("    options:")
                    block.extend(opt_lines)
                lines.extend(block)
    for agent_id, ps in permissions.items():
        for entry in ps:
            pid = entry.get("id")
            lines.append(
                f"  • agent={agent_id}  permission_id={pid}  type={entry.get('permission')!r}  patterns={entry.get('patterns')}"
            )
    lines.extend([
        "",
        "If the user's reply is an answer to one of these, call oc_answer(question_id=<id>, "
        "answer=<verbatim user reply>) and do NOT respond yourself. For permission requests, "
        "the reply must be one of: once | always | reject. If the user is changing topic, "
        "answer normally.",
    ])
    return "\n".join(lines)


def _pre_llm_call_hook(**_: Any) -> dict[str, Any] | None:
    ctx = _build_pre_llm_context()
    if ctx:
        return {"context": ctx}
    return None


def register(ctx: Any) -> None:
    global _runtime
    config = Config.from_plugin_entry(load_entry_config())
    config.ensure_dirs()

    client = OpencodeClient(config.server_url, config.server_password)
    projects = ProjectRegistry(config.projects_file)
    agents = AgentStore(config.agents_file)

    _runtime = Runtime(config=config, client=client, projects=projects, agents=agents)

    for spec in all_tool_specs(_runtime):
        ctx.register_tool(**spec)

    ctx.register_hook("on_session_start", lambda **kw: _runtime.on_session_start(**kw))
    ctx.register_hook("on_session_end", lambda **kw: _runtime.on_session_end(**kw))
    ctx.register_hook("pre_llm_call", _pre_llm_call_hook)

    inject = getattr(ctx, "inject_message", None)
    if callable(inject):
        notify.set_inject_message(inject)

    register_cmd = getattr(ctx, "register_command", None)
    if callable(register_cmd):
        register_cmd(
            "oc",
            handler=commands_mod.make_oc_dispatcher(_runtime),
            description="Opencode-orchestrator commands: list / attach / questions. Run /oc for help.",
            args_hint="[list|attach <agent_id>|questions|help]",
        )

    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            name="oco",
            help="Drive hermes-opencode agents from the shell.",
            setup_fn=cli_mod.setup,
            handler_fn=cli_mod.handler,
            description="list / status / attach / kill / projects without a hermes chat session.",
        )

    event_loop.start(_runtime)
