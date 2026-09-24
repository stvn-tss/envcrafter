"""Per-project workspace directories: `workspaces/<project>/{compose.yaml,.env}`.

* Paths are built from validated slugs, then resolved and checked to be inside
  the workspaces root (defence in depth against traversal and symlinks).
* Directories are created 0700 and files 0600: the .env file holds the
  project's generated secrets.
* All filesystem work runs in a worker thread, never on the event loop.
"""

import asyncio
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

COMPOSE_FILE = "compose.yaml"
ENV_FILE = ".env"


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

    async def create(
        self, project: str, compose: dict[str, Any], variables: dict[str, str]
    ) -> Workspace:
        return await asyncio.to_thread(self._create, project, compose, variables)

    async def remove(self, project: str) -> None:
        await asyncio.to_thread(self._remove, project)

    @staticmethod
    def generate_secrets(names: tuple[str, ...]) -> dict[str, str]:
        # URL-safe alphabet: no quoting or escaping issues in .env or YAML.
        return {name: secrets.token_urlsafe(24) for name in names}

    def _create(
        self, project: str, compose: dict[str, Any], variables: dict[str, str]
    ) -> Workspace:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path_for(project)
        path.mkdir(mode=0o700)  # fails if it already exists: never reuse a workspace
        os.chmod(path, 0o700)  # mkdir's mode is filtered by the umask

        document = yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
        env = "".join(f"{key}={value}\n" for key, value in variables.items())
        _write_private(path / COMPOSE_FILE, document)
        _write_private(path / ENV_FILE, env)
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
