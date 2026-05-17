from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import bootstrap as bootstrap_mod
from . import commands as commands_mod
from . import event_loop
from . import reviewer as reviewer_mod
from . import worktree as wt_mod
from .config import Config, load_entry_config
from .projects import ProjectRegistry
from .state import AgentStore
from .transport import OpencodeClient, OpencodeError


class CliContext:
    def __init__(self, config: Config, projects: ProjectRegistry, agents: AgentStore, client: OpencodeClient) -> None:
        self.config = config
        self.projects = projects
        self.agents = agents
        self.client = client


def build_context() -> CliContext:
    """Build a stand-alone CLI context (no in-memory event loop required).

    Each ``hermes oco`` subcommand reconstructs this from disk-backed state
    so it works outside an active hermes chat session.
    """
    config = Config.from_plugin_entry(load_entry_config())
    config.ensure_dirs()
    client = OpencodeClient(config.server_url, config.server_password, config.serve_hostname)
    projects = ProjectRegistry(config.projects_file)
    agents = AgentStore(config.agents_file)
    return CliContext(config=config, projects=projects, agents=agents, client=client)


def setup(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="oco_command")

    list_p = subs.add_parser("list", help="List tracked agents (same as /oc list).")
    list_p.add_argument("--archived", "--all", "-a", dest="include_archived", action="store_true",
                        help="Also include archived (DONE > 12h) agents.")

    status_p = subs.add_parser("status", help="Show status for one or all agents.")
    status_p.add_argument("agent_id", nargs="?", default=None)
    status_p.add_argument("--json", dest="as_json", action="store_true", help="Emit raw JSON.")
    status_p.add_argument("--archived", "--all", "-a", dest="include_archived", action="store_true",
                          help="Include archived agents when listing all.")

    attach_p = subs.add_parser("attach", help="Print the last N lines of an agent transcript.")
    attach_p.add_argument("agent_id")
    attach_p.add_argument("--lines", type=int, default=commands_mod.DEFAULT_ATTACH_LINES)

    kill_p = subs.add_parser("kill", help="Abort an agent and (optionally) remove its worktree.")
    kill_p.add_argument("agent_id")
    kill_p.add_argument("--force", action="store_true", help="Skip the interactive confirmation.")
    kill_p.add_argument("--keep-worktree", dest="keep_worktree", action="store_true",
                        help="Leave the git worktree on disk after killing.")

    cancel_p = subs.add_parser("cancel", help="Wind down an agent without merging (keeps record as CANCELLED).")
    cancel_p.add_argument("agent_id")
    cancel_p.add_argument("--reason", default=None, help="Free-form reason recorded on the agent.")
    cancel_p.add_argument("--force", action="store_true", help="Skip the interactive confirmation.")

    subs.add_parser("projects", help="List registered projects.")

    subparser.set_defaults(func=handler)


