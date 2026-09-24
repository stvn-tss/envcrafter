"""WebSocket endpoint streaming a job's progress events to the browser.

Protocol (server -> client only)
--------------------------------
    connect   WS /ws/jobs/{job_id}?after_seq=N     (N = last seq rendered, 0 on first connect)
    receive   JSON text frames, one JobEvent each, in strictly increasing `seq` order
    close     1000 job finished (terminal event delivered)       -> do not reconnect
              1008 policy violation (origin, client data, params) -> do not reconnect
              1013 subscriber lagged                               -> reconnect now with after_seq
              4404 unknown or expired job                          -> do not reconnect
    any other close (1006 network drop, server restart...)         -> reconnect with backoff
"""

import asyncio
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.api.deps import EventBusDep, SettingsDep
from app.core.security import is_origin_allowed
from app.services.event_bus import JobEventBus, SubscriberLaggedError, UnknownJobError

router = APIRouter()

WS_CLOSE_UNKNOWN_JOB = 4404  # application-defined range: 4000-4999


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

    # Two concurrent tasks per connection:
    #  - sender:   pumps events from the bus to the socket;
    #  - listener: waits for the client to go away.
    # Without the listener, a closed tab would only be noticed on the next
    # send(), which may be minutes away for a slow step. Meanwhile its
    # subscriber queue would stay registered for nothing.
    sender = asyncio.create_task(_forward_events(websocket, bus, job_id, after_seq))
    listener = asyncio.create_task(_wait_for_client_exit(websocket))
    done, pending = await asyncio.wait({sender, listener}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()  # cancelling the sender runs the bus unsubscribe via aclosing()
    await asyncio.gather(*pending, return_exceptions=True)

    if listener in done:
        close_code = listener.result()
        if close_code is not None:  # client broke the protocol; socket still open
            await websocket.close(code=close_code)
        return  # otherwise the client is already gone: nothing left to close

    error = sender.exception()
    if error is None:
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="job finished")
    elif isinstance(error, SubscriberLaggedError):
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="lagged")
    elif isinstance(error, UnknownJobError):
        await websocket.close(code=WS_CLOSE_UNKNOWN_JOB, reason="unknown job")
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


async def _wait_for_client_exit(websocket: WebSocket) -> int | None:
    """Return None when the client disconnects, or a close code if it misbehaves.

    This channel is read-only for clients. Any data frame is unexpected, so the
    connection is closed instead of parsing input we have no schema for.
    (Future client commands, e.g. "cancel", will get a pydantic model first.)
    """
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    return status.WS_1008_POLICY_VIOLATION
