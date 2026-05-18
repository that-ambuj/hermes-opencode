from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import bootstrap as bootstrap_mod
from . import event_loop
from . import heartbeat as heartbeat_mod
from . import notify
from . import pr as pr_mod
from . import reviewer as reviewer_mod
from . import worktree as wt
from .config import Config
from .projects import ProjectNotFound, ProjectRegistry
from .state import Agent, AgentNotFound, AgentStore
from .transport import OpencodeClient, OpencodeError


ORCHESTRATOR_DIRECTIVE = (
    "[SYSTEM DIRECTIVE: HERMES-OPENCODE - ORCHESTRATOR RULES]\n"
    "You are running under hermes-opencode orchestration. Three rules govern "
    "how you communicate with the orchestrator:\n"
    "\n"
    "1. Asking for human input. When you need a decision, clarification, "
    "approval, or any other reply from the human user before proceeding, you "
    "MUST use opencode's /question API with explicit options (or a free-form "
    "field when options don't apply). The orchestrator forwards /question "
    "entries to the user's DM channel reliably. Plain-text 'which option do "
    "you prefer?' prompts in your message body are detected by a classifier "
    "as a fallback, but the classifier may be wrong; the /question API is the "
    "authoritative signal. Do NOT trail off in plain text expecting the human "
    "to read your prose as a question.\n"
    "\n"
    "2. Signalling review readiness. When the task is complete and the diff "
    "is ready for code review, emit the literal token READY_FOR_REVIEW on its "
    "own line in your response. This is the authoritative signal that the "
    "orchestrator uses to spawn the reviewer immediately. Without this token "
    "the orchestrator falls back to a slow idle-time heuristic that may delay "
    "the review by minutes. Do NOT emit READY_FOR_REVIEW while you still have "
    "pending todos, an open /question, or work in flight.\n"
    "\n"
    "3. Opening the pull request. When the human reviewer approves your "
    "changes, you will be instructed to commit and open the PR yourself. Emit "
    "PR_OPENED: <github-pr-url> on its own line in your response so the "
    "orchestrator can capture the URL.\n"
    "[END SYSTEM DIRECTIVE]"
)


def wrap_initial_prompt(user_prompt: str) -> str:
    return f"{ORCHESTRATOR_DIRECTIVE}\n\n{user_prompt}"


class Runtime:
    def __init__(
        self,
        config: Config,
        client: OpencodeClient,
        projects: ProjectRegistry,
        agents: AgentStore,
    ) -> None:
        self.config = config
        self.client = client
        self.projects = projects
        self.agents = agents

    def on_session_start(self, **_: Any) -> None:
        self.config.ensure_dirs()
        event_loop.start(self)

    def on_session_end(self, **_: Any) -> None:
        pass


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, default=str, indent=2)


def _err(msg: str, **extra: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "error": msg}
    if extra:
        payload.update(extra)
    return json.dumps(payload, default=str, indent=2)


def _ensure_server(rt: Runtime) -> None:
    if rt.config.auto_spawn_server:
        rt.client.ensure_server(log_dir=rt.config.logs_dir)


PROJECT_ADD_SCHEMA: dict[str, Any] = {
    "name": "oc_project_add",
    "description": "Register a project. Reads the git remote URL to derive a stable project_key; auto-derives a 2-5 char abbrev from the label unless overridden. Required before oc_spawn can be used.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string", "description": "Human-readable project label (kebab-case, e.g. 'dodo-payments')."},
            "repo_path": {"type": "string", "description": "Absolute path to a local git repository."},
            "base_branch": {"type": "string", "description": "Default base branch for new feature branches.", "default": "main"},
            "abbrev": {"type": "string", "description": "2-5 char abbreviation prefix for agent ids (auto-derived if omitted)."},
            "bootstrap_skill": {"type": "string", "description": "Qualified hermes skill name to run during worktree bootstrap (e.g. 'hermes-opencode:dp-bootstrap')."},
        },
        "required": ["label", "repo_path"],
    },
}


PROJECT_LIST_SCHEMA: dict[str, Any] = {
    "name": "oc_project_list",
    "description": "List registered projects with their abbrev, repo_path, base_branch, and whether the repo still exists locally.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


PROJECT_SHOW_SCHEMA: dict[str, Any] = {
    "name": "oc_project_show",
    "description": "Show full configuration for one registered project (project_key, remote_url, repo_path, base_branch, bootstrap_skill).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"label": {"type": "string", "description": "Project label as passed to oc_project_add."}},
        "required": ["label"],
    },
}


PROJECT_REMOVE_SCHEMA: dict[str, Any] = {
    "name": "oc_project_remove",
    "description": "Unregister a project. Refuses if any active (non-terminal) agents are still bound to it; kill them first.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    },
}


PROJECT_SET_REPO_PATH_SCHEMA: dict[str, Any] = {
    "name": "oc_project_set_repo_path",
    "description": "Update the local repo path for a registered project (useful after cloning to a new machine or moving the checkout).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string"},
            "repo_path": {"type": "string", "description": "Absolute path to the moved repo."},
        },
        "required": ["label", "repo_path"],
    },
}


SPAWN_SCHEMA: dict[str, Any] = {
    "name": "oc_spawn",
    "description": (
        "Create a git worktree on a new branch, start an opencode session "
        "bound to it, and forward the human's task to opencode VERBATIM. "
        "Returns the agent_id (format: <abbrev>/<task>, max 20 chars) and "
        "the opencode session id. The plugin's background loop will drive "
        "the executor -> reviewer -> commit -> PR cycle.\n"
        "\n"
        "AUTHORITY MODEL: opencode has FULL authority over the task it "
        "receives. Opencode does its own planning, scoping, decomposition, "
        "file exploration, design, and execution. You (the caller) are a "
        "DISPATCHER, not a planner.\n"
        "\n"
        "You MUST NOT, when constructing the `prompt` arg:\n"
        "  - plan, analyze, or decompose the human's task into steps\n"
        "  - add context, hints, file paths, module names, or suggested approaches\n"
        "  - rewrite, paraphrase, summarize, or 'improve' the human's wording\n"
        "  - prepend background or your own framing\n"
        "  - guess at details the human did not state\n"
        "\n"
        "If the human said 'fix the login bug', the `prompt` is literally "
        "'fix the login bug' and nothing more. Opencode investigates from "
        "there.\n"
        "\n"
        "If the human's task is too vague or ambiguous to act on, do NOT "
        "fill in the gaps yourself. Ask the human a clarifying question "
        "FIRST, then call oc_spawn once they answer."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project": {"type": "string", "description": "Registered project label."},
            "task": {"type": "string", "description": "2-4 kebab-case words summarizing the task; used in agent_id and as branch name."},
            "prompt": {
                "type": "string",
                "description": (
                    "The human's task message, forwarded to opencode VERBATIM. "
                    "Pass the human's literal words. No planning, no analysis, "
                    "no decomposition, no added context, no file hints, no "
                    "suggested approach, no rewriting. Opencode plans its own "
                    "work. If the human's message is unclear, ASK the human "
                    "for clarification before calling this tool. Never fill "
                    "in gaps on opencode's behalf."
                ),
            },
            "branch": {"type": "string", "description": "Branch name (default: agent_id)."},
            "base_branch": {"type": "string", "description": "Branch to fork from (default: project's base_branch)."},
            "agent": {"type": "string", "description": "Opencode agent type to request (default: 'build'; opencode may resolve to a different agent if oh-my-openagent overrides are active).", "default": "build"},
        },
        "required": ["project", "task", "prompt"],
    },
}


