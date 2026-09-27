import asyncio
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import EngineError, LogSink, StackHandle
from app.engine.simulated import SimulatedEngine
from tests.conftest import ClientFactory, collect_events, run_job

SHOP = {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"}


def act(client: TestClient, project: str, action: str) -> list[dict[str, Any]]:
    response = client.post(f"/api/environments/{project}/actions", json={"action": action})
    assert response.status_code == 202, response.text
    assert response.json()["mode"] == action
    with client.websocket_connect(response.json()["events_url"]) as ws:
        return collect_events(ws)


def state(client: TestClient, project: str) -> str:
    value: str = client.get(f"/api/environments/{project}").json()["state"]
    return value


def test_stop_then_start_changes_the_environment_state(client: TestClient) -> None:
    run_job(client, SHOP)

    stopped = act(client, "shop", "stop")
    assert [step["key"] for step in stopped[0]["plan"]] == ["container_stop"]
    assert stopped[0]["message"] == "Stop of 'shop' accepted"
    assert stopped[-1]["type"] == "job.succeeded"
    assert stopped[-1]["message"] == "Environment 'shop' is stopped"
    assert state(client, "shop") == "stopped"

    started = act(client, "shop", "start")
    assert [step["key"] for step in started[0]["plan"]] == ["container_start"]
    assert started[-1]["message"] == "Environment 'shop' is running at http://shop.localhost"
    assert started[-1]["urls"][0]["url"] == "http://shop.localhost"
    assert state(client, "shop") == "running"


def test_restart_stops_then_starts(client: TestClient) -> None:
    run_job(client, SHOP)
    events = act(client, "shop", "restart")
    assert [step["key"] for step in events[0]["plan"]] == ["container_stop", "container_start"]
    assert events[0]["message"] == "Restart of 'shop' accepted"
    assert events[-1]["type"] == "job.succeeded"
    assert state(client, "shop") == "running"


def test_lifecycle_input_validation(client: TestClient) -> None:
    ghost = client.post("/api/environments/ghost/actions", json={"action": "stop"})
    assert ghost.status_code == 404
    bad_name = client.post("/api/environments/Bad_Name/actions", json={"action": "stop"})
    assert bad_name.status_code == 422
    run_job(client, SHOP)
    for payload in ({"action": "exec"}, {"action": "stop", "command": "id"}, {}):
        assert client.post("/api/environments/shop/actions", json=payload).status_code == 422
    foreign = client.post(
        "/api/environments/shop/actions",
        json={"action": "stop"},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    not_json = client.post(
        "/api/environments/shop/actions",
        content=b'{"action": "stop"}',
        headers={"Content-Type": "text/plain"},
    )
    assert not_json.status_code == 415


def test_one_job_at_a_time_per_environment(make_client: ClientFactory, settings: Settings) -> None:
    settings.simulated_step_delay = 0.3
    client = make_client()
    run_job(client, SHOP)

    first = client.post("/api/environments/shop/actions", json={"action": "stop"})

    assert first.status_code == 202
    second = client.post("/api/environments/shop/actions", json={"action": "start"})
    assert second.status_code == 409
    assert client.delete("/api/environments/shop").status_code == 409


def test_failed_start_never_deletes_the_environment(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_job(client, SHOP)

    async def broken_start(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
    ) -> None:
        raise EngineError("`docker compose up` failed (exit code 1)")

    monkeypatch.setattr(SimulatedEngine, "start", broken_start)
    events = act(client, "shop", "start")

    assert events[-1]["type"] == "job.failed"
    assert events[-1]["message"] == "`docker compose up` failed (exit code 1)"
    assert (settings.workspaces_dir / "shop" / "compose.yaml").is_file()
    assert "Rolling back" not in "\n".join(event["message"] for event in events)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/environments/shop/actions", {"action": "start"}),
        ("DELETE", "/api/environments/shop", None),
    ],
)
def test_rollback_keeps_the_environment_reserved(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    """A failed deployment stays the project's active job until its rollback is over: a
    Start or a Remove landing while `compose down` runs would race the workspace deletion
    and leave containers that no workspace describes."""
    rolling_back = threading.Event()
    release = threading.Event()

    async def broken_start(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
    ) -> None:
        raise EngineError("`docker compose up` failed (exit code 1)")

    async def slow_remove(self: SimulatedEngine, stack: StackHandle, log: LogSink) -> None:
        rolling_back.set()
        # Set by the test thread: wait in a worker thread, never on the event loop.
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(SimulatedEngine, "start", broken_start)
    monkeypatch.setattr(SimulatedEngine, "remove", slow_remove)
    job = client.post("/api/jobs", json=SHOP).json()
    try:
        assert rolling_back.wait(timeout=10)
        [during] = client.get("/api/environments").json()["environments"]
        conflict = client.request(method, path, json=body)
    finally:
        release.set()

    assert conflict.status_code == 409, conflict.text
    assert during["job"] == {
        "job_id": job["job_id"],
        "mode": "template",
        "events_url": job["events_url"],
        "service": None,
        "cancel_requested": False,
    }
    with client.websocket_connect(job["events_url"]) as ws:
        events = collect_events(ws)
    assert events[-1]["type"] == "job.failed"
    assert not (settings.workspaces_dir / "shop").exists()
    assert client.get("/api/environments").json() == {"environments": []}


MEDIA = {"mode": "template", "template_id": "media-stack", "project_name": "media"}


def test_restart_one_service_leaves_the_others_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_job(client, MEDIA)
    calls: list[tuple[str, str | None]] = []
    original_stop, original_start = SimulatedEngine.stop, SimulatedEngine.start

    async def stop(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
    ) -> None:
        calls.append(("stop", service))
        await original_stop(self, stack, log, service=service)

    async def start(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
    ) -> None:
        calls.append(("start", service))
        await original_start(self, stack, log, service=service)

    monkeypatch.setattr(SimulatedEngine, "stop", stop)
    monkeypatch.setattr(SimulatedEngine, "start", start)
    response = client.post(
        "/api/environments/media/actions", json={"action": "restart", "service": "sonarr"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["service"] == "sonarr"
    with client.websocket_connect(response.json()["events_url"]) as ws:
        events = collect_events(ws)

    assert [step["label"] for step in events[0]["plan"]] == [
        "Container shutdown (sonarr)",
        "Container startup (sonarr)",
    ]
    assert events[0]["message"] == "Restart of 'sonarr' in 'media' accepted"
    assert events[-1]["type"] == "job.succeeded"
    assert events[-1]["message"] == "Service 'sonarr' of 'media' is running"
    assert calls == [("stop", "sonarr"), ("start", "sonarr")]
    assert state(client, "media") == "running"


def test_restart_one_service_validation(client: TestClient) -> None:
    run_job(client, SHOP)
    url = "/api/environments/shop/actions"
    unknown = client.post(url, json={"action": "restart", "service": "sonarr"})
    stop_one = client.post(url, json={"action": "stop", "service": "juice-shop"})
    malformed = client.post(url, json={"action": "restart", "service": "Juice Shop"})
    ghost = client.post(
        "/api/environments/ghost/actions", json={"action": "restart", "service": "app"}
    )

    assert (unknown.status_code, unknown.json()["detail"]) == (404, "Unknown service")
    assert stop_one.status_code == 422
    assert malformed.status_code == 422
    assert (ghost.status_code, ghost.json()["detail"]) == (404, "Unknown environment")
    # The refused requests left no job behind: a whole restart is accepted at once.
    assert client.post(url, json={"action": "restart"}).status_code == 202
