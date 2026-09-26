"""Engine used for local development without Docker, and in tests.

It exercises the full pipeline (validation, rendering, workspace files, event
stream) and only fakes the calls to Docker.
"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime

import yaml

from app.engine.base import (
    HostResources,
    LogLine,
    LogSink,
    ProgressSink,
    RuntimeCheck,
    ServiceStatus,
    StackHandle,
)

# Plausible numbers for the UI; nothing is measured.
_RESOURCES = HostResources(
    memory_total_mb=8192, memory_available_mb=6144, disk_free_mb=51200, disk_is_virtual=False
)


class SimulatedEngine:
    def __init__(self, *, delay: float) -> None:
        self._delay = delay
        self._stopped: set[str] = set()  # compose projects stopped by stop()
        self._pulled: set[str] = set()  # images "downloaded" by this process

    async def pull(self, stack: StackHandle, log: LogSink, progress: ProgressSink) -> None:
        images = list((await self._images(stack)).items())
        for index, (service, image) in enumerate(images, start=1):
            log(f"[simulated] {service}: pulling {image}")
            await asyncio.sleep(self._delay)
            self._pulled.add(image)
            progress(index * 100 // len(images), f"{index} of {len(images)} images")

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        log(f"[simulated] networks and containers created for {stack.compose_project}")
        if stack.edge_network:
            log(f"Reverse proxy attached to {stack.edge_network}")
        await asyncio.sleep(self._delay)

    async def start(self, stack: StackHandle, log: LogSink, *, service: str | None = None) -> None:
        if service is None:
            self._stopped.discard(stack.compose_project)
        for name in [service] if service is not None else await self._images(stack):
            log(f"[simulated] container {stack.compose_project}-{name}-1 started")
            await asyncio.sleep(self._delay)

    async def stop(self, stack: StackHandle, log: LogSink, *, service: str | None = None) -> None:
        if service is None:
            self._stopped.add(stack.compose_project)
        for name in [service] if service is not None else await self._images(stack):
            log(f"[simulated] container {stack.compose_project}-{name}-1 stopped")
            await asyncio.sleep(self._delay)

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
        self._stopped.discard(stack.compose_project)
        log(f"[simulated] project {stack.compose_project} removed")
        await asyncio.sleep(self._delay)

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        # Simulated containers survive restarts of the dev server, like Docker ones.
        result: dict[str, list[ServiceStatus]] = {}
        for stack in stacks:
            try:
                services = await self._images(stack)
            except OSError:
                result[stack.project] = []
                continue
            state = "exited" if stack.compose_project in self._stopped else "running"
            result[stack.project] = [
                ServiceStatus(service=name, state=state, health=None) for name in services
            ]
        return result

    async def recent_logs(self, stack: StackHandle, service: str, *, tail: int) -> list[LogLine]:
        now = datetime.now(UTC)
        return [
            LogLine(timestamp=now, text=f"[simulated] {service}: log line {index + 1}")
            for index in range(min(tail, 5))
        ]

    async def logs(
        self, stack: StackHandle, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        now = datetime.now(UTC)
        for index in range(min(tail, 5)):
            yield LogLine(timestamp=now, text=f"[simulated] {service}: log line {index + 1}")
        await asyncio.Event().wait()  # follow mode: wait until the viewer leaves

    async def missing_images(self, images: Sequence[str]) -> list[str]:
        return [image for image in dict.fromkeys(images) if image not in self._pulled]

    async def resources(self) -> HostResources:
        return _RESOURCES

    async def diagnose(self) -> list[RuntimeCheck]:
        return [
            RuntimeCheck("docker", True, "Simulated engine: nothing runs in Docker."),
            RuntimeCheck("proxy", True, "Simulated engine: no reverse proxy needed."),
        ]

    @staticmethod
    async def _images(stack: StackHandle) -> dict[str, str]:
        text = await asyncio.to_thread(stack.compose_file.read_text, encoding="utf-8")
        services = yaml.safe_load(text)["services"]
        return {name: str(service["image"]) for name, service in services.items()}
