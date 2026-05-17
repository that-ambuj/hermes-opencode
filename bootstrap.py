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
_CLEANUP_BEGIN = "CLEANUP_BEGIN"
_CLEANUP_END = "CLEANUP_END"
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
        f"Inspect this repository and produce TWO scripts: an idempotent bootstrap that "
        f"prepares a fresh git worktree for development, and a matching cleanup that "
        f"INVERSES the bootstrap's side effects when a worktree is torn down. Look at "
        f"README, package.json, pyproject.toml, docker-compose.yml, Makefile, .tool-versions, "
        f".nvmrc, requirements*.txt, Gemfile, Cargo.toml, go.mod, etc.\n\n"
        f"Output:\n"
        f"  1. A line containing only {_FENCE_BEGIN}, then a self-contained bash block (no "
        f"     language tag, no fences inside) that prepares the worktree, then a line "
        f"     containing only {_FENCE_END}.\n"
        f"  2. A line containing only {_CLEANUP_BEGIN}, then a bash block that inverses the "
        f"     bootstrap (stop docker compose services started by bootstrap, drop ephemeral "
        f"     databases, remove generated .env files, etc.), then a line containing only "
        f"     {_CLEANUP_END}. If the bootstrap has no persistent side effects worth "
        f"     reversing, emit an empty bash block (just whitespace between the markers).\n"
        f"  3. {_RECOVERED_MARKER} on its own line, indicating you finished.\n\n"
        f"Both scripts run with cwd=<worktree>. Be idempotent in both. If you must ask the "
        f"user for secrets (DB credentials, API keys, choice of optional local services), "
        f"use your normal question mechanism — do not embed placeholders that require "
        f"manual editing."
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
    bootstrap_bash = fence_match.group(1).strip()
    if not bootstrap_bash:
        return BootstrapResult(ok=False, method="opencode", detail="empty bash block in output")

    cleanup_match = re.search(rf"{_CLEANUP_BEGIN}\s*\n(.*?)\n{_CLEANUP_END}", final_text, re.DOTALL)
    cleanup_bash = cleanup_match.group(1).strip() if cleanup_match else ""

    bootstrap_skill_name = f"hermes-opencode:{project.abbrev}-bootstrap"
    cleanup_skill_name = f"hermes-opencode:{project.abbrev}-cleanup"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    _write_project_skill(
        project, bootstrap_skill_name, "bootstrap",
        f"Bootstrap worktree for {project.label}.",
        bootstrap_bash, timestamp,
    )
    if cleanup_bash:
        _write_project_skill(
            project, cleanup_skill_name, "cleanup",
            f"Tear down a worktree of {project.label} (inverses {project.abbrev}-bootstrap).",
            cleanup_bash, timestamp,
        )
        registry.update(
            project.label,
            bootstrap_skill=bootstrap_skill_name,
            cleanup_skill=cleanup_skill_name,
        )
    else:
        registry.update(project.label, bootstrap_skill=bootstrap_skill_name)

    try:
        await client.delete_session(sid, throwaway_worktree)
    except OpencodeError:
        pass
    detail = bootstrap_skill_name + (f" + {cleanup_skill_name}" if cleanup_bash else " (no cleanup needed)")
    return BootstrapResult(ok=True, method="opencode", skill_updated=True, detail=detail)


def _write_project_skill(
    project: Project, qualified_name: str, kind: str,
    description: str, bash: str, timestamp: str,
) -> Path:
    slug = qualified_name.replace(":", "__")
    skill_dir = hermes_home() / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    title_cap = kind.capitalize()
    skill_path.write_text(
        f"---\nname: {project.abbrev}-{kind}\n"
        f"description: \"{description}\"\nversion: 1.0.0\n"
        f"author: hermes-opencode\ncreated_at: {timestamp}\n---\n\n"
        f"# {title_cap} {project.label}\n\n"
        f"Generated by hermes-opencode for project `{project.label}`. "
        f"Edit the bash block below to refine.\n\n"
        f"```bash\n{bash}\n```\n",
        encoding="utf-8",
    )
    return skill_path


