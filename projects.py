from __future__ import annotations

import json
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .worktree import derive_abbrev, project_key_for, remote_url


@dataclass
class Project:
    label: str
    abbrev: str
    project_key: str
    remote_url: str | None
    repo_path: str
    base_branch: str = "main"
    bootstrap_skill: str | None = None
    cleanup_skill: str | None = None
    created_at: float = field(default_factory=time.time)


class ProjectExists(ValueError):
    pass


class ProjectNotFound(KeyError):
    pass


class ProjectRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self._path.parent, prefix=self._path.name + ".",
            suffix=".tmp", delete=False, encoding="utf-8",
        ) as f:
            json.dump(data, f, indent=2, sort_keys=True)
            tmp = Path(f.name)
        tmp.replace(self._path)

    def list(self) -> list[Project]:
        with self._lock:
            return [Project(**v) for v in self._read().values()]

    def get(self, label: str) -> Project | None:
        with self._lock:
            raw = self._read().get(label)
            return Project(**raw) if raw else None

    def add(
        self, label: str, repo_path: Path, base_branch: str = "main",
        abbrev: str | None = None, bootstrap_skill: str | None = None,
        cleanup_skill: str | None = None,
    ) -> Project:
        repo_path = repo_path.expanduser().resolve()
        if not repo_path.is_dir():
            raise ValueError(f"repo_path not found or not a directory: {repo_path}")
        if not (repo_path / ".git").exists():
            raise ValueError(f"not a git repository: {repo_path}")
        with self._lock:
            d = self._read()
            if label in d:
                raise ProjectExists(f"project label already exists: {label}")
            resolved = abbrev or derive_abbrev(label)
            for other in d.values():
                if other["abbrev"] == resolved and other["label"] != label:
                    raise ValueError(
                        f"abbrev {resolved!r} already used by project {other['label']!r}; "
                        f"pass an explicit `abbrev` argument."
                    )
            project = Project(
                label=label,
                abbrev=resolved,
                project_key=project_key_for(repo_path),
                remote_url=remote_url(repo_path),
                repo_path=str(repo_path),
                base_branch=base_branch,
                bootstrap_skill=bootstrap_skill,
                cleanup_skill=cleanup_skill,
            )
            d[label] = asdict(project)
            self._write(d)
            return project

    def remove(self, label: str) -> Project | None:
        with self._lock:
            d = self._read()
            raw = d.pop(label, None)
            if raw is None:
                return None
            self._write(d)
            return Project(**raw)

    def update(self, label: str, **fields_to_set: object) -> Project:
        with self._lock:
            d = self._read()
            if label not in d:
                raise ProjectNotFound(label)
            for k, v in fields_to_set.items():
                if v is not None:
                    d[label][k] = v
            self._write(d)
            return Project(**d[label])
