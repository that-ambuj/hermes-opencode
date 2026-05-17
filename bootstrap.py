from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import hermes_home
from .projects import Project, ProjectRegistry
from .transport import OpencodeClient, OpencodeError


_BASH_BLOCK_RE = re.compile(r"```bash\s*\n(.*?)\n```", re.DOTALL)
_FENCE_BEGIN = "BOOTSTRAP_BEGIN"
_FENCE_END = "BOOTSTRAP_END"
_RECOVERED_MARKER = "BOOTSTRAP_RECOVERED"


@dataclass
class BootstrapResult:
    ok: bool
    method: str
    stderr_tail: str = ""
    skill_updated: bool = False
    detail: str = ""


def _resolve_skill_path(qualified_name: str) -> Path | None:
    base = hermes_home() / "skills"
    candidate = base / qualified_name / "SKILL.md"
    if candidate.exists():
        return candidate
    sanitized = qualified_name.replace(":", "__").replace("/", "__")
    candidate = base / sanitized / "SKILL.md"
    if candidate.exists():
        return candidate
    return None


def _extract_bash(skill_md: str) -> str | None:
    match = _BASH_BLOCK_RE.search(skill_md)
    if not match:
        return None
    return match.group(1).strip() or None


def _run_bash_in_worktree(script: str, worktree: Path, timeout_sec: float = 600.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PWD"] = str(worktree)
    return subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", script],
        cwd=worktree, capture_output=True, text=True, env=env,
        timeout=timeout_sec, check=False,
    )


async def run_project_bootstrap(
    client: OpencodeClient, project: Project, worktree: Path,
    *, recovery_agent: str = "build", timeout_sec: float = 600.0,
) -> BootstrapResult:
    if not project.bootstrap_skill:
        return BootstrapResult(ok=True, method="skip", detail="no bootstrap_skill configured")

    skill_path = _resolve_skill_path(project.bootstrap_skill)
    if not skill_path:
        return BootstrapResult(ok=False, method="skip", detail=f"bootstrap skill not found: {project.bootstrap_skill}")

    skill_md = skill_path.read_text(encoding="utf-8")
    script = _extract_bash(skill_md)
    if not script:
        return BootstrapResult(ok=False, method="skip", detail="no ```bash``` block in skill")

    try:
        result = _run_bash_in_worktree(script, worktree, timeout_sec=timeout_sec)
    except subprocess.TimeoutExpired:
        return await _opencode_recover(
            client, worktree, skill_md, script, "(timeout)",
            agent=recovery_agent, timeout_sec=timeout_sec,
        )

    if result.returncode == 0:
        return BootstrapResult(ok=True, method="shell", detail=f"exit 0 in {worktree}")

    stderr_tail = (result.stderr or result.stdout or "")[-4096:]
    return await _opencode_recover(
        client, worktree, skill_md, script, stderr_tail,
        agent=recovery_agent, timeout_sec=timeout_sec,
    )


async def _opencode_recover(
    client: OpencodeClient, worktree: Path, skill_md: str, failing_script: str,
    stderr_tail: str, *, agent: str, timeout_sec: float,
) -> BootstrapResult:
    prompt = (
        f"Bootstrap script failed for this worktree. Diagnose and fix the worktree so "
        f"a developer can start work.\n\n"
        f"Failing script:\n```bash\n{failing_script}\n```\n\n"
        f"stderr tail:\n```\n{stderr_tail}\n```\n\n"
        f"When the worktree is ready, emit a line containing exactly: {_RECOVERED_MARKER}\n"
        f"If you changed the bootstrap procedure, also emit an updated bash block delimited by "
        f"{_FENCE_BEGIN} and {_FENCE_END} on their own lines. Otherwise just emit {_RECOVERED_MARKER}.\n\n"
        f"Current skill content for reference:\n{skill_md}"
    )
    try:
        session = await client.create_session(worktree, agent=agent)
    except OpencodeError as e:
        return BootstrapResult(ok=False, method="opencode", detail=f"recovery session create failed: {e}")
    sid = session.get("id") or session.get("sessionID") or ""
    if not sid:
        return BootstrapResult(ok=False, method="opencode", detail="recovery session id missing")
    try:
        resp = await client.send_message(sid, worktree, prompt, timeout=timeout_sec)
    except OpencodeError as e:
        return BootstrapResult(ok=False, method="opencode", detail=f"recovery message failed: {e}")
    try:
        await client.wait_idle(sid, worktree, timeout=timeout_sec)
    except OpencodeError:
        pass

    final_text = OpencodeClient.extract_assistant_text(resp)
    skill_updated = False
    if _FENCE_BEGIN in final_text and _FENCE_END in final_text:
        skill_updated = True

    ok = _RECOVERED_MARKER in final_text
    try:
        await client.delete_session(sid, worktree)
    except OpencodeError:
        pass

    return BootstrapResult(
        ok=ok,
        method="opencode",
        skill_updated=skill_updated,
        detail=final_text[:1024],
    )


