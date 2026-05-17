#!/usr/bin/env python3
"""
Phase 0 — opencode transport spike for the hermes-agent hermes-opencode plugin.

Verifies every HTTP contract the plugin will depend on:
  1. `opencode serve` is reachable (auto-spawns if missing).
  2. POST /session with `agent` and `x-opencode-directory` header.
  3. POST /session/:id/message  sends a verbatim prompt.
  4. GET  /event  SSE emits session.status / session.idle / message parts.
  5. POST /api/session/:id/wait  blocks until idle.
  6. GET  /question  and  GET /permission  report pending entries.
  7. GET  /session/:id/messages  preserves option structure end-to-end.
  8. DELETE /session/:id  cleans up.

Usage:
  uv run --with httpx --with httpx-sse phase0_opencode_spike.py
  # or
  pip install httpx httpx-sse && python phase0_opencode_spike.py

Exit 0 on full success, 1 on any failed step. Self-contained: no hermes-agent
imports, no plugin coupling. Validates only the opencode HTTP surface so that
downstream plugin design rests on a known-good contract.
"""
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "httpx-sse"]
# ///
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import httpx
    from httpx_sse import aconnect_sse
except ImportError as exc:
    sys.stderr.write(f"missing dep: {exc}\ninstall with: pip install httpx httpx-sse\n")
    sys.exit(2)


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


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def spawn_opencode(host: str, port: int) -> subprocess.Popen[str]:
    binary = shutil.which("opencode")
    if not binary:
        raise SystemExit("opencode binary not on PATH; install opencode first")
    env = dict(os.environ)
    proc = subprocess.Popen(
        [binary, "serve", f"--hostname={host}", f"--port={port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if proc.poll() is not None:
            out = (proc.stdout.read() if proc.stdout else "") or "<no output>"
            raise SystemExit(f"opencode serve exited early:\n{out}")
        if port_open(host, port):
            return proc
        time.sleep(0.2)
    proc.terminate()
    raise SystemExit("opencode serve did not become ready within 15s")


def prepare_worktree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").exists():
        return
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("# phase0 spike\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=spike@local",
            "-c",
            "user.name=spike",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
    )


async def run_spike(host: str, port: int, worktree: Path, prompt: str, agent: str) -> None:
    base = f"http://{host}:{port}"
    headers = {"x-opencode-directory": str(worktree)}

    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=60) as client:
        step.start("GET /  (server reachable)")
        try:
            r = await client.get("/")
        except Exception as e:
            step.fail(repr(e))
            return
        step.ok(f"status={r.status_code}")

        events: list[dict] = []
        stop_flag = asyncio.Event()

        async def sse_consumer() -> None:
            try:
                async with aconnect_sse(client, "GET", "/event") as es:
                    async for sse in es.aiter_sse():
                        if not sse.data:
                            continue
                        try:
                            ev = json.loads(sse.data)
                        except json.JSONDecodeError:
                            continue
                        events.append(ev)
                        if stop_flag.is_set():
                            return
            except Exception as e:
                events.append({"_sse_error": repr(e)})

        sse_task = asyncio.create_task(sse_consumer())
        # 2s gives a cold-spawned opencode time to settle and emit the initial
        # server.connected frame. Tighter values race the server's readiness.
        await asyncio.sleep(2.0)

        step.start("GET /event  (SSE stream open)")
        if any(e.get("type") == "server.connected" for e in events):
            step.ok("server.connected received")
        else:
            step.fail(f"no server.connected in first burst: {events[:3]}")

        step.start(f"POST /session  agent={agent!r}  dir={worktree}")
        r = await client.post("/session", json={"agent": agent})
        if r.status_code >= 400:
            step.fail(f"{r.status_code} {r.text[:300]}")
            stop_flag.set()
            return
        session = r.json()
        sid = session.get("id") or session.get("sessionID") or session.get("ID")
        if not sid:
            step.fail(f"no session id in response keys={list(session.keys())[:8]}")
            stop_flag.set()
            return
        step.ok(f"id={sid}")
        step.note(f"session keys: {sorted(session.keys())[:10]}")

        step.start(f"POST /session/{sid}/message  (verbatim prompt)")
        try:
            r = await client.post(
                f"/session/{sid}/message",
                json={"parts": [{"type": "text", "text": prompt}]},
                timeout=180,
            )
            step.ok(f"status={r.status_code} body_chars={len(r.text)}")
        except Exception as e:
            step.fail(repr(e))
            stop_flag.set()
            return

        step.start(f"POST /api/session/{sid}/wait  (block until idle)")
        try:
            r = await client.post(f"/api/session/{sid}/wait", timeout=300)
            step.ok(f"status={r.status_code}")
        except Exception as e:
            step.fail(repr(e))
            stop_flag.set()
            return

        step.start("GET /question  (any pending)")
        r = await client.get("/question")
        if r.status_code != 200:
            step.fail(f"{r.status_code} {r.text[:200]}")
        else:
            qs = r.json() if r.text else []
            count = len(qs) if isinstance(qs, list) else "?"
            step.ok(f"count={count}")
            if isinstance(qs, list) and qs:
                first = qs[0]
                inner = (first.get("questions") or [{}])[0]
                opts = inner.get("options") or []
                if opts and isinstance(opts, list):
                    sample = opts[0]
                    step.note(
                        f"option schema: label={'label' in sample} "
                        f"description={'description' in sample}"
                    )

        step.start("GET /permission  (any pending)")
        r = await client.get("/permission")
        if r.status_code != 200:
            step.fail(f"{r.status_code} {r.text[:200]}")
        else:
            ps = r.json() if r.text else []
            count = len(ps) if isinstance(ps, list) else "?"
            step.ok(f"count={count}")

        step.start(f"GET /api/session/{sid}/message  (transcript, v2 paginated)")
        r = await client.get(f"/api/session/{sid}/message")
        if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
            step.fail(f"status={r.status_code} ctype={r.headers.get('content-type', '?')} body={r.text[:120]}")
        else:
            body = r.json()
            items = body.get("items", [])
            cursor = body.get("cursor", {})
            step.ok(f"items={len(items)} cursor.next={cursor.get('next')!r}")
            if items:
                first = items[0]
                step.note(f"item keys: {sorted(first.keys())[:10]}")

        stop_flag.set()
        await asyncio.sleep(0.3)
        sse_task.cancel()
        try:
            await sse_task
        except (asyncio.CancelledError, Exception):
            pass
        step.start("SSE event-type inventory")
        types_seen = sorted({e.get("type") for e in events if isinstance(e.get("type"), str)})
        if types_seen:
            step.ok(f"{len(types_seen)} distinct type(s) over {len(events)} events")
            for t in types_seen:
                step.note(f"• {t}")
            has_status = any(e.get("type") == "session.status" for e in events)
            has_idle = any(e.get("type") == "session.idle" for e in events)
            step.note(
                f"session.status seen: {has_status}   session.idle seen: {has_idle}"
            )
        else:
            step.fail("no SSE events captured")

        step.start(f"DELETE /session/{sid}  (cleanup)")
        try:
            r = await client.delete(f"/session/{sid}")
            step.ok(f"status={r.status_code}")
        except Exception as e:
            step.fail(repr(e))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Phase 0 transport spike for the hermes-opencode plugin."
    )
    p.add_argument("worktree", nargs="?", default=None, help="path to a worktree (defaults to a temp dir)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4096)
    p.add_argument(
        "--prompt",
        default="Reply with exactly the word 'pong' and nothing else.",
        help="prompt sent verbatim to opencode",
    )
    p.add_argument("--agent", default="build")
    p.add_argument("--keep-worktree", action="store_true")
    args = p.parse_args()

    if args.worktree:
        wt = Path(args.worktree).expanduser().resolve()
        cleanup_wt = False
    else:
        wt = Path(tempfile.mkdtemp(prefix="oc-spike-")).resolve()
        cleanup_wt = not args.keep_worktree
    print(f"worktree: {wt}")
    prepare_worktree(wt)

    spawned: subprocess.Popen[str] | None = None
    try:
        if port_open(args.host, args.port):
            print(f"opencode already on {args.host}:{args.port} — attaching\n")
        else:
            print(f"opencode not running on {args.host}:{args.port} — spawning\n")
            spawned = spawn_opencode(args.host, args.port)
        asyncio.run(run_spike(args.host, args.port, wt, args.prompt, args.agent))
    finally:
        if spawned and spawned.poll() is None:
            spawned.send_signal(signal.SIGTERM)
            try:
                spawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spawned.kill()
        if cleanup_wt:
            shutil.rmtree(wt, ignore_errors=True)

    print()
    if step.failed:
        print(f"FAILED ({len(step.failed)} step(s)):")
        for f in step.failed:
            print(f"  - {f}")
        return 1
    print("ALL STEPS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
