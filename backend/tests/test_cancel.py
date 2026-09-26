"""Cancelling a running job: deployments roll back like failures, analyses just stop."""

import asyncio
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.services.orchestrator as orchestrator_module
from app.core.config import Settings
from app.engine.base import LogSink, ProgressSink, StackHandle
from app.engine.simulated import SimulatedEngine
from app.translator.spec import StackSpec
from app.workspace.manager import WorkspaceManager
from tests.conftest import ClientFactory, FakeTranslator, collect_events, spec

SHOP = {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"}
ORIGIN = {"Origin": "http://localhost:8000"}


def _events(client: TestClient, job: dict[str, Any]) -> list[dict[str, Any]]:
    with client.websocket_connect(job["events_url"]) as ws:
        return collect_events(ws)


def _started(events: list[dict[str, Any]]) -> list[str]:
    return [event["step_key"] for event in events if event["type"] == "step.started"]


def _cancel(client: TestClient, job: dict[str, Any]) -> Any:
    return client.post(f"/api/jobs/{job['job_id']}/cancel", headers=ORIGIN)


def test_cancelling_a_download_rolls_the_deployment_back(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pulling, release = threading.Event(), threading.Event()

    async def slow_pull(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, progress: ProgressSink
    ) -> None:
        pulling.set()
        # Set by the test thread: wait in a worker thread, never on the event loop.
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(SimulatedEngine, "pull", slow_pull)
    job = client.post("/api/jobs", json=SHOP).json()
    try:
        assert pulling.wait(timeout=10)
        response = _cancel(client, job)
        again = _cancel(client, job)
        events = _events(client, job)
    finally:
        release.set()

    assert response.status_code == 202, response.text
    assert response.json()["cancel_requested"] is True
    assert again.status_code == 409
    assert events[-1]["type"] == "job.cancelled"
    assert (
        events[-1]["message"]
        == "Deployment of 'shop' cancelled: everything created for it was removed"
    )
    messages = [event["message"] for event in events]
    assert "Rolling back: removing everything created for this project" in messages
    assert "container_deploy" not in _started(events)
    assert [event["type"] for event in events].count("job.cancelled") == 1
    assert not (settings.workspaces_dir / "shop").exists()
    assert client.get(f"/api/jobs/{job['job_id']}").json()["status"] == "cancelled"
    assert client.get("/api/environments").json() == {"environments": []}
    assert client.post("/api/jobs", json=SHOP).status_code == 202  # the name is free again


def test_a_workspace_step_finishes_before_the_job_stops(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace files are written from a thread: interrupting the step could leave files
    behind, so it finishes and the job stops before the next step instead."""
    writing, release = threading.Event(), threading.Event()
    original = WorkspaceManager.create

    async def slow_create(self: WorkspaceManager, *args: Any, **kwargs: Any) -> Any:
        writing.set()
        await asyncio.to_thread(release.wait, 10)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(WorkspaceManager, "create", slow_create)
    job = client.post("/api/jobs", json=SHOP).json()
    try:
        assert writing.wait(timeout=10)
        response = _cancel(client, job)
    finally:
        release.set()
    events = _events(client, job)

    assert response.status_code == 202
    assert events[-1]["type"] == "job.cancelled"
    assert _started(events)[-1] == "workspace_setup"  # the download never started
    assert not (settings.workspaces_dir / "shop").exists()


def test_cancel_after_the_last_step_still_rolls_back(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel accepted while an uninterruptible last step runs wins over the success:
    the user was told the cancellation was accepted, so the environment is not kept."""
    monkeypatch.setattr(orchestrator_module, "_INTERRUPTIBLE_STEPS", frozenset())
    starting, release = threading.Event(), threading.Event()

    async def gated_start(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, **kwargs: Any
    ) -> None:
        starting.set()
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(SimulatedEngine, "start", gated_start)
    job = client.post("/api/jobs", json=SHOP).json()
    try:
        assert starting.wait(timeout=10)
        response = _cancel(client, job)
    finally:
        release.set()
    events = _events(client, job)

    assert response.status_code == 202
    assert events[-1]["type"] == "job.cancelled"
    assert "job.succeeded" not in [event["type"] for event in events]
    assert not (settings.workspaces_dir / "shop").exists()


def test_cancelling_an_analysis_stores_no_plan(make_client: ClientFactory) -> None:
    thinking, release = threading.Event(), threading.Event()

    class _Slow(FakeTranslator):
        async def translate(self, prompt: str) -> StackSpec:
            thinking.set()
            await asyncio.to_thread(release.wait, 10)
            return await super().translate(prompt)

    client = make_client(_Slow(spec(decision="template", template_id="owasp-juice-shop")))
    job = client.post("/api/plans", json={"prompt": "A vulnerable shop"}).json()
    try:
        assert thinking.wait(timeout=10)
        response = _cancel(client, job)
        events = _events(client, job)
    finally:
        release.set()

    assert response.status_code == 202
    assert events[-1]["type"] == "job.cancelled"
    assert events[-1]["message"] == "Analysis cancelled"
    assert events[-1]["plan_id"] is None
    assert client.get(f"/api/jobs/{job['job_id']}").json()["plan_id"] is None


def test_cancel_is_refused_when_it_cannot_apply(client: TestClient) -> None:
    unknown = client.post("/api/jobs/00000000-0000-0000-0000-000000000000/cancel", headers=ORIGIN)
    finished_job = client.post("/api/jobs", json=SHOP).json()
    _events(client, finished_job)
    finished = _cancel(client, finished_job)
    stop_job = client.post("/api/environments/shop/actions", json={"action": "stop"}).json()
    lifecycle = _cancel(client, stop_job)
    _events(client, stop_job)
    foreign = client.post(
        f"/api/jobs/{finished_job['job_id']}/cancel", headers={"Origin": "https://evil.example"}
    )
    malformed = client.post("/api/jobs/not-a-uuid/cancel", headers=ORIGIN)

    assert unknown.status_code == 404
    assert (finished.status_code, finished.json()["detail"]) == (409, "This job is already over.")
    assert (lifecycle.status_code, lifecycle.json()["detail"]) == (
        409,
        "Only deployments and AI analyses can be cancelled.",
    )
    assert foreign.status_code == 403
    assert malformed.status_code == 422