async def generate_bootstrap_skill(
    client: OpencodeClient, project: Project, throwaway_worktree: Path, registry: ProjectRegistry,
    *, agent: str = "build", timeout_sec: float = 900.0,
) -> BootstrapResult:
    prompt = (
        f"Inspect this repository and produce an idempotent bootstrap script that prepares a "
        f"fresh git worktree for development. Look at README, package.json, pyproject.toml, "
        f"docker-compose.yml, Makefile, .tool-versions, .nvmrc, requirements*.txt, Gemfile, "
        f"Cargo.toml, go.mod — whatever applies. Output exactly one bash block delimited by "
        f"lines containing only {_FENCE_BEGIN} and {_FENCE_END}. Inside the block, write a "
        f"single self-contained bash script (no language tag, no fences) that runs in the "
        f"worktree's directory and prepares it for development. Be idempotent. If you must ask "
        f"the user for secrets (DB credentials, API keys, choice of optional local services), "
        f"use your normal question mechanism — do not embed placeholders that require manual "
        f"editing. After the bash block, emit {_RECOVERED_MARKER} on its own line."
    )
    try:
        session = await client.create_session(throwaway_worktree, agent=agent)
    except OpencodeError as e:
        return BootstrapResult(ok=False, method="opencode", detail=f"generate session create failed: {e}")
    sid = session.get("id") or session.get("sessionID") or ""
    try:
        resp = await client.send_message(sid, throwaway_worktree, prompt, timeout=timeout_sec)
    except OpencodeError as e:
        return BootstrapResult(ok=False, method="opencode", detail=f"generate send failed: {e}")
    try:
        await client.wait_idle(sid, throwaway_worktree, timeout=timeout_sec)
    except OpencodeError:
        pass

    final_text = OpencodeClient.extract_assistant_text(resp)
    fence_match = re.search(rf"{_FENCE_BEGIN}\s*\n(.*?)\n{_FENCE_END}", final_text, re.DOTALL)
    if not fence_match:
        try:
            await client.delete_session(sid, throwaway_worktree)
        except OpencodeError:
            pass
        return BootstrapResult(ok=False, method="opencode", detail=f"no {_FENCE_BEGIN}/{_FENCE_END} block in output")
    bash = fence_match.group(1).strip()
    if not bash:
        return BootstrapResult(ok=False, method="opencode", detail="empty bash block in output")

    skill_dir = hermes_home() / "skills" / project.bootstrap_skill if project.bootstrap_skill else None
    if skill_dir is None:
        slug = f"hermes-opencode__{project.abbrev}-bootstrap"
        skill_dir = hermes_home() / "skills" / slug
        registry.update(project.label, bootstrap_skill=f"hermes-opencode:{project.abbrev}-bootstrap")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        f"---\nname: {project.abbrev}-bootstrap\ndescription: \"Bootstrap worktree for {project.label}.\"\nversion: 1.0.0\nauthor: hermes-opencode\ncreated_at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n---\n\n# Bootstrap {project.label}\n\nIdempotent worktree setup script generated by inspecting the repo.\n\n```bash\n{bash}\n```\n",
        encoding="utf-8",
    )
    try:
        await client.delete_session(sid, throwaway_worktree)
    except OpencodeError:
        pass
    return BootstrapResult(ok=True, method="opencode", skill_updated=True, detail=str(skill_path))
