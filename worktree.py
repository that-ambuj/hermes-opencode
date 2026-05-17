from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_ABBREV_RE = re.compile(r"[^a-z0-9]")
AGENT_ID_MAX = 20


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check, timeout=120,
    )


def remote_url(repo_path: Path) -> str | None:
    try:
        result = _git(repo_path, "config", "--get", "remote.origin.url", check=False)
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def project_key_for(repo_path: Path) -> str:
    url = remote_url(repo_path)
    if url:
        return "proj_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    digest = hashlib.sha256(str(repo_path.resolve()).encode()).hexdigest()[:12]
    return "proj_local_" + digest


def derive_abbrev(label: str) -> str:
    parts = [p for p in label.split("-") if p]
    raw = "".join(p[0] for p in parts) if len(parts) > 1 else label[:3]
    abbrev = _ABBREV_RE.sub("", raw.lower())[:5]
    if len(abbrev) < 2:
        abbrev = _ABBREV_RE.sub("", (label[:3] or "agt").lower())[:5]
    if len(abbrev) < 2:
        abbrev = "agt"
    return abbrev


def slugify(text: str, max_len: int) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not s:
        return "task"
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    last_hyphen = cut.rfind("-")
    if last_hyphen > max_len // 2:
        cut = cut[:last_hyphen]
    return cut.strip("-") or s[:max_len].strip("-") or "task"


def compose_agent_id(abbrev: str, task: str, existing: set[str]) -> str:
    base_task_max = AGENT_ID_MAX - len(abbrev) - 1
    if base_task_max < 1:
        raise ValueError(f"abbrev {abbrev!r} too long; leaves no room for task slug")
    task_slug = slugify(task, base_task_max)
    candidate = f"{abbrev}/{task_slug}"
    if candidate not in existing:
        return candidate
    for n in range(2, 100):
        suffix = f"-{n}"
        task_max = AGENT_ID_MAX - len(abbrev) - 1 - len(suffix)
        if task_max < 1:
            raise ValueError(f"abbrev {abbrev!r} too long for collision suffixes")
        trimmed = slugify(task, task_max)
        candidate = f"{abbrev}/{trimmed}{suffix}"
        if candidate not in existing:
            return candidate
    raise ValueError(f"too many collisions for {abbrev}/{task}")


def agent_id_to_fs(agent_id: str) -> str:
    return agent_id.replace("/", "__")


def branch_exists(repo_path: Path, branch: str) -> bool:
    result = _git(repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return result.returncode == 0


def create_worktree(repo_path: Path, target: Path, branch: str, base: str = "main") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise GitError(f"worktree target already exists: {target}")
    try:
        if branch_exists(repo_path, branch):
            _git(repo_path, "worktree", "add", str(target), branch)
        else:
            _git(repo_path, "worktree", "add", "-b", branch, str(target), base)
    except subprocess.CalledProcessError as e:
        raise GitError(f"git worktree add failed: {e.stderr.strip() or e.stdout.strip()}") from e


def remove_worktree(repo_path: Path, worktree_path: Path, force: bool = True) -> None:
    if not worktree_path.exists():
        try:
            _git(repo_path, "worktree", "prune", check=False)
        except (subprocess.SubprocessError, OSError):
            pass
        return
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    try:
        _git(repo_path, *args, check=False)
    except (subprocess.SubprocessError, OSError):
        pass
