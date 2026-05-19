from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx
from opencode_api import OpencodeAsync
from opencode_api.models import (
    QuestionReplyRequest,
    SessionCreateRequest,
    SessionCreateRequestModel,
    SessionPromptAsyncRequest,
    SessionPromptRequest,
)
from opencode_api.models.permission_respond_request import (
    PermissionRespondRequest,
    Response as PermissionRespondResponseEnum,
)
from opencode_api.net.transport.api_error import ApiError
from opencode_api.net.transport.request_error import RequestError

logger = logging.getLogger("hermes_opencode.transport")


class OpencodeError(RuntimeError):
    pass


_T = TypeVar("_T")


_SDK_DEFAULT_TIMEOUT_MS = 60_000
_SDK_LONG_TIMEOUT_MS = 600_000


def _wrap_transport_errors(coro: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
    @functools.wraps(coro)
    async def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return await coro(*args, **kwargs)
        except OpencodeError:
            raise
        except (httpx.HTTPError, ApiError, RequestError) as e:
            raise OpencodeError(f"transport error in {coro.__name__}: {type(e).__name__}: {e}") from e
    return wrapper


def _response_dict(resp: Any) -> dict[str, Any]:
    raw = getattr(resp, "raw", None)
    if raw is None:
        return {}
    try:
        body = raw.json()
    except (ValueError, AttributeError):
        return {}
    return body if isinstance(body, dict) else {}


def _response_list(resp: Any) -> list[dict[str, Any]]:
    raw = getattr(resp, "raw", None)
    if raw is None:
        return []
    try:
        body = raw.json()
    except (ValueError, AttributeError):
        return []
    return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []


def _response_status_ok(resp: Any) -> bool:
    raw = getattr(resp, "raw", None)
    if raw is None:
        return True
    return getattr(raw, "status_code", 0) in (200, 204)


class OpencodeClient:
    def __init__(
        self,
        host: str,
        port: int,
        password: str | None = None,
    ) -> None:
        self._host = host
        self._port = int(port)
        self.base_url = f"http://{self._host}:{self._port}"
        self._headers: dict[str, str] = {}
        if password:
            self._headers["x-opencode-password"] = password
        self._spawned: subprocess.Popen[str] | None = None
        self._spawn_lock = threading.Lock()
        self._last_serve_log_path: Path | None = None
        self._spawn_started_at: float | None = None
        self._last_spawn_pid: int | None = None
        self._sdk_default: OpencodeAsync | None = None
        self._sdk_long: OpencodeAsync | None = None
        self._sdk_lock = threading.Lock()

    @property
    def endpoint(self) -> str:
        return f"{self._host}:{self._port}"

    def _sdk(self, *, long_timeout: bool = False) -> OpencodeAsync:
        slot_attr = "_sdk_long" if long_timeout else "_sdk_default"
        existing = getattr(self, slot_attr)
        if existing is not None:
            return existing
        with self._sdk_lock:
            existing = getattr(self, slot_attr)
            if existing is not None:
                return existing
            timeout_ms = _SDK_LONG_TIMEOUT_MS if long_timeout else _SDK_DEFAULT_TIMEOUT_MS
            client = OpencodeAsync(base_url=self.base_url, timeout=timeout_ms)
            setattr(self, slot_attr, client)
            return client

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, headers=dict(self._headers), timeout=2.0) as c:
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
            self._spawn_started_at = time.time()
            self._last_spawn_pid = self._spawned.pid
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

    @staticmethod
    def _signal_name_from_returncode(rc: int) -> str | None:
        if rc is None:
            return None
        if rc < 0:
            sig = -rc
        elif rc > 128:
            sig = rc - 128
        else:
            return None
        try:
            return signal.Signals(sig).name
        except (ValueError, AttributeError):
            return f"SIG?({sig})"

    def last_exit_info(self) -> dict[str, Any] | None:
        spawned = self._spawned
        pid = self._last_spawn_pid
        started_at = self._spawn_started_at
        if spawned is None:
            if pid is None:
                return None
            return {
                "pid": pid,
                "exit_code": None,
                "signal_name": None,
                "exit_kind": "unknown_already_reaped",
                "uptime_sec": (time.time() - started_at) if started_at else None,
                "log_path": str(self._last_serve_log_path) if self._last_serve_log_path else None,
            }
        rc = spawned.poll()
        if rc is None:
            return {
                "pid": pid,
                "exit_code": None,
                "signal_name": None,
                "exit_kind": "still_running",
                "uptime_sec": (time.time() - started_at) if started_at else None,
                "log_path": str(self._last_serve_log_path) if self._last_serve_log_path else None,
            }
        signal_name = self._signal_name_from_returncode(rc)
        if signal_name is not None:
            kind = "killed_by_signal"
        elif rc == 0:
            kind = "clean_exit"
        else:
            kind = "nonzero_exit"
        return {
            "pid": pid,
            "exit_code": rc,
            "signal_name": signal_name,
            "exit_kind": kind,
            "uptime_sec": (time.time() - started_at) if started_at else None,
            "log_path": str(self._last_serve_log_path) if self._last_serve_log_path else None,
        }

    def last_serve_log_tail(self, lines: int = 20) -> str:
        return self._tail_log(self._last_serve_log_path, lines)

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
    async def create_session(
        self,
        directory: Path,
        agent: str = "build",
        model: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"agent": agent}
        if model:
            kwargs["model"] = SessionCreateRequestModel(
                id_=model["id"],
                provider_id=model["providerID"],
                **({"variant": model["variant"]} if model.get("variant") else {}),
            )
        body = SessionCreateRequest(**kwargs)
        resp = await self._sdk().session.session_create(request_body=body, directory=str(directory))
        return _response_dict(resp)

    @_wrap_transport_errors
    async def send_message(self, session_id: str, directory: Path, text: str, timeout: float = 600.0) -> dict[str, Any]:
        body = SessionPromptRequest(parts=[{"type": "text", "text": text}])
        resp = await self._sdk(long_timeout=True).session.session_prompt(
            session_id=session_id, request_body=body, directory=str(directory),
        )
        return _response_dict(resp)

    @_wrap_transport_errors
    async def send_message_async(self, session_id: str, directory: Path, text: str, timeout: float = 30.0) -> dict[str, Any]:
        body = SessionPromptAsyncRequest(parts=[{"type": "text", "text": text}])
        await self._sdk().session.session_prompt_async(
            session_id=session_id, request_body=body, directory=str(directory),
        )
        return {"queued": True}

    @_wrap_transport_errors
    async def wait_idle(self, session_id: str, directory: Path, timeout: float = 600.0) -> bool:
        await self._sdk(long_timeout=True).v2.v2_session_wait(
            session_id=session_id, directory=str(directory),
        )
        return True

    @_wrap_transport_errors
    async def get_messages(self, session_id: str, directory: Path, cursor: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"session_id": session_id, "directory": str(directory)}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await self._sdk().v2_messages.v2_session_messages(**kwargs)
        return _response_dict(resp)

    @_wrap_transport_errors
    async def list_questions(self, directory: Path) -> list[dict[str, Any]]:
        resp = await self._sdk().question.question_list(directory=str(directory))
        return _response_list(resp)

    @_wrap_transport_errors
    async def list_permissions(self, directory: Path) -> list[dict[str, Any]]:
        resp = await self._sdk().permission.permission_list(directory=str(directory))
        return _response_list(resp)

    @_wrap_transport_errors
    async def list_session_status(self, directory: Path) -> dict[str, dict[str, Any]]:
        resp = await self._sdk().session.session_status(directory=str(directory))
        body = _response_dict(resp)
        return {sid: status for sid, status in body.items() if isinstance(status, dict)}

    @_wrap_transport_errors
    async def list_todos(self, session_id: str, directory: Path) -> list[dict[str, Any]]:
        resp = await self._sdk().session.session_todo(session_id=session_id, directory=str(directory))
        return _response_list(resp)

    @_wrap_transport_errors
    async def session_diff(self, session_id: str, directory: Path, message_id: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"session_id": session_id, "directory": str(directory)}
        if message_id:
            kwargs["message_id"] = message_id
        resp = await self._sdk().session.session_diff(**kwargs)
        return _response_list(resp)

    @_wrap_transport_errors
    async def reply_question(self, question_id: str, directory: Path, answers: list[list[str]]) -> bool:
        """Outer list = one inner array per sub-question (in order); inner list = selected option labels or [free_text]. Missing/short outer surfaces "Unanswered" on the executor side."""
        body = QuestionReplyRequest(answers=answers)
        resp = await self._sdk().question.question_reply(
            request_id=question_id, request_body=body, directory=str(directory),
        )
        return _response_status_ok(resp)

    @_wrap_transport_errors
    async def reject_question(self, question_id: str, directory: Path) -> bool:
        resp = await self._sdk().question.question_reject(
            request_id=question_id, directory=str(directory),
        )
        return _response_status_ok(resp)

    @_wrap_transport_errors
    async def reply_permission(
        self, session_id: str, permission_id: str, directory: Path,
        reply: str, message: str | None = None,
    ) -> bool:
        if reply not in {"once", "always", "reject"}:
            raise ValueError(f"reply must be one of once|always|reject, got {reply!r}")
        body = PermissionRespondRequest(response=PermissionRespondResponseEnum(reply))
        resp = await self._sdk().session.permission_respond(
            session_id=session_id, permission_id=permission_id,
            request_body=body, directory=str(directory),
        )
        return _response_status_ok(resp)

    @_wrap_transport_errors
    async def delete_session(self, session_id: str, directory: Path) -> bool:
        resp = await self._sdk().session.session_delete(
            session_id=session_id, directory=str(directory),
        )
        return _response_status_ok(resp)

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