RESUME_PR_SCHEMA: dict[str, Any] = {
    "name": "oc_resume_pr",
    "description": (
        "Resume work on an existing OPEN pull request. Looks up the PR via "
        "`gh pr view`, fetches its branch, creates a new worktree CHECKED OUT "
        "on that branch (not a new branch), spawns an opencode session there, "
        "and forwards the human's `prompt` VERBATIM as the follow-up task. "
        "The agent then runs its normal executor -> review -> commit -> "
        "PR-update cycle. The Agent record is created with `pr_url` + "
        "`pr_number` pre-populated so the dashboard and `oc_pr_status` "
        "recognize it immediately.\n"
        "\n"
        "Use this when a previous PR was merged-then-revised, opened-by-a-"
        "human-and-needs-AI-followup, or paused mid-flight and you want to "
        "continue. The original agent record (if any) is NOT touched.\n"
        "\n"
        "Same dispatcher discipline as `oc_spawn`: the `prompt` arg is "
        "forwarded verbatim. Do NOT plan, paraphrase, or summarize it. "
        "Opencode owns the task.\n"
        "\n"
        "Set `skip_review=true` for trivial follow-ups (typo fixes, lint "
        "patches, small comments) where you want to skip the reviewer cycle "
        "entirely. The agent jumps straight from EXECUTING to COMMITTING "
        "once it's idle with a diff."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project": {"type": "string", "description": "Registered project label."},
            "pr_number": {"type": "integer", "description": "Open PR number on the project's github repo."},
            "prompt": {
                "type": "string",
                "description": (
                    "The human's follow-up task, forwarded VERBATIM to "
                    "opencode. No planning, no rewriting, no added context."
                ),
            },
            "skip_review": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, the agent skips the review cycle (sets "
                    "review_cycle_count to review_max_cycles at spawn time so "
                    "the cycle is treated as exhausted) and goes from "
                    "EXECUTING straight to COMMITTING once idle with a diff."
                ),
            },
        },
        "required": ["project", "pr_number", "prompt"],
    },
}


SEND_SCHEMA: dict[str, Any] = {
    "name": "oc_send",
    "description": (
        "Send a follow-up message to a live agent's opencode session. Text "
        "is forwarded VERBATIM, queued asynchronously on the agent's session, "
        "and the tool returns immediately. The agent's reply does NOT come "
        "back in the tool result. Use oc_status or oc_wait to track the "
        "agent's progress, or rely on the plugin's notifications (pending "
        "questions and awaiting-input DMs).\n"
        "\n"
        "Same authority model as oc_spawn: opencode owns the task. You are "
        "a dispatcher. Forward the human's words literally. Do NOT plan, "
        "analyze, paraphrase, add hints, or inject your own framing. If "
        "the human's follow-up is unclear, ask THEM, not opencode. Do NOT "
        "wait for or fabricate the agent's reply in your own response; the "
        "agent will surface its reply through the orchestrator's normal "
        "channels."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
            "text": {
                "type": "string",
                "description": (
                    "The human's follow-up message, forwarded to opencode "
                    "VERBATIM. No planning, no rewriting, no added context. "
                    "Opencode has full authority over how to act on it."
                ),
            },
        },
        "required": ["agent_id", "text"],
    },
}


STATUS_SCHEMA: dict[str, Any] = {
    "name": "oc_status",
    "description": "Show one agent's full status (phase, branch, session_id, pending questions/permissions, pr_url) or a summary table of all tracked agents when agent_id is omitted.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string", "description": "Optional. Omit to list all agents."},
        },
    },
}


WAIT_SCHEMA: dict[str, Any] = {
    "name": "oc_wait",
    "description": "Block until the agent's opencode session goes idle (no more LLM turns running). Returns ok=true on idle, error on timeout. Does not advance the state machine on its own.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
            "timeout_sec": {"type": "number", "default": 600},
        },
        "required": ["agent_id"],
    },
}


KILL_SCHEMA: dict[str, Any] = {
    "name": "oc_kill",
    "description": "Abort an agent and erase it from the registry: delete its opencode session(s) (executor and reviewer), optionally remove its git worktree (default true), drop the agent record entirely. Use for broken/wrong agents you want gone. For 'task abandoned without merging' (e.g. PR closed manually), prefer oc_cancel which keeps the record for audit.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
            "remove_worktree": {"type": "boolean", "default": True},
        },
        "required": ["agent_id"],
    },
}


CANCEL_SCHEMA: dict[str, Any] = {
    "name": "oc_cancel",
    "description": "Wind down a task without merging: runs the cleanup skill, removes the executor + reviewer worktrees, deletes opencode sessions, and sets phase=CANCELLED with an optional reason. Unlike oc_kill, the agent record IS preserved (for audit) and archived after 12h like DONE. Auto-fires when the upstream PR is closed without merging. Refuses on already-terminal agents (DONE / KILLED / CANCELLED).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
            "reason": {"type": "string", "description": "Optional human-readable reason recorded on the agent."},
        },
        "required": ["agent_id"],
    },
}


