"""Engine readings behind the readiness endpoints, shared for a few seconds.

Any web page can send GET requests to the local API, and each reading starts docker
processes. Like the inventory's `docker ps`, a reading is computed once at a time per
key (single-flight) and reused for `cache_seconds`; keys are bounded (the system, and
templates resolved through the catalog).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar, cast

from app.engine.base import Engine, HostResources, RuntimeCheck

T = TypeVar("T")


class ReadinessProbe:
    def __init__(
        self,
        *,
        engine: Engine,
        cache_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def system(self) -> tuple[list[RuntimeCheck], HostResources]:
        """Docker and reverse-proxy checks, and the resources left."""
        return await self._cached("system", self._read_system)

    async def template(self, template_id: str, images: Sequence[str]) -> tuple[int, HostResources]:
        """How many of the template's images are missing, and the resources left."""
        return await self._cached(f"template:{template_id}", lambda: self._read_template(images))

    async def _read_system(self) -> tuple[list[RuntimeCheck], HostResources]:
        runtime, resources = await asyncio.gather(self._engine.diagnose(), self._engine.resources())
        return list(runtime), resources

    async def _read_template(self, images: Sequence[str]) -> tuple[int, HostResources]:
        missing, resources = await asyncio.gather(
            self._engine.missing_images(images), self._engine.resources()
        )
        return len(missing), resources

    async def _cached(self, key: str, read: Callable[[], Awaitable[T]]) -> T:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None and self._clock() - entry[0] < self._cache_seconds:
                return cast(T, entry[1])
            value = await read()
            self._entries[key] = (self._clock(), value)
            return value