def handler(args: argparse.Namespace) -> int:
    sub = getattr(args, "oco_command", None)
    if not sub:
        print("usage: hermes oco {list,status,attach,kill,projects}", file=sys.stderr)
        return 2
    dispatch = _DISPATCH.get(sub)
    if dispatch is None:
        print(f"unknown subcommand: {sub}", file=sys.stderr)
        return 2
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def cmd_list(args: argparse.Namespace) -> int:
    ctx = build_context()
    agents = sorted(ctx.agents.list(), key=lambda a: a.created_at)
    include_archived = bool(getattr(args, "include_archived", False))
    print(commands_mod._fmt_list(agents, include_archived=include_archived))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ctx = build_context()
    agent_id = getattr(args, "agent_id", None)
    as_json = bool(getattr(args, "as_json", False))
    include_archived = bool(getattr(args, "include_archived", False))
    if agent_id:
        agent = ctx.agents.get(agent_id)
        if agent is None:
            print(f"unknown agent: {agent_id}", file=sys.stderr)
            return 1
        payload = asdict(agent)
        if as_json:
            print(json.dumps(payload, default=str, indent=2))
        else:
            _print_agent_summary(payload)
        return 0
    agents = sorted(ctx.agents.list(), key=lambda a: a.created_at)
    if as_json:
        visible = [a for a in agents if include_archived or not a.archived]
        rows = [asdict(a) for a in visible]
        print(json.dumps({"agents": rows, "count": len(rows)}, default=str, indent=2))
        return 0
    print(commands_mod._fmt_list(agents, include_archived=include_archived))
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    ctx = build_context()
    agent_id = args.agent_id
    agent = ctx.agents.get(agent_id)
    if agent is None:
        print(f"unknown agent: {agent_id}", file=sys.stderr)
        return 1
    worktree = Path(agent.worktree_path)
    try:
        body = asyncio.run(ctx.client.get_messages(agent.session_id, worktree))
    except OpencodeError as e:
        print(f"transport error: {e}", file=sys.stderr)
        return 1
    items = body.get("items") or []
    if not items:
        print("no transcript yet")
        return 0
    text = commands_mod._join_buffer_text(items, int(args.lines))
    if not text:
        print("no transcript yet")
        return 0
    print(text)
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    ctx = build_context()
    agent_id = args.agent_id
    agent = ctx.agents.get(agent_id)
    if agent is None:
        print(f"unknown agent: {agent_id}", file=sys.stderr)
        return 1
    if not args.force:
        try:
            ans = input(f"kill agent {agent_id!r} and remove worktree? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted")
            return 1
    remove_worktree = not bool(getattr(args, "keep_worktree", False))
    errors = kill_agent(ctx, agent_id, remove_worktree=remove_worktree)
    if errors:
        for line in errors:
            print(f"warn: {line}", file=sys.stderr)
    print(f"killed {agent_id} (worktree_removed={remove_worktree})")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    ctx = build_context()
    agent_id = args.agent_id
    agent = ctx.agents.get(agent_id)
    if agent is None:
        print(f"unknown agent: {agent_id}", file=sys.stderr)
        return 1
    if agent.phase in {"DONE", "KILLED", "CANCELLED"}:
        print(f"cannot cancel {agent_id}: already {agent.phase}", file=sys.stderr)
        return 1
    if not args.force:
        try:
            ans = input(f"cancel agent {agent_id!r} and remove worktree? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted")
            return 1
    errors = cancel_agent(ctx, agent_id, reason=args.reason)
    if errors:
        for line in errors:
            print(f"warn: {line}", file=sys.stderr)
    print(f"cancelled {agent_id} (reason={args.reason or 'manually cancelled'})")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    ctx = build_context()
    projects = ctx.projects.list()
    if not projects:
        print("no projects registered")
        return 0
    headers = ("label", "abbrev", "repo_path", "base_branch", "exists")
    rows: list[tuple[str, ...]] = [headers]
    for p in projects:
        rows.append((
            p.label, p.abbrev, p.repo_path, p.base_branch,
            "yes" if Path(p.repo_path).is_dir() else "no",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    for idx, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if idx == 0:
            print("  ".join("-" * w for w in widths))
    return 0


def kill_agent(ctx: CliContext, agent_id: str, *, remove_worktree: bool) -> list[str]:
    """Sync-friendly equivalent of the ``oc_kill`` tool handler.

    Deletes executor + reviewer opencode sessions (if any), optionally tears
    down the git worktree(s), and removes the agent from the registry.
    Returns a list of non-fatal error strings; the kill is best-effort.
    """
    errors: list[str] = []
    agent = ctx.agents.get(agent_id)
    if agent is None:
        return [f"unknown agent: {agent_id}"]
    worktree_path = Path(agent.worktree_path)
    try:
        asyncio.run(ctx.client.delete_session(agent.session_id, worktree_path))
    except OpencodeError as e:
        errors.append(f"delete_session: {e}")
    if agent.reviewer_session_id and agent.reviewer_worktree_path:
        try:
            asyncio.run(ctx.client.delete_session(
                agent.reviewer_session_id, Path(agent.reviewer_worktree_path),
            ))
        except OpencodeError as e:
            errors.append(f"delete_reviewer_session: {e}")
    project = ctx.projects.get(agent.project_label)
    if remove_worktree:
        if project and agent.reviewer_worktree_path:
            reviewer_mod.teardown_reviewer_worktree(project, worktree_path)
        if project:
            wt_mod.remove_worktree(Path(project.repo_path), worktree_path)
        elif worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
    try:
        ctx.agents.update(agent_id, phase="KILLED")
    except Exception as e:
        errors.append(f"update phase: {e}")
    event_loop._drop_snapshots(agent_id)
    ctx.agents.remove(agent_id)
    return errors


def cancel_agent(ctx: CliContext, agent_id: str, *, reason: str | None = None) -> list[str]:
    import time
    errors: list[str] = []
    agent = ctx.agents.get(agent_id)
    if agent is None:
        return [f"unknown agent: {agent_id}"]
    worktree_path = Path(agent.worktree_path)
    try:
        asyncio.run(ctx.client.delete_session(agent.session_id, worktree_path))
    except OpencodeError as e:
        errors.append(f"delete_session: {e}")
    except Exception as e:
        errors.append(f"delete_session: {type(e).__name__}: {e}")
    if agent.reviewer_session_id and agent.reviewer_worktree_path:
        try:
            asyncio.run(ctx.client.delete_session(
                agent.reviewer_session_id, Path(agent.reviewer_worktree_path),
            ))
        except OpencodeError as e:
            errors.append(f"delete_reviewer_session: {e}")
        except Exception as e:
            errors.append(f"delete_reviewer_session: {type(e).__name__}: {e}")
    project = ctx.projects.get(agent.project_label)
    if project and agent.reviewer_worktree_path:
        try:
            reviewer_mod.teardown_reviewer_worktree(project, worktree_path)
        except Exception as e:
            errors.append(f"teardown_reviewer_worktree: {e}")
    if project:
        try:
            cleanup_result = asyncio.run(bootstrap_mod.run_project_cleanup(ctx.client, project, worktree_path))
            if not cleanup_result.ok:
                errors.append(f"cleanup_skill: {cleanup_result.detail}")
        except Exception as e:
            errors.append(f"cleanup_skill exception: {e}")
        try:
            wt_mod.remove_worktree(Path(project.repo_path), worktree_path, force=True)
        except Exception as e:
            errors.append(f"remove_worktree: {e}")
    elif worktree_path.exists():
        import shutil as _shutil
        _shutil.rmtree(worktree_path, ignore_errors=True)
    try:
        ctx.agents.update(
            agent_id,
            phase="CANCELLED",
            cancelled_at=time.time(),
            cancellation_reason=reason or "manually cancelled",
        )
    except Exception as e:
        errors.append(f"update: {e}")
    event_loop._drop_snapshots(agent_id)
    return errors


def _print_agent_summary(payload: dict[str, Any]) -> None:
    keys = [
        "agent_id", "project_label", "phase", "branch", "session_id",
        "worktree_path", "pr_url", "pr_number", "pr_merged_at",
        "done_at", "created_at", "last_activity_at", "last_error",
    ]
    width = max(len(k) for k in keys)
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        print(f"  {k.ljust(width)}  {v}")


_DISPATCH = {
    "list": cmd_list,
    "status": cmd_status,
    "attach": cmd_attach,
    "kill": cmd_kill,
    "cancel": cmd_cancel,
    "projects": cmd_projects,
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="hermes oco")
    setup(parser)
    ns = parser.parse_args()
    sys.exit(handler(ns))
