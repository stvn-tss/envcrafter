from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from tests.conftest import ClientFactory, FakeTranslator, collect_events, run_job, spec

DEPLOY_STEPS = [
    "security_validation",
    "workspace_setup",
    "image_pull",
    "network_setup",
    "container_deploy",
]


def _logs(events: list[dict[str, Any]]) -> str:
    return "\n".join(event["message"] for event in events)


def test_lists_templates_with_components(client: TestClient) -> None:
    body = client.get("/api/templates").json()
    by_category: dict[str, list[str]] = {}
    for template in body["templates"]:
        by_category.setdefault(template["category"], []).append(template["id"])
    # At least two templates per category.
    assert all(len(ids) >= 2 for ids in by_category.values())
    assert set(by_category) == {"itsm", "media", "security-lab"}

    glpi = next(t for t in body["templates"] if t["id"] == "glpi")
    images = {c["service"]: c["image"] for c in glpi["components"]}
    assert images == {"glpi": "glpi/glpi:11.0.9", "mariadb": "mariadb:11.4.13"}
    assert next(c for c in glpi["components"] if c["service"] == "glpi")["web_access"] == (
        "<project>.localhost"
    )


def test_template_job_runs_every_step_and_writes_workspace(
    client: TestClient, settings: Settings
) -> None:
    events = run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "demo"})

    assert [step["key"] for step in events[0]["plan"]] == ["template_resolution", *DEPLOY_STEPS]
    assert events[-1]["type"] == "job.succeeded"
    assert events[-1]["url"] == "http://demo.localhost"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs))  # strictly increasing (replays may skip coalesced progress)

    workspace = settings.workspaces_dir / "demo"
    compose = yaml.safe_load((workspace / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["name"] == "ec-demo"
    assert compose["networks"]["internal"] == {
        "name": "ec-demo-internal",
        "internal": True,
        "labels": {"envcrafter.managed": "true", "envcrafter.project": "demo"},
    }
    env = (workspace / ".env").read_text(encoding="utf-8")
    assert "DB_PASSWORD=" in env and "EC_HOSTNAME=demo.localhost" in env


def test_prompt_mode_without_llm_key_is_unavailable(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"mode": "prompt", "prompt": "Deploy GLPI"})
    assert response.status_code == 503


def test_prompt_mode_can_select_a_template(make_client: ClientFactory) -> None:
    translator = FakeTranslator(spec(decision="template", template_id="owasp-juice-shop"))
    client = make_client(translator)

    events = run_job(client, {"mode": "prompt", "prompt": "I want to practise web hacking"})

    assert events[0]["plan"][0]["key"] == "ai_translation"
    assert "Template 'OWASP Juice Shop'" in _logs(events)
    assert events[-1]["type"] == "job.succeeded"
    assert translator.prompts == ["I want to practise web hacking"]


def test_prompt_mode_custom_stack_goes_through_the_policy(make_client: ClientFactory) -> None:
    custom = spec(
        services=[
            {
                "name": "books",
                "image": "docker.io/advplyr/audiobookshelf:2.36.1",
                "purpose": "Audiobooks",
                "environment": [],
                "volumes": [{"name": "config", "mount_path": "/config"}],
                "depends_on": [],
                "needs_internet": False,
            }
        ],
        expose={"service": "books", "port": 80},
    )
    events = run_job(make_client(FakeTranslator(custom)), {"mode": "prompt", "prompt": "books"})

    assert events[-1]["type"] == "job.succeeded"
    assert "passed the schema and policy checks" in _logs(events)


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        (
            {"image": "docker.io/library/nginx:1.29.0", "environment": []},
            "is not on the image allow-list",
        ),
        (
            {
                "image": "docker.io/library/mariadb:11.4.13",
                "environment": [{"name": "MARIADB_PASSWORD", "value": "${HOST_SECRET}"}],
            },
            "unknown variable ${HOST_SECRET}",
        ),
        (
            {
                "image": "docker.io/library/mariadb:11.4.13",
                "environment": [],
                "volumes": [{"name": "../../etc", "mount_path": "/data"}],
            },
            "String should match pattern",
        ),
        (
            {
                "image": "docker.io/bkimminich/juice-shop:v20.2.0",
                "environment": [],
                "needs_internet": True,
            },
            "never get Internet access",
        ),
    ],
)
def test_malicious_llm_output_is_rejected(
    make_client: ClientFactory, service: dict[str, Any], expected: str
) -> None:
    """The LLM is untrusted: whatever it returns, the compose policy decides."""
    base = {"name": "app", "purpose": "x", "volumes": [], "depends_on": [], "needs_internet": False}
    llm_output = spec(services=[{**base, **service}])

    events = run_job(
        make_client(FakeTranslator(llm_output)), {"mode": "prompt", "prompt": "some request"}
    )

    assert events[-1]["type"] == "job.failed"
    assert events[-1]["message"] == "The stack was rejected by the security policy."
    assert expected in _logs(events)