async def generate_cleanup_skill(
    client: OpencodeClient, project: Project, throwaway_worktree: Path, registry: ProjectRegistry,
    *, agent: str = "build", timeout_sec: float = 600.0,
) -> BootstrapResult:
    bootstrap_md = ""
    if project.bootstrap_skill:
        bp = _resolve_skill_path(project.bootstrap_skill)
        if bp:
            try:
                bootstrap_md = bp.read_text(encoding="utf-8")
            except OSError:
                bootstrap_md = ""

    bootstrap_ref = (
        f"Existing bootstrap skill:\n{bootstrap_md}"
        if bootstrap_md
        else "No bootstrap skill on file. Infer the right tear-down from the repo itself."
    )
    prompt = (
        "Generate ONLY a cleanup script that INVERSES this project's bootstrap. Read the repo "
        "(docker-compose.yml, scripts/, Makefile, etc.) and the existing bootstrap (if any) to "
        "decide the right tear-down.\n\n"
        f"{bootstrap_ref}\n\n"
        f"Output:\n"
        f"  1. A line containing only {_CLEANUP_BEGIN}, then a self-contained bash block (no "
        f"     language tag, no fences inside) that runs idempotently and reverses the bootstrap "
        f"     (stop docker compose services started by bootstrap, drop ephemeral databases, "
        f"     remove generated .env files, etc.), then a line containing only {_CLEANUP_END}.\n"
        f"  2. {_RECOVERED_MARKER} on its own line, indicating you finished.\n\n"
        f"The script runs with cwd=<worktree>. If the bootstrap has no persistent side effects "
        f"worth reversing, emit an empty bash block (just whitespace between the markers)."
    )
    try:
        session = await client.create_session(throwaway_worktree, agent=agent)
    except OpencodeError as e:
        return BootstrapResult(ok=False, method="opencode", detail=f"cleanup session create failed: {e}")
    sid = session.get("id") or session.get("sessionID") or ""
    try:
        resp = await client.send_message(sid, throwaway_worktree, prompt, timeout=timeout_sec)
    except OpencodeError as e:
        return BootstrapResult(ok=False, method="opencode", detail=f"cleanup send failed: {e}")
    try:
        await client.wait_idle(sid, throwaway_worktree, timeout=timeout_sec)
    except OpencodeError:
        pass

    final_text = OpencodeClient.extract_assistant_text(resp)
    try:
        await client.delete_session(sid, throwaway_worktree)
    except OpencodeError:
        pass

    cleanup_match = re.search(rf"{_CLEANUP_BEGIN}\s*\n(.*?)\n{_CLEANUP_END}", final_text, re.DOTALL)
    if not cleanup_match:
        return BootstrapResult(ok=False, method="opencode", detail=f"no {_CLEANUP_BEGIN}/{_CLEANUP_END} block in output")
    cleanup_bash = cleanup_match.group(1).strip()

    cleanup_skill_name = f"hermes-opencode:{project.abbrev}-cleanup"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    if not cleanup_bash:
        registry.update(project.label, cleanup_skill=cleanup_skill_name)
        _write_project_skill(
            project, cleanup_skill_name, "cleanup",
            f"Tear down a worktree of {project.label} (no-op: bootstrap has no persistent side effects).",
            "", timestamp,
        )
        return BootstrapResult(ok=True, method="opencode", skill_updated=True, detail=f"{cleanup_skill_name} (no-op)")

    _write_project_skill(
        project, cleanup_skill_name, "cleanup",
        f"Tear down a worktree of {project.label} (inverses {project.abbrev}-bootstrap).",
        cleanup_bash, timestamp,
    )
    registry.update(project.label, cleanup_skill=cleanup_skill_name)
    return BootstrapResult(ok=True, method="opencode", skill_updated=True, detail=cleanup_skill_name)


async def run_project_cleanup(
    client: OpencodeClient, project: Project, worktree: Path,
    *, timeout_sec: float = 300.0,
) -> BootstrapResult:
    if not project.cleanup_skill:
        return BootstrapResult(ok=True, method="skip", detail="no cleanup_skill configured")
    skill_path = _resolve_skill_path(project.cleanup_skill)
    if not skill_path:
        return BootstrapResult(ok=False, method="skip", detail=f"cleanup skill not found: {project.cleanup_skill}")
    skill_md = skill_path.read_text(encoding="utf-8")
    script = _extract_bash(skill_md)
    if not script:
        return BootstrapResult(ok=True, method="skip", detail="cleanup skill has no bash block (no-op)")
    try:
        result = _run_bash_in_worktree(script, worktree, timeout_sec=timeout_sec)
    except subprocess.TimeoutExpired:
        return BootstrapResult(ok=False, method="shell", detail="cleanup timed out")
    if result.returncode == 0:
        return BootstrapResult(ok=True, method="shell", detail="cleanup exit 0")
    tail = (result.stderr or result.stdout or "")[-2048:]
    return BootstrapResult(ok=False, method="shell", detail=f"cleanup exit {result.returncode}: {tail}")
