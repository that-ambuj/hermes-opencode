from __future__ import annotations

import logging
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
_READY_FOR_REVIEW_RE = re.compile(r"(?m)^\s*READY_FOR_REVIEW\s*$", re.IGNORECASE)
_COLLISION_SUFFIX_RE = re.compile(r"-\d+$")
_PR_OPENED_LINE_RE = re.compile(r"PR_OPENED:\s*(https?://[^\s]+/pull/(\d+))", re.IGNORECASE)
_PR_OPENED_VARIANT_RE = re.compile(
    r"\b(?:PR[ _-]*OPENED|OPENED[ _-]*PR|PR[ _-]*URL)\b[^h\n]{0,40}(https?://[^\s)]+/pull/(\d+))",
    re.IGNORECASE,
)
_PR_URL_FALLBACK_RE = re.compile(r"https?://github\.com/[^\s)]+/pull/(\d+)")

REVIEWER_AGENT_TYPE = "plan"

logger = logging.getLogger("hermes_opencode.reviewer")


def _pr_title_from_agent_id(agent_id: str) -> str:
    if "/" in agent_id:
        task_slug = agent_id.split("/", 1)[1]
    else:
        task_slug = agent_id
    task_slug = _COLLISION_SUFFIX_RE.sub("", task_slug)
    return task_slug.replace("-", " ").capitalize()


def reviewer_prompt(executor_initial_prompt: str, base_branch: str) -> str:
    return (
        "You are a strict code reviewer. A peer agent was given this task:\n\n"
        "<<<TASK\n"
        f"{executor_initial_prompt}\n"
        "TASK>>>\n\n"
        "Procedure (mandatory, in order):\n"
        f"  1. Run `git diff origin/{base_branch}...HEAD` and read EVERY changed hunk. If no remote, "
        f"     fall back to `git log --patch {base_branch}..HEAD`.\n"
        "  2. For each changed file, evaluate: correctness (does it actually solve the task), scope creep "
        "     (changes outside the task), missing tests, error-handling gaps, style fit with surrounding code.\n"
        "  3. Reject LGTM if ANY of these hold: untested behavior change, dead code, silently swallowed errors, "
        "     placeholder values, work that doesn't match the task above.\n\n"
        "Output format:\n"
        "  - If requesting changes: a numbered list of concrete change requests, each citing FILE + LINE + "
        "    the exact fix. Then emit the line: REVIEW: REQUESTS_CHANGES\n"
        "  - If everything passes the procedure above: emit the line: REVIEW: LGTM\n\n"
        "Emit exactly one of those two tokens, on its own line, at the very end."
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


def parse_ready_for_review(text: str) -> bool:
    if not text:
        return False
    return bool(_READY_FOR_REVIEW_RE.search(text))


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
        commit_res = wt._git(
            executor_worktree, "commit", "-m", f"chore: {cleaned}", check=False,
        )
        if commit_res.returncode != 0:
            raise wt.GitError(
                f"staging commit failed (cwd={executor_worktree}): "
                f"{(commit_res.stderr or commit_res.stdout).strip()}"
            )
    sister = reviewer_worktree_path(executor_worktree)
    if sister.exists():
        wt._git(repo_path, "worktree", "remove", "--force", str(sister), check=False)
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
        body=f"Initial task:\n\n{agent.initial_prompt}\n\n---\nopened by hermes-opencode for `{agent.agent_id}`",
    )


