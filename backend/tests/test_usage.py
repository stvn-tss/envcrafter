"""GET /api/environments/{project}/usage: CPU and memory of each running service."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.engine.base import EngineError, ServiceUsage, StackHandle
from app.engine.simulated import SimulatedEngine
from tests.conftest import collect_events, run_job

LAB = {"mode": "template", "template_id": "dvwa", "project_name": "lab"}


def _action(client: TestClient, project: str, action: str) -> list[dict[str, Any]]:
    response = client.post(f"/api/environments/{project}/actions", json={"action": action})
    assert response.status_code == 202, response.text
    with client.websocket_connect(response.json()["events_url"]) as ws:
        return collect_events(ws)


def test_running_services_report_their_usage(client: TestClient) -> None:
    run_job(client, LAB)

    body = client.get("/api/environments/lab/usage").json()

    assert body["available"] is True
    assert {item["service"] for item in body["services"]} == {"dvwa", "mariadb"}
    assert all(item["memory_mb"] > 0 for item in body["services"])
    assert all(item["cpu_percent"] is not None for item in body["services"])


def test_a_stopped_environment_uses_nothing(client: TestClient) -> None:
    run_job(client, LAB)
    assert _action(client, "lab", "stop")[-1]["type"] == "job.succeeded"

    body = client.get("/api/environments/lab/usage").json()

    assert body == {"available": True, "services": []}


def test_usage_needs_an_existing_environment(client: TestClient) -> None:
    assert client.get("/api/environments/nothing-here/usage").status_code == 404
    assert client.get("/api/environments/Bad_Name/usage").status_code == 422


def test_usage_says_unavailable_when_docker_cannot_tell(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_job(client, LAB)

    async def unreachable(self: SimulatedEngine, stack: StackHandle) -> list[ServiceUsage]:
        raise EngineError("Docker does not answer.")

    monkeypatch.setattr(SimulatedEngine, "usage", unreachable)

    assert client.get("/api/environments/lab/usage").json() == {
        "available": False,
        "services": [],
    }
