"""Engine used for local development without Docker, and in tests.

It exercises the full pipeline (validation, rendering, workspace files, event
stream) and only fakes the calls to Docker.
"""

import asyncio

import yaml

from app.engine.base import LogSink, StackHandle


class SimulatedEngine:
    def __init__(self, *, delay: float) -> None:
        self._delay = delay

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

    @staticmethod
    async def _images(stack: StackHandle) -> dict[str, str]:
        text = await asyncio.to_thread(stack.compose_file.read_text, encoding="utf-8")
        services = yaml.safe_load(text)["services"]
        return {name: str(service["image"]) for name, service in services.items()}
