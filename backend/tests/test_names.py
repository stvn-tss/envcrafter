"""Deployments without a chosen name get the first free <template>-<n>: glpi-1, glpi-2..."""

from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from tests.conftest import ClientFactory, FakeTranslator, collect_events, spec

ORIGIN = {"Origin": "http://localhost:8000"}


def _finish(client: TestClient, job: dict[str, Any]) -> list[dict[str, Any]]:
    with client.websocket_connect(job["events_url"]) as ws:
        return collect_events(ws)


def _deploy(client: TestClient, payload: dict[str, Any]) -> str:
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 202, response.text
    job = response.json()
    assert _finish(client, job)[-1]["type"] == "job.succeeded"
    name: str = job["project_name"]
    return name


def test_unnamed_deployments_get_the_first_free_number(client: TestClient) -> None:
    glpi = {"mode": "template", "template_id": "glpi"}
    assert _deploy(client, glpi) == "glpi-1"
    assert _deploy(client, glpi) == "glpi-2"
    shop = {"mode": "template", "template_id": "owasp-juice-shop"}
    assert _deploy(client, shop) == "owasp-juice-shop-1"

    removal = client.delete("/api/environments/glpi-1", headers=ORIGIN)
    assert removal.status_code == 202, removal.text
    assert _finish(client, removal.json())[-1]["type"] == "job.succeeded"

    assert _deploy(client, glpi) == "glpi-1"  # the lowest free number again


def test_back_to_back_deployments_get_distinct_names(client: TestClient) -> None:
    jobs = [
        client.post("/api/jobs", json={"mode": "template", "template_id": "dvwa"}).json()
        for _ in range(3)
    ]
    for job in jobs:
        _finish(client, job)
    assert sorted(job["project_name"] for job in jobs) == ["dvwa-1", "dvwa-2", "dvwa-3"]


def test_a_stray_directory_does_not_block_the_next_name(
    client: TestClient, settings: Settings
) -> None:
    # No compose.yaml inside: not an environment, but the name is taken on disk.
    (settings.workspaces_dir / "dvwa-1").mkdir(parents=True)
    assert _deploy(client, {"mode": "template", "template_id": "dvwa"}) == "dvwa-2"


def test_a_chosen_name_is_kept(client: TestClient) -> None:
    payload = {"mode": "template", "template_id": "dvwa", "project_name": "my-lab"}
    assert _deploy(client, payload) == "my-lab"


def test_custom_ai_stacks_are_named_env(make_client: ClientFactory) -> None:
    service = {
        "name": "books",
        "image": "docker.io/advplyr/audiobookshelf:2.36.1",
        "purpose": "Audiobooks",
        "environment": [],
        "volumes": [{"name": "config", "mount_path": "/config"}],
        "depends_on": [],
        "needs_internet": False,
    }
    translator = FakeTranslator(spec(services=[service], expose={"service": "books", "port": 80}))
    assert _deploy(make_client(translator), {"mode": "prompt", "prompt": "books"}) == "env-1"