def executor_open_pr_prompt(branch: str, base_branch: str) -> str:
    return (
        "Your task is complete and code review has signed off. Open a pull request for your work.\n\n"
        "Steps (run them in your shell tool, in order):\n"
        "  1. If `git status --porcelain` shows any uncommitted or untracked changes, stage and commit them "
        "     with a single clear commit message that summarizes what you did. Use the user's existing git "
        "     identity; do NOT pass `-c user.email=...` or `-c user.name=...`. If a pre-review staging commit "
        "     with title `chore: <slug>` already exists at HEAD and you have nothing new to add, you MAY amend "
        "     it with `git commit --amend -m \"<your real commit message>\"` so the final history reflects "
        "     what actually changed.\n"
        f"  2. Push the branch: `git push -u origin {branch}` (use `--force-with-lease` if you amended).\n"
        f"  3. Open the PR with `gh pr create --base {base_branch} --title \"<your concise title>\" "
        "--body \"<your markdown summary>\"`. YOU write the title and body. The title should describe the "
        "change in <= 72 chars. The body should be a short markdown summary of what changed and why; "
        "reference notable files if helpful. Do NOT paste the original task prompt into the body. Do NOT "
        "use `--fill` (it would pull from the slug-based staging commit and produce garbage).\n"
        f"  4. If the PR already exists for branch `{branch}`, run `gh pr view --json url,number` to fetch "
        "     it instead of failing.\n\n"
        "After the PR exists, you MUST emit ONE line in this EXACT format on its own line:\n\n"
        "  PR_OPENED: https://github.com/<owner>/<repo>/pull/<number>\n\n"
        "Concrete example you can copy the shape of (substitute your real URL):\n"
        "  PR_OPENED: https://github.com/octocat/hello-world/pull/42\n\n"
        "The literal `PR_OPENED:` prefix is REQUIRED on its own line. Without it the orchestrator falls "
        "back to a generic slug-based title and pastes the original task prompt as the body, throwing away "
        "the title and body you authored. After emitting the line, stop.\n\n"
        "If anything in steps 1-4 fails, fix it and retry. Do not give up silently."
    )


def parse_pr_opened(text: str) -> tuple[str, int] | None:
    src = text or ""
    m = _PR_OPENED_LINE_RE.search(src)
    if m:
        return m.group(1), int(m.group(2))
    m = _PR_OPENED_VARIANT_RE.search(src)
    if m:
        return m.group(1), int(m.group(2))
    m = _PR_URL_FALLBACK_RE.search(src)
    if m:
        return m.group(0), int(m.group(1))
    return None


def parse_model_id(spec: str) -> dict[str, str] | None:
    """Parse a `provider/model[/variant]` string into the opencode create_session model struct.

    Returns None for empty / malformed input. The provider segment is the
    first path component, the model id is the second, and an optional third
    segment is treated as a variant.
    """
    if not spec or "/" not in spec:
        return None
    parts = [p.strip() for p in spec.split("/", 2) if p.strip()]
    if len(parts) < 2:
        return None
    provider_id, model_id = parts[0], parts[1]
    out: dict[str, str] = {"id": model_id, "providerID": provider_id}
    if len(parts) == 3:
        out["variant"] = parts[2]
    return out


def oneshot_open_pr_prompt(branch: str, base_branch: str, initial_prompt: str) -> str:
    return (
        "You are a one-shot PR-summary author. A peer agent did some work on this "
        f"branch `{branch}` but failed to author its own PR. Your job is to inspect "
        "the diff, author a concise title and a markdown body, then open the PR.\n\n"
        "Steps (run them in your shell tool, in order):\n"
        f"  1. Run `git diff origin/{base_branch}..HEAD --stat` and "
        f"`git diff origin/{base_branch}..HEAD` to see what changed.\n"
        "  2. Author a concise PR title (<= 72 chars) describing the change.\n"
        "  3. Author a short markdown body summarizing what changed and why; "
        "reference notable files if helpful. Do NOT paste the original task prompt "
        "verbatim into the body.\n"
        "  4. If `git status --porcelain` shows uncommitted changes (e.g. the "
        "pre-review `chore: <slug>` staging commit needs an amend), commit them "
        "under the user's git identity. You MAY use `git commit --amend -m "
        "\"<your real message>\"` to replace the slug placeholder.\n"
        f"  5. Push the branch: `git push -u origin {branch}` (use `--force-with-lease` "
        "if you amended).\n"
        f"  6. Open the PR: `gh pr create --base {base_branch} --title \"<your title>\" "
        "--body \"<your body>\"`. Do NOT use `--fill`.\n"
        f"  7. If the PR already exists for `{branch}`, fetch it with "
        "`gh pr view --json url,number` instead of failing.\n\n"
        "After the PR exists, you MUST emit ONE line in this EXACT format on its "
        "own line:\n\n"
        "  PR_OPENED: https://github.com/<owner>/<repo>/pull/<number>\n\n"
        "Concrete example: PR_OPENED: https://github.com/octocat/hello-world/pull/42\n\n"
        "The literal `PR_OPENED:` prefix is REQUIRED. Then stop.\n\n"
        "For context only (do NOT paste this into the PR body), the original task "
        "was:\n\n<<<TASK\n"
        f"{initial_prompt}\n"
        "TASK>>>"
    )


