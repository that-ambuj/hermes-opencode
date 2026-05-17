from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class PrError(RuntimeError):
    pass


@dataclass
class PrInfo:
    number: int
    url: str
    state: str
    merged_at: float | None


_PR_URL_RE = re.compile(r"https?://[^\s]+/pull/(\d+)")


def _gh() -> str:
    gh = shutil.which("gh")
    if not gh:
        raise PrError("gh CLI not found on PATH")
    return gh


def _git(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=worktree, capture_output=True, text=True,
        check=check, timeout=120,
    )


def has_changes(worktree: Path) -> bool:
    res = _git(worktree, "status", "--porcelain", check=False)
    return bool(res.stdout.strip())


def commit_and_push(worktree: Path, message: str, branch: str) -> None:
    if has_changes(worktree):
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", message)
    push = _git(worktree, "push", "-u", "origin", branch, check=False)
    if push.returncode != 0:
        raise PrError(f"git push failed: {push.stderr.strip() or push.stdout.strip()}")


def _existing_pr_from_output(stdout: str, stderr: str) -> tuple[str, int] | None:
    combined = f"{stderr}\n{stdout}"
    if "already exists" not in combined.lower():
        return None
    url_match = _PR_URL_RE.search(combined)
    if not url_match:
        return None
    return url_match.group(0), int(url_match.group(1))


def open_pr(
    worktree: Path, base_branch: str, title: str | None = None,
    body: str | None = None,
) -> PrInfo:
    args = [_gh(), "pr", "create", "--base", base_branch]
    if title:
        args += ["--title", title]
        args += ["--body", body or ""]
    else:
        args += ["--fill"]
    res = subprocess.run(args, cwd=worktree, capture_output=True, text=True, timeout=120, check=False)
    if res.returncode != 0:
        existing = _existing_pr_from_output(res.stdout or "", res.stderr or "")
        if existing is None:
            raise PrError(f"gh pr create failed: {res.stderr.strip() or res.stdout.strip()}")
        url, number = existing
        try:
            info = pr_state(worktree, number)
        except PrError:
            return PrInfo(number=number, url=url, state="OPEN", merged_at=None)
        return PrInfo(number=number, url=url, state=info.state, merged_at=info.merged_at)
    url_match = _PR_URL_RE.search(res.stdout or "")
    if not url_match:
        raise PrError(f"could not parse PR URL from gh output: {res.stdout!r}")
    url = url_match.group(0)
    number = int(url_match.group(1))
    info = pr_state(worktree, number)
    return PrInfo(number=number, url=url, state=info.state, merged_at=info.merged_at)


def pr_state(worktree: Path, number: int) -> PrInfo:
    res = subprocess.run(
        [_gh(), "pr", "view", str(number), "--json", "number,url,state,mergedAt"],
        cwd=worktree, capture_output=True, text=True, timeout=30, check=False,
    )
    if res.returncode != 0:
        raise PrError(f"gh pr view {number} failed: {res.stderr.strip() or res.stdout.strip()}")
    data = json.loads(res.stdout or "{}")
    merged_iso = data.get("mergedAt")
    merged_at: float | None = None
    if merged_iso:
        try:
            from datetime import datetime
            merged_at = datetime.fromisoformat(merged_iso.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            merged_at = time.time()
    return PrInfo(
        number=int(data.get("number") or number),
        url=str(data.get("url") or ""),
        state=str(data.get("state") or "UNKNOWN"),
        merged_at=merged_at,
    )
