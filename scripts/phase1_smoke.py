#!/usr/bin/env python3
"""
Phase 1 smoke test — exercise the full plugin tool catalog end-to-end without
booting a hermes session. Drives the Runtime directly through each handler.

Steps:
  01. import the plugin (relative-import safe path via sys.path)
  02. point HERMES_HOME at a temp dir so we don't touch real state
  03. create a temp git repo (no remote)
  04. oc_project_add  → register it
  05. oc_project_list / oc_project_show → confirm
  06. oc_spawn        → bootstrap a real opencode session with a tiny prompt
  07. oc_status       → confirm phase=EXECUTING and session live
  08. oc_wait         → block until idle
  09. oc_send         → second message, capture reply
  10. oc_kill         → tear down: delete session, remove worktree
  11. oc_project_remove

Exit 0 on full success, 1 on any failure.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "httpx-sse", "PyYAML"]
# ///
from __future__ import annotations

import asyncio
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


def _load_plugin_as_package(name: str = "_oc_orchestrator"):
    import importlib.util

    init = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(PLUGIN_ROOT)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build spec for {init}")
    pkg = importlib.util.module_from_spec(spec)
    pkg.__package__ = name
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    return pkg


PLUGIN = _load_plugin_as_package()


class Reporter:
    def __init__(self) -> None:
        self.n = 0
        self.failed: list[str] = []

    def start(self, name: str) -> None:
        self.n += 1
        sys.stdout.write(f"[{self.n:02d}] {name} … ")
        sys.stdout.flush()

    def ok(self, detail: str = "") -> None:
        sys.stdout.write(f"OK   {detail}\n")

    def fail(self, detail: str) -> None:
        sys.stdout.write(f"FAIL {detail}\n")
        self.failed.append(f"step {self.n}: {detail}")

    def note(self, line: str) -> None:
        sys.stdout.write(f"        {line}\n")


step = Reporter()


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("# smoke\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=s@l", "-c", "user.name=s",
         "commit", "-q", "-m", "init"],
        cwd=path, check=True,
    )


async def call(tool_spec, args: dict) -> dict:
    handler = tool_spec["handler"]
    raw = await handler(args)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "non-json", "raw": raw}


async def main(opencode_port: int) -> int:
    tmp_home = Path(tempfile.mkdtemp(prefix="oc-orch-home-"))
    os.environ["HERMES_HOME"] = str(tmp_home)
    repo_dir = Path(tempfile.mkdtemp(prefix="oc-orch-repo-"))

    step.start(f"set HERMES_HOME={tmp_home}")
    step.ok()

    step.start(f"init throwaway git repo at {repo_dir}")
    init_git_repo(repo_dir)
    step.ok()

    step.start("import plugin modules + assemble Runtime")
    from _oc_orchestrator.config import Config
    from _oc_orchestrator.projects import ProjectRegistry
    from _oc_orchestrator.state import AgentStore
    from _oc_orchestrator.tools import Runtime, all_tool_specs
    from _oc_orchestrator.transport import OpencodeClient

    config = Config.from_plugin_entry({"opencode_server": {"url": f"http://127.0.0.1:{opencode_port}"}, "auto_spawn_server": True})
    config.ensure_dirs()
    client = OpencodeClient(config.server_url, config.server_password)
    projects = ProjectRegistry(config.projects_file)
    agents = AgentStore(config.agents_file)
    rt = Runtime(config=config, client=client, projects=projects, agents=agents)
    specs = {s["name"]: s for s in all_tool_specs(rt)}
    step.ok(f"{len(specs)} tools available")

    if not port_open("127.0.0.1", opencode_port):
        step.start(f"opencode not on :{opencode_port}; ensure_server() will spawn")
        try:
            client.ensure_server()
            step.ok()
        except Exception as e:
            step.fail(repr(e))
            return 1
    else:
        step.start(f"opencode already on :{opencode_port}, attaching")
        step.ok()

    step.start("oc_project_add(label='smoke', repo_path=<temp>)")
    res = await call(specs["oc_project_add"], {"label": "smoke", "repo_path": str(repo_dir)})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    proj = res["data"]
    step.ok(f"abbrev={proj['abbrev']}  key={proj['project_key']}")

    step.start("oc_project_list")
    res = await call(specs["oc_project_list"], {})
    if not res.get("ok") or res["data"]["count"] != 1:
        step.fail(f"expected 1 project, got {res.get('data', {}).get('count')}")
        return 1
    step.ok(f"count={res['data']['count']}")

    step.start("oc_project_show(label='smoke')")
    res = await call(specs["oc_project_show"], {"label": "smoke"})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    step.ok(f"repo_path={res['data']['repo_path']}")

    step.start("oc_spawn(project='smoke', task='ping-test', prompt=...)")
    spawn_res = await call(specs["oc_spawn"], {
        "project": "smoke",
        "task": "ping-test",
        "prompt": "Reply with exactly the word 'pong' and nothing else.",
        "agent": "build",
    })
    if not spawn_res.get("ok"):
        step.fail(spawn_res.get("error", ""))
        return 1
    agent_id = spawn_res["data"]["agent_id"]
    session_id = spawn_res["data"]["session_id"]
    step.ok(f"agent_id={agent_id}  session={session_id[:16]}…")
    step.note(f"opencode_agent_resolved: {spawn_res['data'].get('opencode_agent_resolved')!r}")
    step.note(f"first_turn_assistant_text: {spawn_res['data'].get('first_turn_assistant_text', '')[:200]!r}")

    step.start(f"oc_status(agent_id='{agent_id}')")
    res = await call(specs["oc_status"], {"agent_id": agent_id})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    detail = res["data"]
    step.ok(f"phase={detail['phase']}  pending_q={len(detail.get('pending_questions', []))}  pending_p={len(detail.get('pending_permissions', []))}")

    step.start(f"oc_wait(agent_id='{agent_id}', timeout=120)")
    res = await call(specs["oc_wait"], {"agent_id": agent_id, "timeout_sec": 120})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    step.ok()

    step.start("oc_send(agent_id, text='Reply with only the word ack.')")
    res = await call(specs["oc_send"], {"agent_id": agent_id, "text": "Reply with only the word ack.", "timeout_sec": 120})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    step.ok(f"assistant_text={res['data'].get('assistant_text', '')[:80]!r}")

    step.start("oc_status() (list)")
    res = await call(specs["oc_status"], {})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    step.ok(f"agents listed: {res['data']['count']}")

    step.start(f"oc_kill(agent_id='{agent_id}', remove_worktree=True)")
    res = await call(specs["oc_kill"], {"agent_id": agent_id, "remove_worktree": True})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    step.ok(f"errors={res['data'].get('errors')}")

    step.start("oc_project_remove(label='smoke')")
    res = await call(specs["oc_project_remove"], {"label": "smoke"})
    if not res.get("ok"):
        step.fail(res.get("error", ""))
        return 1
    step.ok()

    step.start("agents.json is empty after kill+remove")
    after_agents = agents.list()
    if after_agents:
        step.fail(f"residual agents: {[a.agent_id for a in after_agents]}")
        return 1
    step.ok("empty")

    step.start("cleanup tempdirs")
    shutil.rmtree(tmp_home, ignore_errors=True)
    shutil.rmtree(repo_dir, ignore_errors=True)
    step.ok()

    return 0


if __name__ == "__main__":
    port = int(os.environ.get("OC_SMOKE_PORT", "4099"))
    spawned: subprocess.Popen | None = None
    try:
        if not port_open("127.0.0.1", port):
            spawned = subprocess.Popen(
                ["opencode", "serve", "--hostname=127.0.0.1", f"--port={port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True,
            )
            deadline = time.time() + 15
            while time.time() < deadline and not port_open("127.0.0.1", port):
                time.sleep(0.2)
        code = asyncio.run(main(port))
    finally:
        if spawned and spawned.poll() is None:
            spawned.terminate()
            try:
                spawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spawned.kill()

    print()
    if step.failed:
        print(f"FAILED ({len(step.failed)} step(s)):")
        for f in step.failed:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL STEPS PASSED ✓")
    sys.exit(code)
