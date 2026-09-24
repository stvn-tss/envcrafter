"""Engine contract shared by the Docker implementation and the simulator."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LogSink = Callable[[str], None]


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


class Engine(Protocol):
    async def pull(self, stack: StackHandle, log: LogSink) -> None: ...

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        """Create networks, volumes and containers without starting them."""

    async def start(self, stack: StackHandle, log: LogSink) -> None:
        """Start containers and wait until they are running/healthy."""

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
        """Remove containers, networks and volumes of the project."""

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        """Observed containers per project, for the given stacks only. Raises EngineError."""
