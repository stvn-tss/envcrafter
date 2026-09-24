from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import EngineError, ServiceStatus, StackHandle
from app.engine.simulated import SimulatedEngine
from app.models.environment import EnvironmentState
from app.services.inventory import EnvironmentInventory, summarize_state
from app.workspace.manager import WorkspaceManager
from tests.conftest import collect_events, run_job

RUNNING = "running"


def _status(service: str, state: str = RUNNING, health: str | None = None) -> ServiceStatus:
    return ServiceStatus(service=service, state=state, health=health)


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ([], EnvironmentState.MISSING),
        ([_status("a", "exited"), _status("b", "exited")], EnvironmentState.STOPPED),
        ([_status("a"), _status("b", "exited")], EnvironmentState.DEGRADED),
        ([_status("a")], EnvironmentState.DEGRADED),  # b has no container at all
        ([_status("a"), _status("b", health="unhealthy")], EnvironmentState.DEGRADED),
        ([_status("a"), _status("b", health="starting")], EnvironmentState.STARTING),
        ([_status("a", health="healthy"), _status("b")], EnvironmentState.RUNNING),
    ],
)
def test_state_summary(observed: list[ServiceStatus], expected: EnvironmentState) -> None:
    assert summarize_state(["a", "b"], observed) is expected


def test_environments_list_is_empty_at_first(client: TestClient) -> None:
    assert client.get("/api/environments").json() == {"environments": []}


def test_deployed_environment_is_listed_with_its_live_state(client: TestClient) -> None:
    run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "desk"})

    [env] = client.get("/api/environments").json()["environments"]

    assert env["project"] == "desk"
    assert (env["title"], env["origin"], env["template_id"]) == ("GLPI", "template", "glpi")
    assert env["state"] == "running"
    assert {s["service"]: s["state"] for s in env["services"]} == {
        "glpi": "running",
        "mariadb": "running",
    }
    assert env["services"][0]["name"] == "GLPI 11"
    assert env["urls"][0]["url"] == "http://desk.localhost"
    assert env["volumes"] == ["glpi-data", "mariadb-data"]
    assert env["job"] is None
    assert client.get("/api/environments/desk").json() == env


def test_environment_lookup_validates_the_name(client: TestClient) -> None:
    assert client.get("/api/environments/nothing-here").status_code == 404
    assert client.get("/api/environments/Bad_Name").status_code == 422


def test_removed_environment_disappears(client: TestClient) -> None:
    run_job(client, {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"})
    response = client.delete("/api/environments/shop")
    with client.websocket_connect(response.json()["events_url"]) as ws:
        collect_events(ws)
    assert client.get("/api/environments").json() == {"environments": []}


def test_running_job_is_attached_to_its_environment(make_client: Any, settings: Settings) -> None:
    settings.simulated_step_delay = 0.3  # the deployment lasts about two seconds
    client = make_client()
    job = client.post(
        "/api/jobs", json={"mode": "template", "template_id": "glpi", "project_name": "desk"}
    ).json()

    [env] = client.get("/api/environments").json()["environments"]

    assert env["job"] == {
        "job_id": job["job_id"],
        "mode": "template",
        "events_url": job["events_url"],
    }
    assert env["state"] in {"pending", "running"}  # pending until the workspace exists
    with client.websocket_connect(job["events_url"]) as ws:
        collect_events(ws)
    assert client.get("/api/environments/desk").json()["job"] is None


def test_engine_failure_reports_unknown_states(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_job(client, {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"})

    async def broken_status(
        self: SimulatedEngine, stacks: Sequence[StackHandle]
    ) -> dict[str, list[ServiceStatus]]:
        raise EngineError("`docker ps` failed (exit code 1)")

    monkeypatch.setattr(SimulatedEngine, "status", broken_status)
    response = client.get("/api/environments")

    assert response.status_code == 200
    [env] = response.json()["environments"]
    assert env["state"] == "unknown"
    assert env["urls"][0]["url"] == "http://shop.localhost"  # metadata still shown


def test_workspace_without_metadata_is_still_listed(client: TestClient, settings: Settings) -> None:
    legacy = settings.workspaces_dir / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "compose.yaml").write_text(
        "name: ec-legacy\nservices:\n  app:\n    image: busybox\n", encoding="utf-8"
    )

    [env] = client.get("/api/environments").json()["environments"]

    assert (env["project"], env["title"], env["origin"]) == ("legacy", "legacy", None)
    assert env["services"] == [
        {"service": "app", "name": "app", "image": None, "state": "running", "health": None}
    ]


def test_jobs_endpoint_lists_retained_and_active_jobs(client: TestClient) -> None:
    events = run_job(
        client, {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"}
    )

    jobs = client.get("/api/jobs").json()["jobs"]

    assert [job["job_id"] for job in jobs] == [events[0]["job_id"]]
    assert jobs[0]["status"] == "succeeded"
    assert client.get("/api/jobs?active=true").json() == {"jobs": []}
    assert client.get("/api/jobs?active=maybe").status_code == 422


class _CountingEngine:
    def __init__(self, observed: dict[str, list[ServiceStatus]]) -> None:
        self.observed = observed
        self.calls = 0

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        self.calls += 1
        return {stack.project: self.observed.get(stack.project, []) for stack in stacks}


class _NoJobs:
    def list_jobs(self, *, active_only: bool = False) -> list[Any]:
        return []

    def active_job(self, project: str) -> None:
        return None


def _workspace(root: Path, project: str) -> None:
    (root / project).mkdir(parents=True)
    (root / project / "compose.yaml").write_text(
        f"name: ec-{project}\nservices:\n  app:\n    image: busybox\n", encoding="utf-8"
    )


@pytest.mark.anyio
async def test_engine_status_is_shared_between_close_refreshes(tmp_path: Path) -> None:
    _workspace(tmp_path, "demo")
    engine = _CountingEngine({"demo": [_status("app")]})
    inventory = EnvironmentInventory(
        workspaces=WorkspaceManager(tmp_path), engine=engine, jobs=_NoJobs(), cache_seconds=60
    )

    first, second = await inventory.list(), await inventory.list()

    assert first == second
    assert engine.calls == 1
    _workspace(tmp_path, "other")  # a new project invalidates the cached snapshot
    assert len(await inventory.list()) == 2
    assert engine.calls == 2
