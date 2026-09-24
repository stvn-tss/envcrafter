"""Engine contract shared by the Docker implementation and the simulator."""

from collections.abc import Callable
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


class Engine(Protocol):
    async def pull(self, stack: StackHandle, log: LogSink) -> None: ...

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        """Create networks, volumes and containers without starting them."""

    async def start(self, stack: StackHandle, log: LogSink) -> None:
        """Start containers and wait until they are running/healthy."""

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
        """Remove containers, networks and volumes of the project."""
