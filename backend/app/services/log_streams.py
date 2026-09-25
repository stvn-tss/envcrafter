"""Container logs for the UI: environment and service checks, secret redaction, and a cap
on concurrent streams (each one is a `docker compose logs --follow` process)."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing, asynccontextmanager

from app.engine.base import Engine, LogLine, StackHandle
from app.workspace.manager import WorkspaceManager
from app.workspace.renderer import compose_project_name

REDACTED = "[redacted]"


class UnknownLogSourceError(LookupError):
    """No such environment, or the service is not part of it."""


class TooManyLogStreamsError(RuntimeError):
    pass


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every exact occurrence of a generated secret. No heuristics: passwords an
    application prints itself (e.g. qBittorrent's first password) are the user's to read."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


async def _redacted(
    lines: AsyncIterator[LogLine], secrets: Sequence[str]
) -> AsyncGenerator[LogLine, None]:
    async for line in lines:
        yield LogLine(timestamp=line.timestamp, text=redact(line.text, secrets))


class LogStreamer:
    def __init__(self, *, workspaces: WorkspaceManager, engine: Engine, max_streams: int) -> None:
        self._workspaces = workspaces
        self._engine = engine
        self._max_streams = max_streams
        self._open = 0

    @asynccontextmanager
    async def open(
        self, project: str, service: str, *, tail: int
    ) -> AsyncIterator[AsyncGenerator[LogLine, None]]:
        """Reserve a stream slot and yield the redacted lines of one service."""
        if self._open >= self._max_streams:
            raise TooManyLogStreamsError
        self._open += 1  # before any await: concurrent callers see the slot taken
        try:
            services, stack = await asyncio.to_thread(self._resolve, project)
            if service not in services:
                raise UnknownLogSourceError(service)
            secrets = await asyncio.to_thread(self._workspaces.read_secret_values, project)
            async with (
                aclosing(self._engine.logs(stack, service, tail=tail)) as raw,
                aclosing(_redacted(raw, secrets)) as lines,
            ):
                yield lines
        finally:
            self._open -= 1

    def _resolve(self, project: str) -> tuple[list[str], StackHandle]:
        workspace = self._workspaces.get(project)
        if workspace is None:
            raise UnknownLogSourceError(project)
        services = self._workspaces.read_compose(project).get("services")
        stack = StackHandle(
            project=project,
            compose_project=compose_project_name(project),
            compose_file=workspace.compose_file,
            edge_network=None,
        )
        return (list(services) if isinstance(services, dict) else []), stack
