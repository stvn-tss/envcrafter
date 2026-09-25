import asyncio
from collections.abc import Iterator, Sequence
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import LogSink, ServiceStatus, StackHandle
from app.engine.simulated import SimulatedEngine
from app.models.deployment import EventType, PlanStep
from app.services.event_bus import JobEventBus
from app.services.orchestrator import Job, StepContext, health_summary
from tests.conftest import ClientFactory, run_job


@pytest.mark.anyio
async def test_history_keeps_only_the_latest_progress_per_step() -> None:
    bus = JobEventBus(history_size=100, queue_size=10)
    job_id = uuid4()
    bus.open_channel(job_id)
    bus.publish(job_id, EventType.STEP_STARTED, "pull", step_key="image_pull")
    for percent in (10, 20, 30):
        bus.publish(
            job_id, EventType.STEP_PROGRESS, f"{percent}%", step_key="image_pull", percent=percent
        )
    bus.publish(job_id, EventType.STEP_PROGRESS, "half", step_key="container_deploy", percent=50)
    bus.publish(job_id, EventType.JOB_FAILED, "stop")

    events = [event async for event in bus.subscribe(job_id)]

    assert [(event.type, event.percent) for event in events] == [
        (EventType.STEP_STARTED, None),
        (EventType.STEP_PROGRESS, 30),
        (EventType.STEP_PROGRESS, 50),
        (EventType.JOB_FAILED, None),
    ]
    assert [event.seq for event in events] == [1, 4, 5, 6]


@pytest.mark.anyio
async def test_live_subscribers_receive_every_progress_event() -> None:
    bus = JobEventBus(history_size=100, queue_size=10)
    job_id = uuid4()
    bus.open_channel(job_id)
    bus.publish(job_id, EventType.STEP_STARTED, "pull", step_key="image_pull")
    stream = bus.subscribe(job_id)
    assert (await anext(stream)).type == EventType.STEP_STARTED  # now registered live

    for percent in (10, 20, 30):
        bus.publish(
            job_id, EventType.STEP_PROGRESS, f"{percent}%", step_key="image_pull", percent=percent
        )
    bus.publish(job_id, EventType.JOB_FAILED, "stop")

    assert [event.percent async for event in stream] == [10, 20, 30, None]


class _RecordingBus:
    def __init__(self) -> None:
        self.percents: list[int | None] = []

    def publish(self, job_id: Any, event_type: EventType, message: str, **fields: Any) -> None:
        assert event_type == EventType.STEP_PROGRESS
        self.percents.append(fields["percent"])


def test_progress_is_throttled_but_completion_always_goes_out() -> None:
    bus = _RecordingBus()
    ticks: Iterator[float] = iter([0.0, 0.1, 0.2, 0.6, 0.65])
    ctx = StepContext(
        job=Job(id=uuid4(), mode="template", project_name="demo"),
        step=PlanStep(key="image_pull", label="Image download"),
        index=4,
        total=6,
        bus=bus,  # type: ignore[arg-type]
        progress_interval=0.5,
        clock=lambda: next(ticks),
    )

    for percent in (10, 20, 30, 140, 100):
        ctx.progress(percent, f"{percent}%")

    assert bus.percents == [10, 100, 100]  # 140 is clamped to 100 (always sent)


def test_health_summary_names_what_is_not_ready() -> None:
    observed = [
        ServiceStatus(service="glpi", state="running", health="starting"),
        ServiceStatus(service="mariadb", state="running", health="healthy"),
    ]
    assert health_summary(["glpi", "mariadb"], observed) == (
        50,
        "1 of 2 services ready · waiting for glpi",
    )
    assert health_summary(["glpi"], [ServiceStatus("glpi", "running", None)]) == (
        100,
        "1 of 1 services ready",
    )


def test_image_download_reports_progress(client: TestClient) -> None:
    events = run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "desk"})

    progress = [event for event in events if event["type"] == "step.progress"]

    assert progress
    assert progress[-1]["step_key"] == "image_pull"
    assert progress[-1]["percent"] == 100
    assert progress[-1]["message"] == "2 of 2 images"


class _SlowHealthEngine(SimulatedEngine):
    """Start takes a while; healthchecks turn healthy after a few polls."""

    def __init__(self) -> None:
        super().__init__(delay=0)
        self.polls = 0

    async def start(self, stack: StackHandle, log: LogSink) -> None:
        await asyncio.sleep(0.3)

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        self.polls += 1
        glpi_health = "starting" if self.polls < 3 else "healthy"
        return {
            stack.project: [
                ServiceStatus("glpi", "running", glpi_health),
                ServiceStatus("mariadb", "running", "healthy"),
            ]
            for stack in stacks
        }


def test_startup_reports_health_while_waiting(
    make_client: ClientFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _SlowHealthEngine()
    monkeypatch.setattr("app.main.build_engine", lambda _settings: engine)
    settings.health_poll_seconds = 0.05
    settings.progress_interval_seconds = 0
    client = make_client()

    events = run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "desk"})

    health = [
        e for e in events if e["type"] == "step.progress" and e["step_key"] == "container_deploy"
    ]
    assert engine.polls >= 1
    assert (
        health[-1]["message"].endswith("services ready")
        or "waiting for glpi" in health[-1]["message"]
    )
    assert events[-1]["type"] == "job.succeeded"
