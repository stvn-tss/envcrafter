"""WebSocket endpoints streaming job progress and container logs to the browser.

Job progress
------------
    connect   WS /ws/jobs/{job_id}?after_seq=N     (N = last seq rendered, 0 on first connect)
    receive   JSON text frames, one JobEvent each, in strictly increasing `seq` order
    close     1000 job finished (terminal event delivered)       -> do not reconnect
              1008 policy violation (origin, client data, params) -> do not reconnect
              1013 subscriber lagged                               -> reconnect now with after_seq
              4404 unknown or expired job                          -> do not reconnect
    any other close (1006 network drop, server restart...)         -> reconnect with backoff

Container logs
--------------
    connect   WS /ws/environments/{project}/logs?service=<name>&tail=<0..log_tail_max, default 200>
    receive   JSON text frames: {"type": "line", "timestamp": <iso|null>, "text": str}, then,
              once the process ends, {"type": "end", "message": "Log stream ended."}
    close     1000 log stream ended (process exited)               -> do not reconnect
              1008 policy violation (origin, client data, params)   -> do not reconnect
              1013 too many concurrent log streams                 -> reconnect later
              4404 unknown environment or service                  -> do not reconnect
    any other close (1006 network drop, server restart...)         -> reconnect with backoff

Both channels are read-only for clients: any data frame from the client closes the
connection with 1008, and a client disconnect stops the underlying work (bus
unsubscribe, or killing the `docker compose logs --follow` subprocess).
"""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import aclosing
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, WebSocket, WebSocketDisconnect, status

from app.api.deps import EventBusDep, LogStreamerDep, SettingsDep
from app.core.security import is_origin_allowed
from app.engine.base import LogLine
from app.models.common import PROJECT_NAME_PATTERN
from app.models.environment import LogEndFrame, LogLineFrame
from app.models.template import SERVICE_NAME_PATTERN
from app.services.event_bus import JobEventBus, SubscriberLaggedError, UnknownJobError
from app.services.log_streams import TooManyLogStreamsError, UnknownLogSourceError

router = APIRouter()

WS_CLOSE_UNKNOWN = 4404  # unknown or expired job, environment or service


@dataclass(frozen=True)
class _Outcome:
    client_left: bool  # the client disconnected, or broke the protocol (close_code set)
    close_code: int | None
    error: BaseException | None  # producer failure; None when it finished normally


async def _race(websocket: WebSocket, producer: Coroutine[Any, Any, None]) -> _Outcome:
    """Run the producer until it ends or the client goes away, whichever comes first.

    The listener notices a closed tab at once instead of on the next send(), which may
    be minutes away; cancelling the producer runs its cleanup (unsubscribe, kill...).
    """
    sender = asyncio.create_task(producer)
    listener = asyncio.create_task(_wait_for_client_exit(websocket))
    try:
        done, pending = await asyncio.wait({sender, listener}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        # This coroutine was itself cancelled from outside (the ASGI server tearing down
        # the connection, e.g. a test client whose teardown forces the handler to stop
        # right after it hands us the disconnect it already queued): request cancellation
        # of both tasks and return at once, with nothing left to send back since the
        # transport is already gone. Do NOT await anything else here: the enclosing scope
        # keeps re-delivering this same cancellation to us on every checkpoint until we
        # stop awaiting entirely, so any further `await` in this handler would just be
        # cancelled again. Each task still runs its own `finally`/`aclosing` cleanup on
        # its own schedule once the event loop gets back to it.
        sender.cancel()
        listener.cancel()
        return _Outcome(client_left=True, close_code=None, error=None)
    if listener in done:
        return _Outcome(client_left=True, close_code=listener.result(), error=None)
    return _Outcome(client_left=False, close_code=None, error=sender.exception())


@router.websocket("/ws/jobs/{job_id}")
async def stream_job_events(
    websocket: WebSocket,
    job_id: UUID,  # non-UUID values are rejected by FastAPI with close code 1008
    bus: EventBusDep,
    settings: SettingsDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> None:
    # SECURITY: browsers do not apply CORS to WebSockets. Without this check,
    # any website open in the user's browser could silently connect to
    # ws://localhost:8000 and read deployment logs (Cross-Site WebSocket
    # Hijacking). Closing before accept() rejects the handshake with HTTP 403.
    if not is_origin_allowed(websocket.headers.get("origin"), settings.allowed_origins):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    outcome = await _race(websocket, _forward_events(websocket, bus, job_id, after_seq))
    if outcome.client_left:
        if outcome.close_code is not None:  # client broke the protocol; socket still open
            await websocket.close(code=outcome.close_code)
        return  # otherwise the client is already gone: nothing left to close

    error = outcome.error
    if error is None:
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="job finished")
    elif isinstance(error, SubscriberLaggedError):
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="lagged")
    elif isinstance(error, UnknownJobError):
        await websocket.close(code=WS_CLOSE_UNKNOWN, reason="unknown job")
    elif isinstance(error, WebSocketDisconnect):
        return  # send failed because the client left between two frames
    else:
        raise error


async def _forward_events(
    websocket: WebSocket, bus: JobEventBus, job_id: UUID, after_seq: int
) -> None:
    # aclosing() guarantees that the generator's `finally` (removing our queue
    # from the channel) runs as soon as we stop iterating, including when this
    # task is cancelled, instead of whenever the generator is garbage-collected.
    async with aclosing(bus.subscribe(job_id, after_seq)) as events:
        async for event in events:
            # Only the typed, whitelisted JobEvent fields are serialized: no
            # exception text or internal object can leak through this channel.
            await websocket.send_text(event.model_dump_json())


@router.websocket("/ws/environments/{project}/logs")
async def stream_container_logs(
    websocket: WebSocket,
    project: Annotated[str, Path(pattern=PROJECT_NAME_PATTERN)],
    service: Annotated[str, Query(pattern=SERVICE_NAME_PATTERN)],
    streamer: LogStreamerDep,
    settings: SettingsDep,
    tail: Annotated[int, Query(ge=0)] = 200,
) -> None:
    # SECURITY: same Cross-Site WebSocket Hijacking guard as the job stream.
    if not is_origin_allowed(websocket.headers.get("origin"), settings.allowed_origins):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        async with streamer.open(project, service, tail=min(tail, settings.log_tail_max)) as lines:
            outcome = await _race(websocket, _forward_lines(websocket, lines))
    except UnknownLogSourceError:
        await websocket.close(code=WS_CLOSE_UNKNOWN, reason="unknown environment or service")
        return
    except TooManyLogStreamsError:
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="too many log streams")
        return

    if outcome.client_left:
        if outcome.close_code is not None:
            await websocket.close(code=outcome.close_code)
        return
    if outcome.error is None:
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="log stream ended")
    elif not isinstance(outcome.error, WebSocketDisconnect):
        raise outcome.error


async def _forward_lines(websocket: WebSocket, lines: AsyncIterator[LogLine]) -> None:
    async for line in lines:
        frame = LogLineFrame(timestamp=line.timestamp, text=line.text)
        await websocket.send_text(frame.model_dump_json())
    await websocket.send_text(LogEndFrame(message="Log stream ended.").model_dump_json())


async def _wait_for_client_exit(websocket: WebSocket) -> int | None:
    """Return None when the client disconnects, or a close code if it misbehaves.

    Both channels are read-only for clients. Any data frame is unexpected, so the
    connection is closed instead of parsing input we have no schema for.
    (Future client commands, e.g. "cancel", will get a pydantic model first.)
    """
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    return status.WS_1008_POLICY_VIOLATION
