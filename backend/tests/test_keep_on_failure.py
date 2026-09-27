"""keep_on_failure: a failed deployment stays in place for debugging instead of rolling back."""

import asyncio
import json
import threading
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import EngineError, LogSink, ProgressSink, ServiceStatus, StackHandle
from app.engine.simulated import SimulatedEngine
from tests.conftest import ClientFactory, FakeTranslator, collect_events, run_job, spec

ORIGIN = {"Origin": "http://localhost:8000"}
LAB = {"mode": "template", "template_id": "dvwa", "project_name": "lab"}


async def broken_start(
    self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
) -> None:
    raise EngineError("`docker compose up` failed (exit code 1)")


async def half_running(
    self: SimulatedEngine, stacks: Sequence[StackHandle]
) -> dict[str, list[ServiceStatus]]:
    return {
        stack.project: [
            ServiceStatus(service="dvwa", state="exited", health=None),
            ServiceStatus(service="mariadb", state="running", health="healthy"),
        ]
        for stack in stacks
    }


def _logs(events: list[dict[str, Any]]) -> str:
    return "\n".join(event["message"] for event in events)


def _action(client: TestClient, project: str, action: str) -> list[dict[str, Any]]:
    response = client.post(f"/api/environments/{project}/actions", json={"action": action})
    assert response.status_code == 202, response.text
    with client.websocket_connect(response.json()["events_url"]) as ws:
        return collect_events(ws)


def test_a_failed_deployment_is_kept_when_asked(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[str] = []

    async def recording_remove(self: SimulatedEngine, stack: StackHandle, log: LogSink) -> None:
        removed.append(stack.project)

    monkeypatch.setattr(SimulatedEngine, "remove", recording_remove)
    monkeypatch.setattr(SimulatedEngine, "start", broken_start)
    monkeypatch.setattr(SimulatedEngine, "status", half_running)

    events = run_job(client, {**LAB, "keep_on_failure": True})

    assert events[-1]["type"] == "job.failed"
    assert events[-1]["kept"] is True
    assert "Kept for debugging" in _logs(events)
    assert "Rolling back" not in _logs(events)
    assert removed == []
    assert (settings.workspaces_dir / "lab" / "compose.yaml").is_file()
    meta = json.loads((settings.workspaces_dir / "lab" / "meta.json").read_text(encoding="utf-8"))
    assert meta["failure"]["message"] == events[-1]["message"]
    view = client.get("/api/environments/lab").json()
    assert view["state"] == "failed"
    assert view["failure"]["message"] == events[-1]["message"]


def test_without_the_option_a_failure_still_rolls_back(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SimulatedEngine, "start", broken_start)

    events = run_job(client, LAB)

    assert events[-1]["type"] == "job.failed"
    assert events[-1]["kept"] is None
    assert not (settings.workspaces_dir / "lab").exists()


def test_a_failure_before_the_workspace_keeps_nothing(
    make_client: ClientFactory, settings: Settings
) -> None:
    rogue = spec(
        services=[
            {
                "name": "app",
                "image": "docker.io/library/nginx:1.29.0",  # not on the allow-list
                "purpose": "x",
                "environment": [],
                "volumes": [],
                "depends_on": [],
                "needs_internet": False,
            }
        ]
    )
    client = make_client(FakeTranslator(rogue))

    request = {"mode": "prompt", "prompt": "a web server", "project_name": "web"}
    events = run_job(client, {**request, "keep_on_failure": True})

    assert events[-1]["type"] == "job.failed"
    assert events[-1]["kept"] is None
    assert not (settings.workspaces_dir / "web").exists()


def test_a_cancelled_deployment_is_rolled_back_even_when_kept_on_failure(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pulling, release = threading.Event(), threading.Event()

    async def slow_pull(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, progress: ProgressSink
    ) -> None:
        pulling.set()
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(SimulatedEngine, "pull", slow_pull)
    job = client.post("/api/jobs", json={**LAB, "keep_on_failure": True}).json()
    try:
        assert pulling.wait(10)
        assert client.post(f"/api/jobs/{job['job_id']}/cancel", headers=ORIGIN).status_code == 202
        with client.websocket_connect(job["events_url"]) as ws:
            events = collect_events(ws)
    finally:
        release.set()

    assert events[-1]["type"] == "job.cancelled"
    assert not (settings.workspaces_dir / "lab").exists()


def test_a_successful_start_clears_the_failure_mark(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a kept failure, fixed and then stopped, would read "Failed" forever."""
    calls = {"start": 0}
    real_start = SimulatedEngine.start

    async def fails_once(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
    ) -> None:
        calls["start"] += 1
        if calls["start"] == 1:
            raise EngineError("`docker compose up` failed (exit code 1)")
        await real_start(self, stack, log, service=service)

    monkeypatch.setattr(SimulatedEngine, "start", fails_once)
    assert run_job(client, {**LAB, "keep_on_failure": True})[-1]["kept"] is True

    started = _action(client, "lab", "start")
    assert started[-1]["type"] == "job.succeeded", started[-1]["message"]
    assert "The failed deployment mark is cleared" in _logs(started)
    assert _action(client, "lab", "stop")[-1]["type"] == "job.succeeded"

    view = client.get("/api/environments/lab").json()
    assert view["state"] == "stopped"
    assert view["failure"] is None


def test_keep_on_failure_is_a_boolean(client: TestClient) -> None:
    response = client.post("/api/jobs", json={**LAB, "keep_on_failure": "sometimes"})
    assert response.status_code == 422
