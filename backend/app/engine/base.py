"""Engine contract shared by the Docker implementation and the simulator."""

from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

LogSink = Callable[[str], None]
ProgressSink = Callable[[int | None, str], None]


class EngineError(RuntimeError):
    """Deployment failed. The message is safe to show to users."""


@dataclass(frozen=True)
class StackHandle:
    project: str
    compose_project: str  # ec-<project>
    compose_file: Path
    edge_network: str | None  # set when Traefik must be attached


@dataclass(frozen=True)
class ServiceStatus:
    """Observed state of one service container."""

    service: str
    state: str  # Docker state: running, exited, created, restarting, paused, dead...
    health: str | None  # healthy, unhealthy, starting, or None without a healthcheck


@dataclass(frozen=True)
class HostResources:
    """What the Docker host has left, as its containers see it. None: not measurable."""

    memory_total_mb: int | None
    memory_available_mb: int | None
    disk_free_mb: int | None
    # Docker Desktop's VM disk image: its free space may exceed the host drive's.
    disk_is_virtual: bool


@dataclass(frozen=True)
class RuntimeCheck:
    """One prerequisite of real deployments. `detail` is safe to show to users."""

    name: Literal["docker", "proxy"]
    ok: bool
    detail: str


@dataclass(frozen=True)
class LogLine:
    timestamp: datetime | None
    text: str


class Engine(Protocol):
    async def pull(self, stack: StackHandle, log: LogSink, progress: ProgressSink) -> None: ...

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        """Create networks, volumes and containers without starting them."""

    async def start(self, stack: StackHandle, log: LogSink, *, service: str | None = None) -> None:
        """Ensure the reverse proxy is attached, start containers and wait until healthy.
        With `service`, start that service alone (the others keep their state)."""

    async def stop(self, stack: StackHandle, log: LogSink, *, service: str | None = None) -> None:
        """Stop the containers, keeping them, their networks and volumes; with `service`,
        that service alone."""

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
        """Remove containers, networks and volumes of the project."""

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        """Observed containers per project, for the given stacks only. Raises EngineError."""

    async def recent_logs(self, stack: StackHandle, service: str, *, tail: int) -> list[LogLine]:
        """Last `tail` lines of one service, without following (e.g. before a rollback)."""
        ...

    def logs(self, stack: StackHandle, service: str, *, tail: int) -> AsyncGenerator[LogLine, None]:
        """Last `tail` lines of one service, then follow until the caller closes the generator."""
        ...

    async def missing_images(self, images: Sequence[str]) -> list[str]:
        """The given image references that are not in the local image store yet. Raises
        EngineError when Docker cannot tell."""
        ...

    async def resources(self) -> HostResources:
        """Memory and disk left for containers. Never raises: unknown values are None."""
        ...

    async def diagnose(self) -> list[RuntimeCheck]:
        """Whether the container runtime answers and the reverse proxy runs. Never raises."""
        ...
