"""Per-project workspace directories: `workspaces/<project>/{compose.yaml,.env}`.

* Paths are built from validated slugs, then resolved and checked to be inside
  the workspaces root (defence in depth against traversal and symlinks).
* Directories are created 0700 and files 0600: the .env file holds the
  project's generated secrets.
* All filesystem work runs in a worker thread, never on the event loop.
"""

import asyncio
import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.models.common import PROJECT_NAME_PATTERN
from app.models.environment import EnvironmentMeta
from app.policy.compose_policy import BUILTIN_VARIABLES

logger = logging.getLogger(__name__)

COMPOSE_FILE = "compose.yaml"
ENV_FILE = ".env"
META_FILE = "meta.json"
MAX_META_BYTES = 64 * 1024
_PROJECT_RE = re.compile(PROJECT_NAME_PATTERN)


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    project: str
    path: Path

    @property
    def compose_file(self) -> Path:
        return self.path / COMPOSE_FILE


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path_for(self, project: str) -> Path:
        root = self._root.resolve()
        path = (root / project).resolve()
        if path.parent != root:
            raise WorkspaceError("workspace path escapes the workspaces root")
        return path

    def get(self, project: str) -> Workspace | None:
        path = self._path_for(project)
        if not (path / COMPOSE_FILE).is_file():
            return None
        return Workspace(project=project, path=path)

    def exists(self, project: str) -> bool:
        return self._path_for(project).exists()

    def list_projects(self) -> list[str]:
        """Blocking. Workspace names that are valid project slugs and hold a compose file."""
        if not self._root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self._root.iterdir()
            if _PROJECT_RE.match(entry.name)
            and not entry.is_symlink()
            and (entry / COMPOSE_FILE).is_file()
        )

    def read_compose(self, project: str) -> dict[str, Any]:
        """Blocking. The rendered compose document of a workspace (our own output: it may
        contain YAML anchors emitted by safe_dump, so the strict loader is not used)."""
        text = (self._path_for(project) / COMPOSE_FILE).read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        if not isinstance(document, dict):
            raise WorkspaceError("the workspace compose file is not a mapping")
        return document

    async def create(
        self,
        project: str,
        compose: dict[str, Any],
        variables: dict[str, str],
        meta: EnvironmentMeta,
    ) -> Workspace:
        return await asyncio.to_thread(self._create, project, compose, variables, meta)

    async def remove(self, project: str) -> None:
        await asyncio.to_thread(self._remove, project)

    def read_meta(self, project: str) -> EnvironmentMeta | None:
        """Blocking: call through asyncio.to_thread(). None when absent or unreadable."""
        path = self._path_for(project) / META_FILE
        if not path.is_file():
            return None
        try:
            if path.stat().st_size > MAX_META_BYTES:
                raise ValueError(f"{META_FILE} exceeds {MAX_META_BYTES} bytes")
            return EnvironmentMeta.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            # The environment is still listed from Docker; details stay server-side.
            logger.warning("Ignoring unreadable %s of %s", META_FILE, project, exc_info=True)
            return None

    def read_secret_values(self, project: str) -> list[str]:
        """Blocking. Generated secret values of a project, only to redact them from logs."""
        try:
            text = (self._path_for(project) / ENV_FILE).read_text(encoding="utf-8")
        except OSError:
            return []
        values: list[str] = []
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key not in BUILTIN_VARIABLES and len(value) >= 8:
                values.append(value)
        return values

    @staticmethod
    def generate_secrets(names: tuple[str, ...]) -> dict[str, str]:
        # URL-safe alphabet: no quoting or escaping issues in .env or YAML.
        return {name: secrets.token_urlsafe(24) for name in names}

    def _create(
        self,
        project: str,
        compose: dict[str, Any],
        variables: dict[str, str],
        meta: EnvironmentMeta,
    ) -> Workspace:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path_for(project)
        path.mkdir(mode=0o700)  # fails if it already exists: never reuse a workspace
        os.chmod(path, 0o700)  # mkdir's mode is filtered by the umask

        document = yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
        env = "".join(f"{key}={value}\n" for key, value in variables.items())
        _write_private(path / COMPOSE_FILE, document)
        _write_private(path / ENV_FILE, env)
        _write_private(path / META_FILE, meta.model_dump_json(indent=2) + "\n")
        return Workspace(project=project, path=path)

    def _remove(self, project: str) -> None:
        path = self._path_for(project)
        if path.exists():
            shutil.rmtree(path)


def _write_private(path: Path, content: str) -> None:
    # O_EXCL: never follow or overwrite a file planted in advance.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