async def oneshot_open_pr(
    client: OpencodeClient,
    agent: Agent,
    base_branch: str,
    model_specs: list[str],
    *,
    timeout_sec: float = 600.0,
) -> tuple[pr.PrInfo | None, list[str]]:
    """Spawn a fresh opencode session in the executor's worktree, iterating
    through `model_specs` until one successfully emits `PR_OPENED:`.

    Returns `(info, attempts)`. `info` is the opened-PR info or None if all
    models exhausted. `attempts` lists every model spec tried + outcome (used
    for the FAILED last_error message and the audit log).
    """
    worktree = Path(agent.worktree_path)
    attempts: list[str] = []
    for spec in model_specs:
        model = parse_model_id(spec)
        if model is None:
            attempts.append(f"{spec}: invalid spec, skipped")
            logger.warning("oneshot_open_pr: skipping invalid model spec %r", spec)
            continue
        logger.info(
            "oneshot_open_pr: %s trying model %s (provider=%s id=%s variant=%s)",
            agent.agent_id, spec, model["providerID"], model["id"], model.get("variant", ""),
        )
        try:
            session = await client.create_session(worktree, agent="build", model=model)
        except OpencodeError as e:
            attempts.append(f"{spec}: create_session failed: {e}")
            logger.warning("oneshot_open_pr: %s create_session(%s) failed: %s", agent.agent_id, spec, e)
            continue
        session_id = session.get("id") or session.get("sessionID") or ""
        if not session_id:
            attempts.append(f"{spec}: create_session returned no id")
            continue
        prompt = oneshot_open_pr_prompt(agent.branch, base_branch, agent.initial_prompt)
        try:
            resp = await client.send_message(session_id, worktree, prompt, timeout=timeout_sec)
        except OpencodeError as e:
            attempts.append(f"{spec}: send_message failed: {e}")
            logger.warning("oneshot_open_pr: %s send_message(%s) failed: %s", agent.agent_id, spec, e)
            continue
        final_text = OpencodeClient.extract_assistant_text(resp) or ""
        logger.info(
            "oneshot_open_pr: %s model=%s response (%d chars, first 800: %s)",
            agent.agent_id, spec, len(final_text), final_text[:800],
        )
        parsed = parse_pr_opened(final_text)
        if not parsed:
            attempts.append(f"{spec}: no PR_OPENED sentinel in response")
            logger.warning(
                "oneshot_open_pr: %s model=%s emitted no PR_OPENED sentinel. Response (4KB):\n%s",
                agent.agent_id, spec, final_text[:4000],
            )
            continue
        url, number = parsed
        attempts.append(f"{spec}: ok PR #{number}")
        try:
            info = pr.pr_state(worktree, number)
            if info.url:
                return info, attempts
            return pr.PrInfo(number=number, url=url, state="OPEN", merged_at=None), attempts
        except pr.PrError:
            return pr.PrInfo(number=number, url=url, state="OPEN", merged_at=None), attempts
    return None, attempts


async def executor_open_pr(
    client: OpencodeClient, agent: Agent, base_branch: str, *, timeout_sec: float = 900.0,
) -> pr.PrInfo | None:
    worktree = Path(agent.worktree_path)
    prompt = executor_open_pr_prompt(agent.branch, base_branch)
    try:
        resp = await client.send_message(agent.session_id, worktree, prompt, timeout=timeout_sec)
    except OpencodeError as e:
        logger.warning("executor_open_pr: send_message failed for %s: %s", agent.agent_id, e)
        return None
    final_text = OpencodeClient.extract_assistant_text(resp) or ""
    logger.info(
        "executor_open_pr: %s response received (%d chars, first 800: %s)",
        agent.agent_id, len(final_text), final_text[:800],
    )
    parsed = parse_pr_opened(final_text)
    if not parsed:
        logger.warning(
            "executor_open_pr: %s emitted no recognizable PR URL (tried strict / variant / fallback "
            "regexes); falling back to slug-based finalize_and_open_pr. Full executor response "
            "(truncated to 4KB) follows:\n%s",
            agent.agent_id, final_text[:4000],
        )
        return None
    url, number = parsed
    logger.info("executor_open_pr: %s parsed PR url=%s number=%d", agent.agent_id, url, number)
    try:
        info = pr.pr_state(worktree, number)
        if info.url:
            return info
        return pr.PrInfo(number=number, url=url, state="OPEN", merged_at=None)
    except pr.PrError as e:
        logger.warning("executor_open_pr: pr_state(%s) failed: %s; using parsed url", number, e)
        return pr.PrInfo(number=number, url=url, state="OPEN", merged_at=None)
