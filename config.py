from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_NAME = "hermes-opencode"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4096
DEFAULT_PR_FALLBACK_MODELS: tuple[str, ...] = (
    "openai/gpt-5.5",
    "opencode/deepseek-v4-flash-free",
)

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
    """Read this plugin's entry from ``~/.hermes/config.yaml``.

    Returns the dict under ``plugins.entries.hermes-opencode`` or an
    empty dict on any read/parse failure. Importable from both
    ``__init__.py`` (in-session) and ``cli.py`` (out-of-session CLI
    subcommands) without a circular import.

    v0.16.3 bugfix: previous versions called
    ``cfg_get(f"plugins.entries.{PLUGIN_NAME}", {})`` which silently
    returned ``{}`` because ``hermes_cli.config.cfg_get`` takes
    ``(cfg_dict, *positional_path_keys, default=...)`` not a dotted
    string. The bug masked every user-set value in the plugin's YAML
    entry (host, review.max_cycles, notify.*, classifier.*, etc.) so
    only the dataclass defaults ever applied. The fix walks the path
    with positional keys.
    """
    try:
        from hermes_cli.config import cfg_get, load_config  # type: ignore
    except ImportError:
        return _load_entry_from_raw_yaml()
    try:
        cfg = load_config()
    except Exception:
        return _load_entry_from_raw_yaml()
    entry = cfg_get(cfg, "plugins", "entries", PLUGIN_NAME, default={})
    return entry if isinstance(entry, dict) else {}


def _load_entry_from_raw_yaml() -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    config_path = _resolve_hermes_home() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    node: object = data
    for key in ("plugins", "entries", PLUGIN_NAME):
        if not isinstance(node, dict) or key not in node:
            return {}
        node = node[key]
    return node if isinstance(node, dict) else {}


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    server_password: str | None = None
    pr_fallback_models: list[str] = field(default_factory=lambda: list(DEFAULT_PR_FALLBACK_MODELS))
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
    notify_events: set[str] = field(default_factory=lambda: {"pr_opened", "done", "failed", "awaiting_human", "awaiting_human_resumed", "review_started", "cancelled", "tick_error", "aborted", "rate_limited", "rate_limit_cleared", "queued", "queue_drained"})
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
        default_events = {"pr_opened", "done", "failed", "awaiting_human", "awaiting_human_resumed", "review_started", "cancelled", "tick_error", "aborted", "rate_limited", "rate_limit_cleared", "queued", "queue_drained"}

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

        host_value = server.get("host") or os.environ.get("OPENCODE_HOST") or DEFAULT_HOST
        port_raw = server.get("port") if server.get("port") is not None else os.environ.get("OPENCODE_PORT")
        port_value = int(port_raw) if port_raw is not None else DEFAULT_PORT
        return cls(
            host=host_value,
            port=port_value,
            server_password=server.get("password") or os.environ.get("OPENCODE_SERVER_PASSWORD") or None,
            pr_fallback_models=cls._resolve_pr_fallback_models(server.get("pr_fallback_models")),
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

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def connect_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        for p in (self.worktrees_root, self.logs_dir, self.projects_file.parent):
            p.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _resolve_pr_fallback_models(yaml_value: Any) -> list[str]:
        if isinstance(yaml_value, list) and yaml_value:
            return [str(x).strip() for x in yaml_value if str(x).strip()]
        env_value = os.environ.get("OPENCODE_PR_FALLBACK_MODELS")
        if env_value:
            parsed = [x.strip() for x in env_value.split(",") if x.strip()]
            if parsed:
                return parsed
        return list(DEFAULT_PR_FALLBACK_MODELS)
