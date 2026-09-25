"""Engine contract shared by the Docker implementation and the simulator."""

from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

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
class LogLine:
    timestamp: datetime | None
    text: str


class Engine(Protocol):
    async def pull(self, stack: StackHandle, log: LogSink, progress: ProgressSink) -> None: ...

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        """Create networks, volumes and containers without starting them."""

    async def start(self, stack: StackHandle, log: LogSink) -> None:
        """Ensure the reverse proxy is attached, start containers and wait until healthy."""

    async def stop(self, stack: StackHandle, log: LogSink) -> None:
        """Stop the containers, keeping them, their networks and volumes."""

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
        """Remove containers, networks and volumes of the project."""

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        """Observed containers per project, for the given stacks only. Raises EngineError."""

    def logs(self, stack: StackHandle, service: str, *, tail: int) -> AsyncGenerator[LogLine, None]:
        """Last `tail` lines of one service, then follow until the caller closes the generator."""
        ...