RETRY_SCHEMA: dict[str, Any] = {
    "name": "oc_retry",
    "description": (
        "Kick an agent to retry its current phase. Three modes:\n"
        "\n"
        "1. FAILED agent: restores `phase_before_failed`, clears retry counts "
        "and tick-failure streak, resumes the agent loop.\n"
        "2. NEEDS_INTERVENTION agent: restores `phase_before_intervention`, "
        "clears the intervention reason, resumes.\n"
        "3. Any other non-terminal agent (EXECUTING / REVIEWING / "
        "COMMITTING / etc.): resets the per-phase retry counter and "
        "`last_tick_error` so the next tick runs with a clean slate. "
        "Useful after a gateway restart, transient network outage, or "
        "to force an immediate re-tick.\n"
        "\n"
        "Refuses on terminal phases that are truly unrecoverable: DONE "
        "(work merged), KILLED (record erased), CANCELLED (deliberate "
        "abandonment). Refuses on FAILED agents whose `last_error` "
        "indicates an unrecoverable cause (e.g. `project gone`)."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
        },
        "required": ["agent_id"],
    },
}


def make_project_add(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        try:
            project = rt.projects.add(
                label=args["label"],
                repo_path=Path(args["repo_path"]),
                base_branch=args.get("base_branch", rt.config.default_base_branch),
                abbrev=args.get("abbrev"),
                bootstrap_skill=args.get("bootstrap_skill"),
            )
            return _ok(asdict(project))
        except (KeyError, ValueError) as e:
            return _err(str(e))
    return handler


def make_project_list(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        rows = []
        for p in rt.projects.list():
            row = asdict(p)
            row["repo_exists"] = Path(p.repo_path).is_dir()
            rows.append(row)
        return _ok({"projects": rows, "count": len(rows)})
    return handler


def make_project_show(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        label = args.get("label")
        if not label:
            return _err("missing label")
        p = rt.projects.get(label)
        if not p:
            return _err(f"unknown project: {label}")
        return _ok(asdict(p))
    return handler


def make_project_remove(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        label = args.get("label")
        if not label:
            return _err("missing label")
        active = [a for a in rt.agents.list() if a.project_label == label and a.phase not in {"DONE", "KILLED", "FAILED"}]
        if active:
            return _err(
                f"cannot remove '{label}': {len(active)} active agent(s). Kill them first.",
                active_agents=[a.agent_id for a in active],
            )
        removed = rt.projects.remove(label)
        if not removed:
            return _err(f"unknown project: {label}")
        return _ok({"removed": asdict(removed)})
    return handler


def make_project_set_repo_path(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        label = args.get("label")
        path = args.get("repo_path")
        if not label or not path:
            return _err("label and repo_path required")
        resolved = Path(path).expanduser().resolve()
        if not (resolved / ".git").exists():
            return _err(f"not a git repository: {resolved}")
        try:
            updated = rt.projects.update(label, repo_path=str(resolved))
            return _ok(asdict(updated))
        except ProjectNotFound:
            return _err(f"unknown project: {label}")
    return handler


def make_spawn(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        try:
            project_label = args["project"]
            task = args["task"]
            prompt = args["prompt"]
        except KeyError as e:
            return _err(f"missing required arg: {e.args[0]}")

        project = rt.projects.get(project_label)
        if not project:
            return _err(f"unknown project: {project_label}. Run oc_project_add first.")
        repo_path = Path(project.repo_path)
        if not (repo_path / ".git").exists():
            return _err(f"project repo missing: {repo_path}")

        if project.bootstrap_skill is None and rt.config.auto_bootstrap_on_first_spawn:
            notify.fanout(
                sinks=rt.config.notify_sinks,
                title="generating bootstrap skill",
                body=f"project {project_label!r} has no bootstrap_skill; spawning a one-shot opencode introspection session before the first agent spawn.",
                meta={"kind": "auto_bootstrap", "project": project_label},
                dashboard_path=rt.config.notifications_file,
                gateway_platform=rt.config.notify_gateway_platform,
                gateway_chat_id=rt.config.notify_gateway_chat_id,
            )
            throwaway = (rt.config.worktrees_root / f"genboot-{project.abbrev}-{int(time.time())}").resolve()
            throwaway_branch = f"oc-orch-genboot-{project.abbrev}-{int(time.time())}"
            try:
                _ensure_server(rt)
            except OpencodeError as e:
                return _err(f"opencode server unavailable: {e}")
            try:
                wt.create_worktree(repo_path, throwaway, branch=throwaway_branch, base=project.base_branch)
            except wt.GitError as e:
                return _err(f"auto bootstrap worktree create failed: {e}")
            try:
                result = await bootstrap_mod.generate_bootstrap_skill(rt.client, project, throwaway, rt.projects)
                if not result.ok:
                    return _err(f"auto bootstrap generation failed: {result.detail}")
                refreshed = rt.projects.get(project_label)
                if refreshed:
                    project = refreshed
            finally:
                wt.remove_worktree(repo_path, throwaway, force=True)

        existing = rt.agents.ids()
        try:
            agent_id = wt.compose_agent_id(project.abbrev, task, existing)
        except ValueError as e:
            return _err(str(e))

        branch = args.get("branch") or agent_id
        base_branch = args.get("base_branch") or project.base_branch
        agent_type = args.get("agent", "build")
        fs = wt.agent_id_to_fs(agent_id)
        worktree_path = (rt.config.worktrees_root / fs).resolve()

        try:
            _ensure_server(rt)
        except OpencodeError as e:
            return _err(f"opencode server unavailable: {e}")

        try:
            wt.create_worktree(repo_path, worktree_path, branch=branch, base=base_branch)
        except wt.GitError as e:
            return _err(f"worktree creation failed: {e}")

        boot = await bootstrap_mod.run_project_bootstrap(rt.client, project, worktree_path)
        if not boot.ok:
            wt.remove_worktree(repo_path, worktree_path)
            return _err(f"bootstrap failed ({boot.method}): {boot.detail}")

        try:
            session = await rt.client.create_session(worktree_path, agent=agent_type)
        except OpencodeError as e:
            wt.remove_worktree(repo_path, worktree_path)
            return _err(f"session create failed: {e}")

        session_id = session.get("id") or session.get("sessionID") or ""
        if not session_id:
            wt.remove_worktree(repo_path, worktree_path)
            return _err(f"opencode returned no session id; keys={list(session.keys())[:8]}")

        agent = Agent(
            agent_id=agent_id,
            project_label=project_label,
            worktree_path=str(worktree_path),
            session_id=session_id,
            branch=branch,
            initial_prompt=prompt,
            phase="EXECUTING",
        )
        rt.agents.add(agent)

        rate_limited_agents = [
            a for a in rt.agents.list() if a.phase == "RATE_LIMITED"
        ]
        if rate_limited_agents:
            blocked_by = [a.agent_id for a in rate_limited_agents]
            rt.agents.update(
                agent_id, phase="QUEUED", queued_blocked_by=blocked_by,
            )
            event_loop.start(rt)
            event_loop.ensure_agent_task(agent_id)
            return _ok({
                "agent_id": agent_id,
                "session_id": session_id,
                "worktree_path": str(worktree_path),
                "branch": branch,
                "queued": True,
                "blocked_by": blocked_by,
                "note": (
                    f"task queued (phase=QUEUED); waiting for {len(blocked_by)} "
                    "rate-limited agent(s) to clear before first turn fires"
                ),
                "bootstrap": {"ok": boot.ok, "method": boot.method, "skill_updated": boot.skill_updated},
            })

        wrapped_prompt = wrap_initial_prompt(prompt)
        try:
            await rt.client.send_message_async(session_id, worktree_path, wrapped_prompt)
        except OpencodeError as e:
            rt.agents.update(agent_id, phase="FAILED", last_error=str(e))
            return _err(f"send_message_async failed: {e}", agent_id=agent_id)

        rt.agents.update(agent_id, phase="EXECUTING")

        event_loop.start(rt)
        event_loop.ensure_agent_task(agent_id)

        return _ok({
            "agent_id": agent_id,
            "session_id": session_id,
            "worktree_path": str(worktree_path),
            "branch": branch,
            "queued": True,
            "note": "first turn queued asynchronously; poll oc_status/oc_wait to track progress",
            "bootstrap": {"ok": boot.ok, "method": boot.method, "skill_updated": boot.skill_updated},
        })
    return handler


def make_resume_pr(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        try:
            project_label = args["project"]
            pr_number = int(args["pr_number"])
            prompt = args["prompt"]
        except (KeyError, ValueError, TypeError) as e:
            return _err(f"missing or invalid required arg: {e}")
        skip_review = bool(args.get("skip_review", False))

        project = rt.projects.get(project_label)
        if not project:
            return _err(f"unknown project: {project_label}. Run oc_project_add first.")
        repo_path = Path(project.repo_path)
        if not (repo_path / ".git").exists():
            return _err(f"project repo missing: {repo_path}")

        import subprocess as _subprocess
        try:
            res = _subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "headRefName,state,url,number"],
                cwd=str(repo_path), capture_output=True, text=True, check=False, timeout=30,
            )
        except (OSError, _subprocess.SubprocessError) as e:
            return _err(f"gh pr view failed: {e}")
        if res.returncode != 0:
            return _err(f"gh pr view returned {res.returncode}: {(res.stderr or res.stdout).strip()}")
        try:
            pr_info = json.loads(res.stdout)
        except (ValueError, TypeError) as e:
            return _err(f"gh pr view output parse failed: {e}")
        state = (pr_info.get("state") or "").upper()
        if state != "OPEN":
            return _err(f"PR #{pr_number} is {state or '(unknown)'}; resume_pr only works on OPEN PRs")
        branch = pr_info.get("headRefName") or ""
        pr_url = pr_info.get("url") or ""
        if not branch:
            return _err("gh pr view returned no headRefName")

        try:
            wt._git(repo_path, "fetch", "origin", branch, check=False)
        except Exception as e:
            return _err(f"git fetch origin {branch} failed: {e}")

        existing = rt.agents.ids()
        task_slug = f"resume-pr-{pr_number}"
        try:
            agent_id = wt.compose_agent_id(project.abbrev, task_slug, existing)
        except ValueError as e:
            return _err(str(e))

        fs = wt.agent_id_to_fs(agent_id)
        worktree_path = (rt.config.worktrees_root / fs).resolve()

        try:
            _ensure_server(rt)
        except OpencodeError as e:
            return _err(f"opencode server unavailable: {e}")

        try:
            wt.create_worktree(repo_path, worktree_path, branch=branch, base=project.base_branch)
        except wt.GitError as e:
            return _err(f"worktree creation failed: {e}")

        boot = await bootstrap_mod.run_project_bootstrap(rt.client, project, worktree_path)
        if not boot.ok:
            wt.remove_worktree(repo_path, worktree_path)
            return _err(f"bootstrap failed ({boot.method}): {boot.detail}")

        try:
            session = await rt.client.create_session(worktree_path, agent="build")
        except OpencodeError as e:
            wt.remove_worktree(repo_path, worktree_path)
            return _err(f"session create failed: {e}")
        session_id = session.get("id") or session.get("sessionID") or ""
        if not session_id:
            wt.remove_worktree(repo_path, worktree_path)
            return _err(f"opencode returned no session id; keys={list(session.keys())[:8]}")

        agent = Agent(
            agent_id=agent_id,
            project_label=project_label,
            worktree_path=str(worktree_path),
            session_id=session_id,
            branch=branch,
            initial_prompt=prompt,
            phase="EXECUTING",
            pr_url=pr_url,
            pr_number=pr_number,
            review_cycle_count=(rt.config.review_max_cycles if skip_review else 0),
        )
        rt.agents.add(agent)

        rate_limited_agents = [a for a in rt.agents.list() if a.phase == "RATE_LIMITED"]
        if rate_limited_agents:
            blocked_by = [a.agent_id for a in rate_limited_agents]
            rt.agents.update(agent_id, phase="QUEUED", queued_blocked_by=blocked_by)
            event_loop.start(rt)
            event_loop.ensure_agent_task(agent_id)
            return _ok({
                "agent_id": agent_id,
                "session_id": session_id,
                "worktree_path": str(worktree_path),
                "branch": branch,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "skip_review": skip_review,
                "queued": True,
                "blocked_by": blocked_by,
                "bootstrap": {"ok": boot.ok, "method": boot.method, "skill_updated": boot.skill_updated},
            })

        wrapped_prompt = wrap_initial_prompt(prompt)
        try:
            await rt.client.send_message_async(session_id, worktree_path, wrapped_prompt)
        except OpencodeError as e:
            rt.agents.update(agent_id, phase="FAILED", last_error=str(e))
            return _err(f"send_message_async failed: {e}", agent_id=agent_id)

        event_loop.start(rt)
        event_loop.ensure_agent_task(agent_id)

        return _ok({
            "agent_id": agent_id,
            "session_id": session_id,
            "worktree_path": str(worktree_path),
            "branch": branch,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "skip_review": skip_review,
            "queued": True,
            "bootstrap": {"ok": boot.ok, "method": boot.method, "skill_updated": boot.skill_updated},
        })
    return handler


def make_send(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        text = args.get("text")
        if not agent_id or text is None:
            return _err("agent_id and text required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        worktree_path = Path(agent.worktree_path)
        try:
            await rt.client.send_message_async(agent.session_id, worktree_path, text)
        except OpencodeError as e:
            return _err(f"send_message_async failed: {e}", agent_id=agent_id)
        rt.agents.update(agent_id, last_activity_at=time.time())
        await event_loop._resume_from_awaiting_human(
            agent, reason="oc_send human reply",
        )
        return _ok({
            "agent_id": agent_id,
            "queued": True,
            "note": "message queued asynchronously; the agent's reply is NOT returned here. Use oc_status or oc_wait to track progress.",
        })
    return handler


def make_status(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if agent_id:
            agent = rt.agents.get(agent_id)
            if not agent:
                return _err(f"unknown agent: {agent_id}")
            return _ok(await _detailed_status(rt, agent))
        rows: list[dict[str, Any]] = []
        for a in rt.agents.list():
            rows.append({
                "agent_id": a.agent_id,
                "project": a.project_label,
                "branch": a.branch,
                "phase": a.phase,
                "pr_url": a.pr_url,
                "created_at": a.created_at,
                "last_activity_at": a.last_activity_at,
            })
        return _ok({"agents": rows, "count": len(rows)})
    return handler


async def _detailed_status(rt: Runtime, agent: Agent) -> dict[str, Any]:
    worktree_path = Path(agent.worktree_path)
    detail: dict[str, Any] = {
        "agent_id": agent.agent_id,
        "project": agent.project_label,
        "phase": agent.phase,
        "branch": agent.branch,
        "session_id": agent.session_id,
        "worktree_path": str(worktree_path),
        "pr_url": agent.pr_url,
        "pr_number": agent.pr_number,
        "pr_merged_at": agent.pr_merged_at,
        "done_at": agent.done_at,
        "created_at": agent.created_at,
        "last_activity_at": agent.last_activity_at,
        "last_error": agent.last_error,
    }
    try:
        questions = await rt.client.list_questions(worktree_path)
        permissions = await rt.client.list_permissions(worktree_path)
        detail["pending_questions"] = [
            {"id": q.get("id"), "session_id": q.get("sessionID"), "n_questions": len(q.get("questions") or [])}
            for q in questions if q.get("sessionID") == agent.session_id
        ]
        detail["pending_permissions"] = [
            {"id": p.get("id"), "session_id": p.get("sessionID"), "permission": p.get("permission")}
            for p in permissions if p.get("sessionID") == agent.session_id
        ]
    except OpencodeError as e:
        detail["transport_error"] = str(e)
    return detail


def make_wait(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        timeout = float(args.get("timeout_sec", 600))
        try:
            became_idle = await rt.client.wait_idle(agent.session_id, Path(agent.worktree_path), timeout=timeout)
        except OpencodeError as e:
            return _err(f"wait failed: {e}")
        if not became_idle:
            return _err(f"timeout waiting for idle ({timeout}s)", agent_id=agent_id)
        rt.agents.update(agent_id, last_activity_at=time.time())
        return _ok({"agent_id": agent_id, "idle": True})
    return handler


def make_kill(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        remove_wt = bool(args.get("remove_worktree", True))
        worktree_path = Path(agent.worktree_path)
        errors: list[str] = []
        try:
            await rt.client.delete_session(agent.session_id, worktree_path)
        except OpencodeError as e:
            errors.append(f"delete_session: {e}")
        if agent.reviewer_session_id and agent.reviewer_worktree_path:
            try:
                await rt.client.delete_session(agent.reviewer_session_id, Path(agent.reviewer_worktree_path))
            except OpencodeError as e:
                errors.append(f"delete_reviewer_session: {e}")
        project = rt.projects.get(agent.project_label)
        if remove_wt:
            if project and agent.reviewer_worktree_path:
                reviewer_mod.teardown_reviewer_worktree(project, worktree_path)
            if project:
                try:
                    cleanup_result = await bootstrap_mod.run_project_cleanup(rt.client, project, worktree_path)
                    if not cleanup_result.ok:
                        errors.append(f"cleanup_skill: {cleanup_result.detail}")
                except Exception as e:
                    errors.append(f"cleanup_skill exception: {e}")
                wt.remove_worktree(Path(project.repo_path), worktree_path)
            elif worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
        rt.agents.update(agent_id, phase="KILLED")
        event_loop._drop_snapshots(agent_id)
        event_loop.clear_text_buffer(agent_id)
        rt.agents.remove(agent_id)
        return _ok({"agent_id": agent_id, "killed": True, "worktree_removed": remove_wt, "errors": errors})
    return handler


def make_cancel(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        if agent.phase in {"DONE", "KILLED", "CANCELLED"}:
            return _err(f"cannot cancel from phase {agent.phase}")
        reason = args.get("reason") or "manually cancelled"
        worktree_path = Path(agent.worktree_path)
        errors: list[str] = []
        try:
            await rt.client.delete_session(agent.session_id, worktree_path)
        except OpencodeError as e:
            errors.append(f"delete_session: {e}")
        except Exception as e:
            errors.append(f"delete_session: {type(e).__name__}: {e}")
        if agent.reviewer_session_id and agent.reviewer_worktree_path:
            try:
                await rt.client.delete_session(agent.reviewer_session_id, Path(agent.reviewer_worktree_path))
            except OpencodeError as e:
                errors.append(f"delete_reviewer_session: {e}")
            except Exception as e:
                errors.append(f"delete_reviewer_session: {type(e).__name__}: {e}")
        project = rt.projects.get(agent.project_label)
        if project and agent.reviewer_worktree_path:
            try:
                reviewer_mod.teardown_reviewer_worktree(project, worktree_path)
            except Exception as e:
                errors.append(f"teardown_reviewer_worktree: {e}")
        if project:
            try:
                cleanup_result = await bootstrap_mod.run_project_cleanup(rt.client, project, worktree_path)
                if not cleanup_result.ok:
                    errors.append(f"cleanup_skill: {cleanup_result.detail}")
            except Exception as e:
                errors.append(f"cleanup_skill exception: {e}")
            try:
                wt.remove_worktree(Path(project.repo_path), worktree_path, force=True)
            except Exception as e:
                errors.append(f"remove_worktree: {e}")
        elif worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        rt.agents.update(
            agent_id,
            phase="CANCELLED",
            cancelled_at=time.time(),
            cancellation_reason=reason,
        )
        event_loop._drop_snapshots(agent_id)
        event_loop.clear_text_buffer(agent_id)
        return _ok({"agent_id": agent_id, "phase": "CANCELLED", "reason": reason, "errors": errors})
    return handler


def make_retry(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        if agent.phase in {"DONE", "KILLED", "CANCELLED"}:
            return _err(f"cannot retry from terminal phase {agent.phase}")
        last_error = (agent.last_error or "").lower()
        if agent.phase == "FAILED" and "project gone" in last_error:
            return _err(
                f"cannot retry: {agent.last_error}. "
                f"Re-add the project via oc_project_add and re-spawn."
            )
        if agent.phase == "FAILED":
            target = agent.phase_before_failed or "EXECUTING"
            updated = rt.agents.update(
                agent_id,
                phase=target,
                phase_before_failed=None,
                last_error=None,
                last_tick_error=None,
                last_tick_error_at=None,
                consecutive_tick_failures=0,
                consecutive_aborts=0,
                last_abort_msg_id=None,
                idle_since=None,
            )
            return _ok({
                "agent_id": agent_id,
                "from_phase": "FAILED",
                "restored_phase": target,
                "mode": "failed-resume",
            })
        if agent.phase == "NEEDS_INTERVENTION":
            target = agent.phase_before_intervention or "EXECUTING"
            updated = rt.agents.update(
                agent_id,
                phase=target,
                phase_before_intervention=None,
                intervention_reason=None,
                intervention_since=None,
                last_error=None,
                idle_since=None,
            )
            return _ok({
                "agent_id": agent_id,
                "from_phase": "NEEDS_INTERVENTION",
                "restored_phase": target,
                "mode": "intervention-resume",
            })
        rt.agents.update(
            agent_id,
            phase_retry_count=0,
            last_error=None,
            last_tick_error=None,
            last_tick_error_at=None,
            consecutive_tick_failures=0,
        )
        return _ok({
            "agent_id": agent_id,
            "phase": agent.phase,
            "mode": "kick",
            "note": "retry counters cleared; next tick runs immediately",
        })
    return handler


ANSWER_SCHEMA: dict[str, Any] = {
    "name": "oc_answer",
    "description": "Reply to (or reject) a pending opencode question raised by an agent. The user's reply text is forwarded VERBATIM to opencode. Use this when the pre_llm_call context surfaces a pending question_id and the user's message looks like a reply to it.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question_id": {"type": "string", "description": "Question id from /question or from the pre_llm_call injection."},
            "answer": {"type": "string", "description": "Verbatim user reply text. Forwarded unmodified to opencode."},
            "answers": {"type": "array", "items": {"type": "string"}, "description": "Alternative: list of selected option labels. Use this when the question listed structured options."},
            "reject": {"type": "boolean", "default": False, "description": "Reject the question instead of answering."},
        },
        "required": ["question_id"],
    },
}


REVIEW_NOW_SCHEMA: dict[str, Any] = {
    "name": "oc_review_now",
    "description": "Force-trigger the reviewer phase for an agent (escape hatch when the auto idle-detector misses or you want to short-circuit waiting). The agent transitions to REVIEW_SPAWNING on the next event-loop tick.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    },
}


REVIEW_AGAIN_SCHEMA: dict[str, Any] = {
    "name": "oc_review_again",
    "description": "Run another review cycle on an agent. Tears down the prior reviewer worktree first, then transitions back to REVIEW_SPAWNING. Useful when the first review pass missed issues or after manual changes.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    },
}


SKIP_REVIEW_SCHEMA: dict[str, Any] = {
    "name": "oc_skip_review",
    "description": "Skip the reviewer cycle entirely and jump straight to COMMITTING + open PR. For trivial agent work that doesn't need review.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    },
}


PR_STATUS_SCHEMA: dict[str, Any] = {
    "name": "oc_pr_status",
    "description": "Live `gh pr view` for an agent's PR — returns number, url, state (OPEN/MERGED/CLOSED), and merged_at. The plugin's bg loop already polls this every 5 min after PR_OPEN; use this tool for an on-demand check.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    },
}


REGEN_BOOTSTRAP_SCHEMA: dict[str, Any] = {
    "name": "oc_project_regenerate_bootstrap",
    "description": "Regenerate the bootstrap skill for a project by spawning a short-lived opencode introspection session that reads the repo (README, package.json, pyproject.toml, Makefile, etc.) and writes a fresh SKILL.md with an idempotent bash setup script.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    },
}


REGEN_CLEANUP_SCHEMA: dict[str, Any] = {
    "name": "oc_project_regenerate_cleanup",
    "description": "Generate (or regenerate) ONLY the cleanup skill for a project, leaving the bootstrap skill untouched. Useful for projects registered before cleanup-skill support landed, or when you've customized the bootstrap and want a fresh cleanup that reverses it. Spawns a short-lived opencode introspection session that reads the existing bootstrap (if any) and the repo, then writes the per-project cleanup SKILL.md.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    },
}


SET_NOTIFY_TARGET_SCHEMA: dict[str, Any] = {
    "name": "oc_set_notify_target",
    "description": "Configure where heartbeats and question alerts are delivered: the gateway DM target (platform + chat_id) and/or the active notify sinks (any combination of cli, gateway, dashboard).",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "platform": {"type": "string", "description": "Gateway platform: telegram | discord | slack | ..."},
            "chat_id": {"type": "string", "description": "Chat / channel id where DMs are delivered."},
            "sinks": {"type": "array", "items": {"type": "string"}, "description": "Active sinks: any of [cli, gateway, dashboard]."},
        },
    },
}


OUTPUT_SCHEMA: dict[str, Any] = {
    "name": "oc_output",
    "description": "Return the latest assistant text for an agent. Prefers the live SSE delta/snapshot buffer (populated by the background consumer) and falls back to a /message pull from opencode when the buffer is empty. Set clear=true to reset the buffer after reading (only meaningful when source=='sse').",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent_id": {"type": "string"},
            "clear": {"type": "boolean", "default": False, "description": "If true and the result came from the SSE buffer, reset the buffer entry after returning."},
        },
        "required": ["agent_id"],
    },
}


HEARTBEAT_NOW_SCHEMA: dict[str, Any] = {
    "name": "oc_heartbeat_send_now",
    "description": "Send the heartbeat status report immediately to all configured sinks (CLI inject_message / gateway DM / dashboard JSONL). Useful for testing the notify pipeline or for ad-hoc status pings outside the hourly schedule.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"force": {"type": "boolean", "default": False, "description": "Send even if outside the day window with no pending tasks."}},
    },
}


def _find_agent_for_question(rt: Runtime, question_id: str) -> Agent | None:
    questions, _perms = event_loop.get_pending_snapshot()
    for agent_id, qs in questions.items():
        for q in qs:
            if q.get("id") == question_id:
                return rt.agents.get(agent_id)
    return None


def make_answer(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        qid = args.get("question_id")
        if not qid:
            return _err("question_id required")
        agent = _find_agent_for_question(rt, qid)
        if not agent:
            return _err(f"no live agent has a pending question with id={qid}")
        worktree = Path(agent.worktree_path)
        try:
            if args.get("reject"):
                ok = await rt.client.reject_question(qid, worktree)
                action = "rejected"
            else:
                answers = args.get("answers")
                if answers is None:
                    answer = args.get("answer")
                    if answer is None:
                        return _err("provide answer (string) or answers (list of option labels)")
                    answers = [answer]
                ok = await rt.client.reply_question(qid, worktree, list(answers))
                action = "replied"
        except OpencodeError as e:
            return _err(f"opencode error: {e}")
        if not ok:
            return _err("opencode rejected the answer")
        await event_loop._resume_from_awaiting_human(
            agent, reason=f"oc_answer {action} for question {qid}",
        )
        return _ok({"agent_id": agent.agent_id, "question_id": qid, "action": action})
    return handler


def make_review_now(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        if agent.phase in {"DONE", "KILLED", "FAILED", "PR_OPEN"}:
            return _err(f"cannot review_now from phase {agent.phase}")
        rt.agents.update(agent_id, phase="REVIEW_SPAWNING")
        event_loop.ensure_agent_task(agent_id)
        return _ok({"agent_id": agent_id, "phase": "REVIEW_SPAWNING"})
    return handler


def make_review_again(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        if agent.phase in {"DONE", "KILLED", "FAILED"}:
            return _err(f"cannot review_again from phase {agent.phase}")
        project = rt.projects.get(agent.project_label)
        if project and agent.reviewer_worktree_path:
            reviewer_mod.teardown_reviewer_worktree(project, Path(agent.worktree_path))
        rt.agents.update(
            agent_id,
            phase="REVIEW_SPAWNING",
            reviewer_session_id=None,
            reviewer_worktree_path=None,
            review_cycle_count=agent.review_cycle_count + 1,
        )
        event_loop.ensure_agent_task(agent_id)
        return _ok({"agent_id": agent_id, "phase": "REVIEW_SPAWNING"})
    return handler


def make_skip_review(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        if agent.phase in {"DONE", "KILLED", "FAILED", "PR_OPEN"}:
            return _err(f"cannot skip_review from phase {agent.phase}")
        rt.agents.update(agent_id, phase="COMMITTING")
        event_loop.ensure_agent_task(agent_id)
        return _ok({"agent_id": agent_id, "phase": "COMMITTING"})
    return handler


def make_pr_status(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        if not agent.pr_number:
            return _err(f"agent {agent_id} has no PR yet (phase={agent.phase})")
        try:
            info = pr_mod.pr_state(Path(agent.worktree_path), agent.pr_number)
        except pr_mod.PrError as e:
            return _err(str(e))
        return _ok({"agent_id": agent_id, "number": info.number, "url": info.url, "state": info.state, "merged_at": info.merged_at})
    return handler


def make_set_notify_target(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        platform = args.get("platform")
        chat_id = args.get("chat_id")
        sinks = args.get("sinks")
        if platform is not None:
            rt.config.notify_gateway_platform = platform
        if chat_id is not None:
            rt.config.notify_gateway_chat_id = chat_id
        if sinks is not None:
            allowed = {"cli", "gateway", "dashboard"}
            bad = [s for s in sinks if s not in allowed]
            if bad:
                return _err(f"unknown sink(s): {bad}; allowed={sorted(allowed)}")
            rt.config.notify_sinks = list(sinks)
        return _ok({
            "platform": rt.config.notify_gateway_platform,
            "chat_id": rt.config.notify_gateway_chat_id,
            "sinks": rt.config.notify_sinks,
            "note": "config is in-memory for this hermes session; persist via ~/.hermes/config.yaml plugins.entries.hermes-opencode.notify",
        })
    return handler


def _pull_assistant_text(body: dict[str, Any]) -> tuple[str, int]:
    items = body.get("items") or []
    chunks: list[str] = []
    parts_count = 0
    for item in items:
        for p in item.get("parts") or []:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str) and t:
                    chunks.append(t)
                    parts_count += 1
    return ("\n".join(chunks), parts_count)


def make_output(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        agent_id = args.get("agent_id")
        if not agent_id:
            return _err("agent_id required")
        agent = rt.agents.get(agent_id)
        if not agent:
            return _err(f"unknown agent: {agent_id}")
        buffer = event_loop.get_text_buffer(agent_id)
        if buffer:
            text = "\n".join(buffer[k] for k in sorted(buffer.keys()) if isinstance(buffer[k], str))
            payload = {"agent_id": agent_id, "text": text, "source": "sse", "parts": len(buffer)}
            if bool(args.get("clear", False)):
                event_loop.clear_text_buffer(agent_id)
            return _ok(payload)
        worktree = Path(agent.worktree_path)
        try:
            body = await rt.client.get_messages(agent.session_id, worktree)
        except OpencodeError as e:
            return _err(f"get_messages failed: {e}")
        text, parts_count = _pull_assistant_text(body)
        return _ok({"agent_id": agent_id, "text": text, "source": "pull", "parts": parts_count})
    return handler


def make_heartbeat_send_now(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        result = heartbeat_mod.send_heartbeat(rt, force=bool(args.get("force", False)))
        return _ok(result)
    return handler


def make_regen_bootstrap(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        label = args.get("label")
        if not label:
            return _err("label required")
        project = rt.projects.get(label)
        if not project:
            return _err(f"unknown project: {label}")
        repo_path = Path(project.repo_path)
        if not (repo_path / ".git").exists():
            return _err(f"project repo missing: {repo_path}")
        throwaway = Path(tempfile.mkdtemp(prefix=f"oc-orch-genboot-{project.abbrev}-"))
        throwaway.rmdir()
        try:
            wt.create_worktree(repo_path, throwaway, branch=f"oc-orch-genboot-{project.abbrev}-{int(time.time())}", base=project.base_branch)
        except wt.GitError as e:
            shutil.rmtree(throwaway, ignore_errors=True)
            return _err(f"throwaway worktree create failed: {e}")
        try:
            try:
                rt.client.ensure_server(log_dir=rt.config.logs_dir)
            except OpencodeError as e:
                return _err(f"opencode server unavailable: {e}")
            result = await bootstrap_mod.generate_bootstrap_skill(rt.client, project, throwaway, rt.projects)
            if not result.ok:
                return _err(f"generation failed: {result.detail}")
            updated = rt.projects.get(label)
            return _ok({
                "label": label,
                "method": result.method,
                "skill_path": result.detail,
                "bootstrap_skill": updated.bootstrap_skill if updated else None,
            })
        finally:
            wt.remove_worktree(repo_path, throwaway, force=True)
    return handler


def make_regen_cleanup(rt: Runtime) -> Callable[..., Awaitable[str]]:
    async def handler(args: dict, **_: Any) -> str:
        label = args.get("label")
        if not label:
            return _err("label required")
        project = rt.projects.get(label)
        if not project:
            return _err(f"unknown project: {label}")
        repo_path = Path(project.repo_path)
        if not (repo_path / ".git").exists():
            return _err(f"project repo missing: {repo_path}")
        throwaway = Path(tempfile.mkdtemp(prefix=f"oc-orch-gencleanup-{project.abbrev}-"))
        throwaway.rmdir()
        try:
            wt.create_worktree(repo_path, throwaway, branch=f"oc-orch-gencleanup-{project.abbrev}-{int(time.time())}", base=project.base_branch)
        except wt.GitError as e:
            shutil.rmtree(throwaway, ignore_errors=True)
            return _err(f"throwaway worktree create failed: {e}")
        try:
            try:
                rt.client.ensure_server(log_dir=rt.config.logs_dir)
            except OpencodeError as e:
                return _err(f"opencode server unavailable: {e}")
            result = await bootstrap_mod.generate_cleanup_skill(rt.client, project, throwaway, rt.projects)
            if not result.ok:
                return _err(f"generation failed: {result.detail}")
            updated = rt.projects.get(label)
            return _ok({
                "label": label,
                "method": result.method,
                "skill_detail": result.detail,
                "cleanup_skill": updated.cleanup_skill if updated else None,
                "bootstrap_skill_unchanged": True,
            })
        finally:
            wt.remove_worktree(repo_path, throwaway, force=True)
    return handler


def all_tool_specs(rt: Runtime) -> list[dict[str, Any]]:
    return [
        {"name": "oc_project_add", "toolset": "hermes_opencode", "schema": PROJECT_ADD_SCHEMA, "handler": make_project_add(rt), "is_async": True, "emoji": "📁"},
        {"name": "oc_project_list", "toolset": "hermes_opencode", "schema": PROJECT_LIST_SCHEMA, "handler": make_project_list(rt), "is_async": True, "emoji": "📋"},
        {"name": "oc_project_show", "toolset": "hermes_opencode", "schema": PROJECT_SHOW_SCHEMA, "handler": make_project_show(rt), "is_async": True, "emoji": "🔍"},
        {"name": "oc_project_remove", "toolset": "hermes_opencode", "schema": PROJECT_REMOVE_SCHEMA, "handler": make_project_remove(rt), "is_async": True, "emoji": "🗑️"},
        {"name": "oc_project_set_repo_path", "toolset": "hermes_opencode", "schema": PROJECT_SET_REPO_PATH_SCHEMA, "handler": make_project_set_repo_path(rt), "is_async": True, "emoji": "📍"},
        {"name": "oc_spawn", "toolset": "hermes_opencode", "schema": SPAWN_SCHEMA, "handler": make_spawn(rt), "is_async": True, "emoji": "🚀"},
        {"name": "oc_resume_pr", "toolset": "hermes_opencode", "schema": RESUME_PR_SCHEMA, "handler": make_resume_pr(rt), "is_async": True, "emoji": "🔁"},
        {"name": "oc_send", "toolset": "hermes_opencode", "schema": SEND_SCHEMA, "handler": make_send(rt), "is_async": True, "emoji": "💬"},
        {"name": "oc_status", "toolset": "hermes_opencode", "schema": STATUS_SCHEMA, "handler": make_status(rt), "is_async": True, "emoji": "📊"},
        {"name": "oc_wait", "toolset": "hermes_opencode", "schema": WAIT_SCHEMA, "handler": make_wait(rt), "is_async": True, "emoji": "⏳"},
        {"name": "oc_kill", "toolset": "hermes_opencode", "schema": KILL_SCHEMA, "handler": make_kill(rt), "is_async": True, "emoji": "🛑"},
        {"name": "oc_cancel", "toolset": "hermes_opencode", "schema": CANCEL_SCHEMA, "handler": make_cancel(rt), "is_async": True, "emoji": "🚫"},
        {"name": "oc_retry", "toolset": "hermes_opencode", "schema": RETRY_SCHEMA, "handler": make_retry(rt), "is_async": True, "emoji": "🔄"},
        {"name": "oc_output", "toolset": "hermes_opencode", "schema": OUTPUT_SCHEMA, "handler": make_output(rt), "is_async": True, "emoji": "📤"},
        {"name": "oc_answer", "toolset": "hermes_opencode", "schema": ANSWER_SCHEMA, "handler": make_answer(rt), "is_async": True, "emoji": "✉️"},
        {"name": "oc_review_now", "toolset": "hermes_opencode", "schema": REVIEW_NOW_SCHEMA, "handler": make_review_now(rt), "is_async": True, "emoji": "🔎"},
        {"name": "oc_review_again", "toolset": "hermes_opencode", "schema": REVIEW_AGAIN_SCHEMA, "handler": make_review_again(rt), "is_async": True, "emoji": "🔁"},
        {"name": "oc_skip_review", "toolset": "hermes_opencode", "schema": SKIP_REVIEW_SCHEMA, "handler": make_skip_review(rt), "is_async": True, "emoji": "⏭️"},
        {"name": "oc_pr_status", "toolset": "hermes_opencode", "schema": PR_STATUS_SCHEMA, "handler": make_pr_status(rt), "is_async": True, "emoji": "🔗"},
        {"name": "oc_project_regenerate_bootstrap", "toolset": "hermes_opencode", "schema": REGEN_BOOTSTRAP_SCHEMA, "handler": make_regen_bootstrap(rt), "is_async": True, "emoji": "🧰"},
        {"name": "oc_project_regenerate_cleanup", "toolset": "hermes_opencode", "schema": REGEN_CLEANUP_SCHEMA, "handler": make_regen_cleanup(rt), "is_async": True, "emoji": "🧹"},
        {"name": "oc_set_notify_target", "toolset": "hermes_opencode", "schema": SET_NOTIFY_TARGET_SCHEMA, "handler": make_set_notify_target(rt), "is_async": True, "emoji": "📡"},
        {"name": "oc_heartbeat_send_now", "toolset": "hermes_opencode", "schema": HEARTBEAT_NOW_SCHEMA, "handler": make_heartbeat_send_now(rt), "is_async": True, "emoji": "💓"},
    ]
