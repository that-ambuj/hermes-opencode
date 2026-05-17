from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("hermes_opencode.transport")


class OpencodeError(RuntimeError):
    pass


_T = TypeVar("_T")


def _wrap_transport_errors(coro: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
    @functools.wraps(coro)
    async def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return await coro(*args, **kwargs)
        except OpencodeError:
            raise
        except httpx.HTTPError as e:
            raise OpencodeError(f"transport error in {coro.__name__}: {type(e).__name__}: {e}") from e
    return wrapper


class OpencodeClient:
    def __init__(self, base_url: str, password: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        if password:
            self._headers["x-opencode-password"] = password
        parsed = urlparse(self.base_url)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 80
        self._spawned: subprocess.Popen[str] | None = None
        self._spawn_lock = threading.Lock()
        self._last_serve_log_path: Path | None = None

    def _client(self, directory: Path | None = None, timeout: float = 60.0) -> httpx.AsyncClient:
        headers = dict(self._headers)
        if directory is not None:
            headers["x-opencode-directory"] = str(directory)
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout)

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    async def ping(self) -> bool:
        try:
            async with self._client(timeout=2.0) as c:
                r = await c.get("/")
                return r.status_code == 200
        except Exception:
            return False

    def ensure_server(self, deadline_sec: float = 15.0, log_dir: Path | None = None) -> None:
        with self._spawn_lock:
            if self._port_open(self._host, self._port):
                return
            self._reap_tracked_spawn()
            binary = shutil.which("opencode")
            if not binary:
                for candidate in [
                    Path.home() / ".bun" / "bin" / "opencode",
                    Path("/usr/local/bin/opencode"),
                    Path("/opt/homebrew/bin/opencode"),
                ]:
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        binary = str(candidate)
                        break
            if not binary:
                raise OpencodeError("opencode binary not found on PATH; install opencode first")
            env = dict(os.environ)
            log_path = self._prepare_serve_log_path(log_dir)
            log_handle = self._open_serve_log(log_path)
            self._spawned = subprocess.Popen(
                [binary, "serve", f"--hostname={self._host}", f"--port={self._port}"],
                stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_handle is not None else subprocess.DEVNULL,
                text=True,
                env=env,
                start_new_session=True,
            )
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass
                logger.info("opencode serve stdout+stderr → %s (pid=%s)", log_path, self._spawned.pid)
                self._last_serve_log_path = log_path
            deadline = time.time() + deadline_sec
            while time.time() < deadline:
                if self._spawned.poll() is not None:
                    exit_code = self._spawned.returncode
                    self._spawned = None
                    detail = self._tail_log(log_path, 30) if log_path else ""
                    raise OpencodeError(
                        f"opencode serve exited during startup (rc={exit_code})"
                        + (f"; last log lines:\n{detail}" if detail else "")
                    )
                if self._port_open(self._host, self._port):
                    return
                time.sleep(0.2)
            try:
                self._spawned.terminate()
            finally:
                self._spawned = None
            raise OpencodeError(f"opencode serve did not become ready within {deadline_sec:.0f}s")

    @staticmethod
    def _prepare_serve_log_path(log_dir: Path | None) -> Path | None:
        if log_dir is None:
            return None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("could not create opencode serve log dir %s: %s", log_dir, e)
            return None
        ts = time.strftime("%Y%m%d-%H%M%S")
        return log_dir / f"opencode-serve.{ts}.log"

    @staticmethod
    def _open_serve_log(log_path: Path | None):
        if log_path is None:
            return None
        try:
            return log_path.open("ab", buffering=0)
        except OSError as e:
            logger.warning("could not open opencode serve log %s: %s", log_path, e)
            return None

    @staticmethod
    def _tail_log(log_path: Path | None, lines: int) -> str:
        if log_path is None or not log_path.exists():
            return ""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def _reap_tracked_spawn(self) -> None:
        spawned = self._spawned
        if spawned is None:
            return
        try:
            if spawned.poll() is None:
                spawned.terminate()
                try:
                    spawned.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    spawned.kill()
                    try:
                        spawned.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception as e:
            logger.debug("reaping tracked opencode spawn raised: %s", e)
        finally:
            self._spawned = None

    @_wrap_transport_errors
    async def create_session(self, directory: Path, agent: str = "build") -> dict[str, Any]:
        async with self._client(directory) as c:
            r = await c.post("/session", json={"agent": agent})
            if r.status_code >= 400:
                raise OpencodeError(f"POST /session failed: {r.status_code} {r.text[:200]}")
            return r.json()

    @_wrap_transport_errors
    async def send_message(self, session_id: str, directory: Path, text: str, timeout: float = 600.0) -> dict[str, Any]:
        async with self._client(directory, timeout=timeout) as c:
            r = await c.post(
                f"/session/{session_id}/message",
                json={"parts": [{"type": "text", "text": text}]},
            )
            if r.status_code >= 400:
                raise OpencodeError(f"POST message failed: {r.status_code} {r.text[:200]}")
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}

    @_wrap_transport_errors
    async def send_message_async(self, session_id: str, directory: Path, text: str, timeout: float = 30.0) -> dict[str, Any]:
        """Fire-and-forget: queue a prompt on the session and return immediately.

        Use from synchronous-host code paths (hermes tool handlers, CLI subcommands)
        where blocking the caller for the full assistant turn is unacceptable.
        The plugin's bg event-loop is responsible for picking up completion via
        the polling / SSE channels.
        """
        async with self._client(directory, timeout=timeout) as c:
            r = await c.post(
                f"/session/{session_id}/prompt_async",
                json={"parts": [{"type": "text", "text": text}]},
            )
            if r.status_code >= 400:
                raise OpencodeError(f"POST prompt_async failed: {r.status_code} {r.text[:200]}")
            try:
                return r.json() if r.text else {"queued": True}
            except Exception:
                return {"raw": r.text, "queued": True}

    @_wrap_transport_errors
    async def wait_idle(self, session_id: str, directory: Path, timeout: float = 600.0) -> bool:
        async with self._client(directory, timeout=timeout) as c:
            try:
                r = await c.post(f"/api/session/{session_id}/wait")
                return r.status_code in (200, 204)
            except httpx.ReadTimeout:
                return False

    @_wrap_transport_errors
    async def get_messages(self, session_id: str, directory: Path, cursor: str | None = None) -> dict[str, Any]:
        async with self._client(directory) as c:
            params: dict[str, str] = {}
            if cursor:
                params["cursor"] = cursor
            r = await c.get(f"/api/session/{session_id}/message", params=params)
            if r.status_code >= 400:
                raise OpencodeError(f"GET messages failed: {r.status_code} {r.text[:200]}")
            return r.json()

    @_wrap_transport_errors
    async def list_questions(self, directory: Path) -> list[dict[str, Any]]:
        async with self._client(directory) as c:
            r = await c.get("/question")
            if r.status_code >= 400:
                raise OpencodeError(f"GET /question failed: {r.status_code}")
            data = r.json() if r.text else []
            return data if isinstance(data, list) else []

    @_wrap_transport_errors
    async def list_permissions(self, directory: Path) -> list[dict[str, Any]]:
        async with self._client(directory) as c:
            r = await c.get("/permission")
            if r.status_code >= 400:
                raise OpencodeError(f"GET /permission failed: {r.status_code}")
            data = r.json() if r.text else []
            return data if isinstance(data, list) else []

    @_wrap_transport_errors
    async def reply_question(self, question_id: str, directory: Path, answers: list[str]) -> bool:
        async with self._client(directory) as c:
            r = await c.post(f"/question/{question_id}/reply", json={"answers": answers})
            return r.status_code in (200, 204)

    @_wrap_transport_errors
    async def reject_question(self, question_id: str, directory: Path) -> bool:
        async with self._client(directory) as c:
            r = await c.post(f"/question/{question_id}/reject")
            return r.status_code in (200, 204)

    @_wrap_transport_errors
    async def reply_permission(
        self, session_id: str, permission_id: str, directory: Path,
        reply: str, message: str | None = None,
    ) -> bool:
        if reply not in {"once", "always", "reject"}:
            raise ValueError(f"reply must be one of once|always|reject, got {reply!r}")
        body: dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        async with self._client(directory) as c:
            r = await c.post(f"/session/{session_id}/permissions/{permission_id}", json=body)
            return r.status_code in (200, 204)

    @_wrap_transport_errors
    async def delete_session(self, session_id: str, directory: Path) -> bool:
        async with self._client(directory) as c:
            r = await c.delete(f"/session/{session_id}")
            return r.status_code in (200, 204)

    async def stream_events(
        self, directory: Path, stop_event: asyncio.Event,
        *, reconnect_backoff: float = 3.0,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            from httpx_sse import aconnect_sse
        except ImportError as e:
            raise OpencodeError(f"httpx-sse not installed: {e}") from e
        while not stop_event.is_set():
            headers = dict(self._headers)
            headers["x-opencode-directory"] = str(directory)
            try:
                async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=None) as c:
                    async with aconnect_sse(c, "GET", "/event") as es:
                        async for sse in es.aiter_sse():
                            if stop_event.is_set():
                                return
                            data = sse.data
                            if not data:
                                continue
                            try:
                                yield json.loads(data)
                            except (ValueError, TypeError):
                                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("stream_events reconnecting after %s", e)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=reconnect_backoff)
                    return
                except asyncio.TimeoutError:
                    continue

    @staticmethod
    def extract_assistant_text(send_message_response: dict[str, Any]) -> str:
        parts = send_message_response.get("parts") or []
        chunks: list[str] = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        return "\n".join(chunks).strip()