def test_unsupported_request_fails_with_explanation(make_client: ClientFactory) -> None:
    llm_output = spec(decision="unsupported", explanation="Host access is not possible.")
    events = run_job(make_client(FakeTranslator(llm_output)), {"mode": "prompt", "prompt": "root"})

    assert events[-1]["type"] == "job.failed"
    assert "Host access is not possible." in events[-1]["message"]


def test_removal_deletes_the_workspace(client: TestClient, settings: Settings) -> None:
    run_job(client, {"mode": "template", "template_id": "dvwa", "project_name": "lab"})
    assert (settings.workspaces_dir / "lab").is_dir()

    response = client.delete("/api/environments/lab")
    assert response.status_code == 202
    with client.websocket_connect(response.json()["events_url"]) as ws:
        events = collect_events(ws)

    assert events[-1]["type"] == "job.succeeded"
    assert not Path(settings.workspaces_dir / "lab").exists()


def test_removal_input_validation(client: TestClient) -> None:
    assert client.delete("/api/environments/unknown-env").status_code == 404
    assert client.delete("/api/environments/..%2F..%2Fetc").status_code in {404, 422}
    assert client.delete("/api/environments/Bad_Name").status_code == 422
    foreign = client.delete("/api/environments/lab", headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403


def test_project_name_cannot_be_reused(client: TestClient) -> None:
    run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "taken"})
    response = client.post(
        "/api/jobs", json={"mode": "template", "template_id": "zabbix", "project_name": "taken"}
    )
    assert response.status_code == 409


def test_reconnect_replays_only_missed_events(client: TestClient) -> None:
    full = run_job(client, {"mode": "template", "template_id": "owasp-juice-shop"})
    job_id = full[0]["job_id"]
    cutoff = full[2]["seq"]

    with client.websocket_connect(f"/ws/jobs/{job_id}?after_seq={cutoff}") as ws:
        replay = collect_events(ws)

    assert [e["seq"] for e in replay] == [e["seq"] for e in full if e["seq"] > cutoff]


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "template", "template_id": "../../etc/passwd"},
        {"mode": "template", "template_id": "glpi", "project_name": "Bad Name!"},
        {"mode": "template", "template_id": "glpi", "privileged": True},
        {"mode": "prompt", "prompt": "hi"},
        {"mode": "prompt", "prompt": "x" * 2001},
        {"mode": "prompt", "prompt": "deploy glpi" + chr(0x202E) + ";rm -rf /"},
        {"mode": "shell", "command": "id"},
    ],
)
def test_rejects_invalid_payloads(client: TestClient, payload: dict[str, Any]) -> None:
    assert client.post("/api/jobs", json=payload).status_code == 422


def test_unknown_template_returns_404(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"mode": "template", "template_id": "nope"})
    assert response.status_code == 404


def test_post_rejects_foreign_origin(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={"mode": "template", "template_id": "glpi"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_post_requires_json_content_type(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        content=b'{"mode": "template", "template_id": "glpi"}',
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_websocket_rejects_foreign_origin(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/ws/jobs/{uuid4()}", headers={"Origin": "https://evil.example"}),
    ):
        pass
    assert exc_info.value.code == 1008


def test_websocket_unknown_job_closes_with_4404(client: TestClient) -> None:
    with (
        client.websocket_connect(f"/ws/jobs/{uuid4()}") as ws,
        pytest.raises(WebSocketDisconnect) as exc_info,
    ):
        ws.receive_json()
    assert exc_info.value.code == 4404


def test_rejects_unknown_host_header(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Host": "attacker.example"})
    assert response.status_code == 400
