from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import pr
from . import worktree as wt
from .projects import Project, ProjectRegistry
from .state import Agent, AgentStore
from .transport import OpencodeClient, OpencodeError


_LGTM_RE = re.compile(r"\bREVIEW\s*:\s*LGTM\b", re.IGNORECASE)
_REQUEST_RE = re.compile(r"\bREVIEW\s*:\s*REQUESTS?_CHANGES\b", re.IGNORECASE)
_COLLISION_SUFFIX_RE = re.compile(r"-\d+$")

REVIEWER_AGENT_TYPE = "plan"


def _pr_title_from_agent_id(agent_id: str) -> str:
    if "/" in agent_id:
        task_slug = agent_id.split("/", 1)[1]
    else:
        task_slug = agent_id
    task_slug = _COLLISION_SUFFIX_RE.sub("", task_slug)
    return task_slug.replace("-", " ").capitalize()


def reviewer_prompt(executor_initial_prompt: str, base_branch: str) -> str:
    return (
        "You are a code reviewer. A peer agent was given this task:\n\n"
        "«««\n"
        f"{executor_initial_prompt}\n"
        "»»»\n\n"
        f"Their work is the diff on this worktree's current branch vs origin/{base_branch} (or local HEAD if no remote). "
        "Read the diff (`git diff` from your shell, or `git log`), then assess: correctness, scope creep, "
        "missing tests, style fit.\n\n"
        "Emit one of these tokens on its own line at the end:\n"
        "  REVIEW: LGTM             — if no changes needed\n"
        "  REVIEW: REQUESTS_CHANGES — if you have feedback\n\n"
        "If you emit REQUESTS_CHANGES, list the change requests above the token as a numbered list. "
        "Be specific (file + line + what to do)."
    )


def addressing_prompt(review_text: str) -> str:
    return (
        "Address the following reviewer feedback. Make the changes in this worktree, then say `addressed.`\n\n"
        "«««\n"
        f"{review_text}\n"
        "»»»"
    )


@dataclass
class ReviewVerdict:
    kind: str
    body: str


def classify_review(text: str) -> ReviewVerdict:
    if _LGTM_RE.search(text):
        return ReviewVerdict(kind="lgtm", body=text)
    if _REQUEST_RE.search(text):
        return ReviewVerdict(kind="requests_changes", body=text)
    return ReviewVerdict(kind="ambiguous", body=text)


def reviewer_worktree_path(executor_worktree: Path) -> Path:
    return executor_worktree.parent / (executor_worktree.name + ".review")


def stage_reviewer_worktree(
    project: Project, executor: Agent, executor_worktree: Path,
) -> Path:
    repo_path = Path(project.repo_path)
    if not (repo_path / ".git").exists():
        raise wt.GitError(f"project repo missing: {repo_path}")
    if wt.GitError and not executor_worktree.exists():
        raise wt.GitError(f"executor worktree missing: {executor_worktree}")
    if wt._git(executor_worktree, "status", "--porcelain", check=False).stdout.strip():
        cleaned = _pr_title_from_agent_id(executor.agent_id)
        wt._git(executor_worktree, "add", "-A")
        wt._git(
            executor_worktree, "-c", "user.email=opencode-orchestrator@local",
            "-c", "user.name=opencode-orchestrator",
            "commit", "-m", f"chore: {cleaned}",
        )
    sister = reviewer_worktree_path(executor_worktree)
    if sister.exists():
        try:
            wt._git(repo_path, "worktree", "remove", "--force", str(sister), check=False)
        except Exception:
            shutil.rmtree(sister, ignore_errors=True)
    wt._git(repo_path, "worktree", "add", "--detach", str(sister), executor.branch)
    return sister


def teardown_reviewer_worktree(project: Project, executor_worktree: Path) -> None:
    sister = reviewer_worktree_path(executor_worktree)
    wt.remove_worktree(Path(project.repo_path), sister, force=True)


async def spawn_reviewer_session(
    client: OpencodeClient, sister_worktree: Path, agent: Agent, base_branch: str,
) -> tuple[str, str]:
    session = await client.create_session(sister_worktree, agent=REVIEWER_AGENT_TYPE)
    session_id = session.get("id") or session.get("sessionID") or ""
    if not session_id:
        raise OpencodeError("reviewer session create returned no id")
    prompt = reviewer_prompt(agent.initial_prompt, base_branch)
    resp = await client.send_message(session_id, sister_worktree, prompt)
    final_text = OpencodeClient.extract_assistant_text(resp)
    return session_id, final_text


async def send_addressing_to_executor(
    client: OpencodeClient, agent: Agent, review_text: str,
) -> str:
    worktree = Path(agent.worktree_path)
    resp = await client.send_message(
        agent.session_id, worktree, addressing_prompt(review_text), timeout=900,
    )
    return OpencodeClient.extract_assistant_text(resp)


async def finalize_and_open_pr(
    project: Project, agent: Agent, base_branch: str, title: str | None = None,
) -> pr.PrInfo:
    worktree = Path(agent.worktree_path)
    cleaned_title = title or _pr_title_from_agent_id(agent.agent_id)
    pr.commit_and_push(worktree, cleaned_title, agent.branch)
    return pr.open_pr(
        worktree, base_branch=base_branch, title=cleaned_title,
        body=f"Initial task:\n\n{agent.initial_prompt}\n\n---\nopened by opencode-orchestrator for `{agent.agent_id}`",
    )
