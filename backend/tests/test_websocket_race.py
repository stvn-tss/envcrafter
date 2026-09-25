"""Regression tests for `_race`'s behaviour when the handler task is cancelled from
outside (server shutdown, or an ASGI server tearing the connection down) while the
producer is still active. `TestClient`-based tests never exercise this path directly:
they always run the whole ASGI stack, and the client always disconnects from its own
side. Here the handlers are invoked directly, bypassing FastAPI's dependency
injection and routing, with fake collaborators standing in for the WebSocket, the
event bus and the engine.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.websocket import stream_container_logs, stream_job_events
from app.engine.base import LogLine
from app.services.log_streams import LogStreamer
from app.workspace.manager import WorkspaceManager

_SETTINGS = SimpleNamespace(allowed_origins=["http://localhost:8000"], log_tail_max=1000)


class _FinalizingEngine:
    """Stands in for the Docker engine. `finalized` counts how many times the log
    generator's `finally` ran; the real engine kills the `docker compose logs`
    subprocess there."""

    def __init__(self) -> None:
        self.finalized = 0

    async def logs(
        self, stack: object, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        try:
            yield LogLine(timestamp=None, text="line 1")
            await asyncio.Event().wait()  # follow mode: blocks until cancelled
        finally:
            self.finalized += 1


class _FinalizingBus:
    """Stands in for the event bus. `finalized` counts how many times the
    subscription generator's `finally` ran; the real bus unregisters the
    subscriber queue there."""

    def __init__(self) -> None:
        self.finalized = 0

    async def subscribe(self, job_id: object, after_seq: int) -> AsyncGenerator[object, None]:
        try:
            yield SimpleNamespace(model_dump_json=lambda: "{}")
            await asyncio.Event().wait()  # the job is still running
        finally:
            self.finalized += 1


class _FakeWebSocket:
    """Accepts, records the close code, and otherwise never disconnects on its own:
    every test here cancels the handler task from outside instead, exactly like a
    server shutdown or an ASGI server tearing the connection down would."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {"origin": "http://localhost:8000"}
        self.close_code: int | None = None
        self.first_frame = asyncio.Event()
        self._gone = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code

    async def send_text(self, text: str) -> None:
        self.first_frame.set()

    async def receive(self) -> dict[str, object]:
        await self._gone.wait()  # never set in these tests
        return {"type": "websocket.disconnect", "code": 1001}


def _workspace(root: Path) -> WorkspaceManager:
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "compose.yaml").write_text(
        "name: ec-demo\nservices:\n  app:\n    image: busybox\n", encoding="utf-8"
    )
    (root / "demo" / ".env").write_text("EC_PROJECT=demo\n", encoding="utf-8")
    return WorkspaceManager(root)


@pytest.mark.anyio
async def test_cancelling_the_logs_handler_propagates_and_cleans_up(tmp_path: Path) -> None:
    engine = _FinalizingEngine()
    streamer = LogStreamer(
        workspaces=_workspace(tmp_path),
        engine=engine,  # type: ignore[arg-type]
        max_streams=4,
    )
    ws = _FakeWebSocket()
    handler = asyncio.create_task(
        stream_container_logs(
            ws,  # type: ignore[arg-type]
            project="demo",
            service="app",
            streamer=streamer,
            settings=_SETTINGS,  # type: ignore[arg-type]
            tail=10,
        )
    )
    await ws.first_frame.wait()  # the handler has sent one line and is now following
    await asyncio.sleep(0)
    handler.cancel()
    await asyncio.wait([handler])

    assert handler.cancelled(), "CancelledError must propagate out of the handler, not be swallowed"
    assert engine.finalized == 1, "the log generator's cleanup must run before the handler ends"
    assert streamer._open == 0, "the concurrency slot must be released"


@pytest.mark.anyio
async def test_cancelling_the_job_stream_handler_propagates_and_cleans_up() -> None:
    bus = _FinalizingBus()
    ws = _FakeWebSocket()
    handler = asyncio.create_task(
        stream_job_events(
            ws,  # type: ignore[arg-type]
            job_id=uuid4(),
            bus=bus,  # type: ignore[arg-type]
            settings=_SETTINGS,  # type: ignore[arg-type]
            after_seq=0,
        )
    )
    await ws.first_frame.wait()  # the handler has sent one event and is now waiting
    await asyncio.sleep(0)
    handler.cancel()
    await asyncio.wait([handler])

    assert handler.cancelled(), "CancelledError must propagate out of the handler, not be swallowed"
    assert bus.finalized == 1, "the subscription's cleanup must run before the handler ends"
