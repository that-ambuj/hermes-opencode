from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_NAME = "hermes-opencode"
DEFAULT_SERVER_URL = "http://127.0.0.1:4096"


def _resolve_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return get_hermes_home()
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def hermes_home() -> Path:
    return _resolve_hermes_home()


def plugin_state_dir() -> Path:
    d = hermes_home() / "plugins" / PLUGIN_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_entry_config() -> dict:
    """Read this plugin's entry from ~/.hermes/config.yaml.

    Importable from both ``__init__.py`` (in-session) and ``cli.py`` (out-of-
    session CLI subcommands) without a circular import.
    """
    try:
        from hermes_cli.config import cfg_get  # type: ignore
    except ImportError:
        return {}
    try:
        return cfg_get(f"plugins.entries.{PLUGIN_NAME}", {}) or {}
    except Exception:
        return {}


@dataclass
class Config:
    server_url: str = DEFAULT_SERVER_URL
    server_password: str | None = None
    default_base_branch: str = "main"
    worktrees_root: Path = field(default_factory=lambda: plugin_state_dir() / "wt")
    projects_file: Path = field(default_factory=lambda: plugin_state_dir() / "projects.json")
    agents_file: Path = field(default_factory=lambda: plugin_state_dir() / "agents.json")
    logs_dir: Path = field(default_factory=lambda: plugin_state_dir() / "logs")
    notifications_file: Path = field(default_factory=lambda: plugin_state_dir() / "notifications.jsonl")
    auto_spawn_server: bool = True
    notify_sinks: list[str] = field(default_factory=lambda: ["cli", "dashboard"])
    notify_gateway_platform: str | None = None
    notify_gateway_chat_id: str | None = None
    notify_events: set[str] = field(default_factory=lambda: {"pr_opened", "done", "failed", "awaiting_human", "review_started"})
    events_log: Path = field(default_factory=lambda: plugin_state_dir() / "events.log")
    heartbeat_enabled: bool = True
    heartbeat_timezone: str | None = None
    heartbeat_day_start: int = 9
    heartbeat_day_end: int = 23
    review_max_cycles: int = 1
    auto_bootstrap_on_first_spawn: bool = True

    @classmethod
    def from_plugin_entry(cls, entry: dict | None) -> "Config":
        entry = entry or {}
        server = entry.get("opencode_server") or {}
        pr = entry.get("pr") or {}
        notify = entry.get("notify") or {}
        gateway = notify.get("gateway") or {}
        events = (notify.get("events") or {})
        heartbeat = entry.get("heartbeat") or {}
        day_window = heartbeat.get("unconditional_hours", [9, 23])
        default_events = {"pr_opened", "done", "failed", "awaiting_human", "review_started"}
        return cls(
            server_url=server.get("url", DEFAULT_SERVER_URL),
            server_password=server.get("password") or os.environ.get("OPENCODE_SERVER_PASSWORD") or None,
            default_base_branch=pr.get("base_branch", "main"),
            auto_spawn_server=bool(entry.get("auto_spawn_server", True)),
            notify_sinks=list(notify.get("sinks", ["cli", "dashboard"])),
            notify_gateway_platform=gateway.get("platform"),
            notify_gateway_chat_id=gateway.get("chat_id") or os.environ.get(
                f"{(gateway.get('platform') or '').upper()}_HOME_CHANNEL"
            ) or None,
            notify_events=set(events.get("enabled", default_events)),
            heartbeat_enabled=bool(heartbeat.get("enabled", True)),
            heartbeat_timezone=heartbeat.get("timezone"),
            heartbeat_day_start=int(day_window[0]) if len(day_window) >= 1 else 9,
            heartbeat_day_end=int(day_window[1]) if len(day_window) >= 2 else 23,
        )

    def ensure_dirs(self) -> None:
        for p in (self.worktrees_root, self.logs_dir, self.projects_file.parent):
            p.mkdir(parents=True, exist_ok=True)
