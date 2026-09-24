"""End-to-end validation of every template against the real control plane.

Requires `docker compose -f deploy/docker-compose.yml up -d --build` and Docker
on this machine. Opt-in (slow, pulls images):

    ENVCRAFTER_E2E=1 uv run pytest tests/e2e -v

For each template: deploy through the API, follow the WebSocket to the end,
check every web UI answers through Traefik, check network isolation with the
Docker CLI, then remove the environment through the API.
"""

import json
import os
import socket
import subprocess
import time
from typing import Any

import httpx2 as httpx
import pytest
from websockets.sync.client import connect

pytestmark = pytest.mark.skipif(
    not os.environ.get("ENVCRAFTER_E2E"), reason="set ENVCRAFTER_E2E=1 to run end-to-end tests"
)

UI_HOST = "envcrafter.localhost"
ORIGIN = f"http://{UI_HOST}"
TERMINAL = {"job.succeeded", "job.failed"}


def _api() -> httpx.Client:
    # *.localhost is resolved by browsers, not always by the OS resolver:
    # connect to the loopback address and set the Host header explicitly.
    return httpx.Client(base_url="http://127.0.0.1", headers={"Host": UI_HOST}, timeout=30)


def _follow(events_url: str, timeout: float = 1200) -> list[dict[str, Any]]:
    sock = socket.create_connection(("127.0.0.1", 80))
    events: list[dict[str, Any]] = []
    with connect(f"ws://{UI_HOST}{events_url}", sock=sock, origin=ORIGIN) as ws:  # type: ignore[arg-type]
        while True:
            event = json.loads(ws.recv(timeout=timeout))
            events.append(event)
            if event["type"] in TERMINAL:
                return events


def _wait_for_ui(host: str, attempts: int = 30) -> int:
    """Traefik applies Docker events after a short throttle (2s by default)."""
    status = 0
    for _ in range(attempts):
        status = httpx.get("http://127.0.0.1/", headers={"Host": host}, timeout=30).status_code
        if status not in {404, 502, 503}:
            break
        time.sleep(2)
    return status


def _docker(*args: str) -> str:
    # Test-only helper with a fixed argv (no shell): the docker CLI from PATH.
    command = ["docker", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout  # noqa: S603


def _templates() -> list[dict[str, Any]]:
    if not os.environ.get("ENVCRAFTER_E2E"):
        return []
    with _api() as api:
        templates: list[dict[str, Any]] = api.get("/api/templates").json()["templates"]
        return templates


@pytest.mark.parametrize("template", _templates(), ids=lambda t: t["id"])
def test_template_deploys_serves_and_is_removed(template: dict[str, Any]) -> None:
    with _api() as api:
        response = api.post(
            "/api/jobs",
            json={"mode": "template", "template_id": template["id"]},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 202, response.text
        job = response.json()
        project = job["project_name"]

        try:
            events = _follow(job["events_url"])
            log = "\n".join(event["message"] for event in events)
            assert events[-1]["type"] == "job.succeeded", log

            # Every web UI answers through Traefik (no 404 = routed, no 5xx = up).
            for component in template["components"]:
                if component["web_access"] is not None:
                    host = component["web_access"].replace("<project>", project)
                    status = _wait_for_ui(host)
                    assert status < 400 or status == 401, (host, status)

            # Network isolation, checked on the real Docker objects.
            internal = json.loads(_docker("network", "inspect", f"ec-{project}-internal"))[0]
            assert internal["Internal"] is True
            assert internal["Labels"]["envcrafter.managed"] == "true"
            networks = _docker("network", "ls", "--format", "{{.Name}}").split()
            assert (f"ec-{project}-egress" in networks) == template["needs_internet"]
        finally:
            removal = api.delete(f"/api/environments/{project}", headers={"Origin": ORIGIN})
            assert removal.status_code == 202, removal.text
            done = _follow(removal.json()["events_url"])
            assert done[-1]["type"] == "job.succeeded", done[-1]["message"]

    leftovers = _docker("ps", "-a", "--filter", f"label=envcrafter.project={project}", "-q")
    assert leftovers.strip() == ""
