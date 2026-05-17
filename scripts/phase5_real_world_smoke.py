#!/usr/bin/env python3
"""Phase 5 — real-world end-to-end smoke.

Drives the full executor -> reviewer -> commit -> PR_OPEN chain against a real
sandbox git repo with `gh` CLI configured. Polls agent phase every 10s and
prints transitions. Cleans up on exit: closes PR, deletes branch, removes
worktree, unregisters project.

Usage:
    OC_SMOKE_PORT=4099 SANDBOX_REPO=/tmp/oc-orch-smoke-sandbox \\
    /path/to/uv run --quiet phase5_real_world_smoke.py
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "httpx-sse", "PyYAML"]
# ///
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "_oc_orch_smoke", PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = "_oc_orch_smoke"
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["_oc_orch_smoke"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def port_open(host, port, t=0.3):
    try:
        with socket.create_connection((host, port), timeout=t):
            return True
    except OSError:
        return False


def spawn_opencode(port):
    proc = subprocess.Popen(
        ["opencode", "serve", "--hostname=127.0.0.1", f"--port={port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(75):
        if port_open("127.0.0.1", port):
            return proc
        time.sleep(0.2)
    proc.terminate()
    raise SystemExit("opencode didn't come up")


def log(msg):
    sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


async def call(spec, args):
    raw = await spec["handler"](args)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw}


async def run(opencode_port, sandbox_repo: Path):
    tmp_home = Path(tempfile.mkdtemp(prefix="oc-orch-p5-"))
    os.environ["HERMES_HOME"] = str(tmp_home)
    log(f"HERMES_HOME={tmp_home}")
    log(f"sandbox={sandbox_repo}")

    pkg = load_plugin()
    from _oc_orch_smoke.config import Config
    from _oc_orch_smoke.projects import ProjectRegistry
    from _oc_orch_smoke.state import AgentStore
    from _oc_orch_smoke.tools import Runtime, all_tool_specs
    from _oc_orch_smoke.transport import OpencodeClient

    cfg = Config.from_plugin_entry({
        "opencode_server": {"url": f"http://127.0.0.1:{opencode_port}"},
        "auto_spawn_server": False,
    })
    cfg.ensure_dirs()
    client = OpencodeClient(cfg.server_url, cfg.server_password)
    projects = ProjectRegistry(cfg.projects_file)
    agents = AgentStore(cfg.agents_file)
    rt = Runtime(config=cfg, client=client, projects=projects, agents=agents)
    specs = {s["name"]: s for s in all_tool_specs(rt)}
    log(f"loaded {len(specs)} tools")

    agent_id = None
    pr_number = None
    project_label = "oco-smoke"
    try:
        log("oc_project_add")
        r = await call(specs["oc_project_add"], {
            "label": project_label, "repo_path": str(sandbox_repo), "base_branch": "main",
        })
        if not r.get("ok"):
            log(f"FAIL project_add: {r.get('error')}")
            return 1

        log("oc_spawn (tiny task)")
        prompt = (
            "Create a file named `phase5-marker.md` in this worktree with exactly "
            "this single-line content: `opencode-orchestrator phase-5 smoke marker`. "
            "Do not commit. Do not modify any other file. When done, briefly say "
            "'marker created'."
        )
        r = await call(specs["oc_spawn"], {
            "project": project_label, "task": "phase5-marker",
            "prompt": prompt, "agent": "build",
        })
        if not r.get("ok"):
            log(f"FAIL spawn: {r.get('error')}")
            return 1
        agent_id = r["data"]["agent_id"]
        log(f"agent_id={agent_id}  session={r['data']['session_id'][:16]}")
        log(f"first turn finish={r['data'].get('first_turn_finish')!r}")
        log(f"opencode resolved agent={r['data'].get('opencode_agent_resolved')!r}")

        log("watching state machine (poll every 10s, cap 30 min)")
        last_phase = "EXECUTING"
        deadline = time.time() + 30 * 60
        while time.time() < deadline:
            await asyncio.sleep(10)
            r = await call(specs["oc_status"], {"agent_id": agent_id})
            if not r.get("ok"):
                log(f"status error: {r.get('error')}")
                continue
            phase = r["data"]["phase"]
            if phase != last_phase:
                pq = len(r["data"].get("pending_questions") or [])
                pp = len(r["data"].get("pending_permissions") or [])
                pr_url = r["data"].get("pr_url") or "-"
                log(f"phase: {last_phase} -> {phase}  q={pq} p={pp} pr={pr_url}")
                last_phase = phase
                if phase == "PR_OPEN":
                    pr_number = r["data"].get("pr_number")
                    log(f"PR_OPEN reached! pr_number={pr_number}")
                    return 0
                if phase in {"DONE", "FAILED", "KILLED"}:
                    if phase == "DONE":
                        return 0
                    log(f"terminal phase {phase}, error={r['data'].get('last_error')}")
                    return 1
        log("timed out at 30 min")
        return 1
    finally:
        log("--- cleanup ---")
        if pr_number:
            try:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_number), "--delete-branch",
                     "--comment", "smoke test cleanup", "--repo", "that-ambuj/opencode-orchestrator-smoke"],
                    cwd=sandbox_repo, capture_output=True, text=True, timeout=30, check=False,
                )
                log(f"closed PR #{pr_number}")
            except Exception as e:
                log(f"pr close failed: {e}")
        if agent_id:
            r = await call(specs["oc_kill"], {"agent_id": agent_id, "remove_worktree": True})
            log(f"kill: ok={r.get('ok')} errors={r.get('data', {}).get('errors')}")
        try:
            await call(specs["oc_project_remove"], {"label": project_label})
        except Exception:
            pass
        shutil.rmtree(tmp_home, ignore_errors=True)


def main():
    port = int(os.environ.get("OC_SMOKE_PORT", "4099"))
    sandbox = Path(os.environ["SANDBOX_REPO"]).resolve()
    if not (sandbox / ".git").exists():
        raise SystemExit(f"not a git repo: {sandbox}")

    spawned = None
    if not port_open("127.0.0.1", port):
        log(f"spawning opencode serve on :{port}")
        spawned = spawn_opencode(port)
    else:
        log(f"attaching to existing opencode on :{port}")

    try:
        code = asyncio.run(run(port, sandbox))
    finally:
        if spawned and spawned.poll() is None:
            spawned.terminate()
            try:
                spawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spawned.kill()
    sys.exit(code)


if __name__ == "__main__":
    main()
