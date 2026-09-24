"""Engine used for local development without Docker, and in tests.

It exercises the full pipeline (validation, rendering, workspace files, event
stream) and only fakes the calls to Docker.
"""

import asyncio
from collections.abc import Sequence

import yaml

from app.engine.base import LogSink, ServiceStatus, StackHandle


class SimulatedEngine:
    def __init__(self, *, delay: float) -> None:
        self._delay = delay
        self._stopped: set[str] = set()  # compose projects stopped by stop()

    async def pull(self, stack: StackHandle, log: LogSink) -> None:
        for service, image in (await self._images(stack)).items():
            log(f"[simulated] {service}: pulling {image}")
            await asyncio.sleep(self._delay)

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        log(f"[simulated] networks and containers created for {stack.compose_project}")
        if stack.edge_network:
            log(f"Reverse proxy attached to {stack.edge_network}")
        await asyncio.sleep(self._delay)

    async def start(self, stack: StackHandle, log: LogSink) -> None:
        for service in await self._images(stack):
            log(f"[simulated] container {stack.compose_project}-{service}-1 started")
            await asyncio.sleep(self._delay)

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
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

    @staticmethod
    async def _images(stack: StackHandle) -> dict[str, str]:
        text = await asyncio.to_thread(stack.compose_file.read_text, encoding="utf-8")
        services = yaml.safe_load(text)["services"]
        return {name: str(service["image"]) for name, service in services.items()}
