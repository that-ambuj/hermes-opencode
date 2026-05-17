from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_NAME = "hermes-opencode"
DEFAULT_SERVER_URL = "http://127.0.0.1:4096"

HOME_CHANNEL_PLATFORMS = (
    "bluebubbles",
    "telegram",
    "discord",
    "slack",
    "teams",
    "google_chat",
    "feishu",
    "wecom",
    "line",
    "irc",
    "mattermost",
    "sms",
    "qqbot",
)


def discover_home_channel() -> tuple[str, str, str] | None:
    for platform in HOME_CHANNEL_PLATFORMS:
        env_var = f"{platform.upper()}_HOME_CHANNEL"
        chat_id = os.environ.get(env_var)
        if chat_id:
            return platform, chat_id, f"env:{env_var}"
    return None


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
    notify_discovery_source: str | None = None
    notify_events: set[str] = field(default_factory=lambda: {"pr_opened", "done", "failed", "awaiting_human", "review_started", "cancelled"})
    events_log: Path = field(default_factory=lambda: plugin_state_dir() / "events.log")
    heartbeat_enabled: bool = True
    heartbeat_timezone: str | None = None
    heartbeat_day_start: int = 9
    heartbeat_day_end: int = 23
    review_max_cycles: int = 1
    auto_bootstrap_on_first_spawn: bool = True
    classifier_enabled: bool = True
    classifier_task_name: str = "hermes_opencode.awaiting_input"
    classifier_max_input_chars: int = 2000
    classifier_max_output_tokens: int = 80
    classifier_timeout_sec: float = 8.0
    awaiting_input_stall_timeout_sec: float = 300.0
    awaiting_input_reminder_interval_sec: float = 1800.0

    @classmethod
    def from_plugin_entry(cls, entry: dict | None) -> "Config":
        entry = entry or {}
        server = entry.get("opencode_server") or {}
        pr = entry.get("pr") or {}
        notify = entry.get("notify") or {}
        gateway = notify.get("gateway") or {}
        events = (notify.get("events") or {})
        heartbeat = entry.get("heartbeat") or {}
        classifier = entry.get("classifier") or {}
        awaiting = entry.get("awaiting_input") or {}
        day_window = heartbeat.get("unconditional_hours", [9, 23])
        default_events = {"pr_opened", "done", "failed", "awaiting_human", "review_started", "cancelled"}

        platform = gateway.get("platform")
        explicit_chat_id = gateway.get("chat_id")
        chat_id: str | None = None
        discovery_source: str | None = None
        if platform:
            chat_id = explicit_chat_id or os.environ.get(f"{platform.upper()}_HOME_CHANNEL") or None
            if chat_id:
                discovery_source = "explicit" if explicit_chat_id else f"env:{platform.upper()}_HOME_CHANNEL"
        else:
            detected = discover_home_channel()
            if detected is not None:
                platform, chat_id, discovery_source = detected

        explicit_sinks = notify.get("sinks")
        if explicit_sinks is not None:
            sinks = list(explicit_sinks)
        elif platform and chat_id:
            sinks = ["gateway", "dashboard"]
        else:
            sinks = ["cli", "dashboard"]

        return cls(
            server_url=server.get("url", DEFAULT_SERVER_URL),
            server_password=server.get("password") or os.environ.get("OPENCODE_SERVER_PASSWORD") or None,
            default_base_branch=pr.get("base_branch", "main"),
            auto_spawn_server=bool(entry.get("auto_spawn_server", True)),
            notify_sinks=sinks,
            notify_gateway_platform=platform,
            notify_gateway_chat_id=chat_id,
            notify_discovery_source=discovery_source,
            notify_events=set(events.get("enabled", default_events)),
            heartbeat_enabled=bool(heartbeat.get("enabled", True)),
            heartbeat_timezone=heartbeat.get("timezone"),
            heartbeat_day_start=int(day_window[0]) if len(day_window) >= 1 else 9,
            heartbeat_day_end=int(day_window[1]) if len(day_window) >= 2 else 23,
            classifier_enabled=bool(classifier.get("enabled", True)),
            classifier_task_name=str(classifier.get("task", "hermes_opencode.awaiting_input")),
            classifier_max_input_chars=int(classifier.get("max_input_chars", 2000)),
            classifier_max_output_tokens=int(classifier.get("max_output_tokens", 80)),
            classifier_timeout_sec=float(classifier.get("timeout_sec", 8.0)),
            awaiting_input_stall_timeout_sec=float(awaiting.get("stall_timeout_sec", 300.0)),
            awaiting_input_reminder_interval_sec=float(awaiting.get("reminder_interval_sec", 1800.0)),
        )

    def ensure_dirs(self) -> None:
        for p in (self.worktrees_root, self.logs_dir, self.projects_file.parent):
            p.mkdir(parents=True, exist_ok=True)
