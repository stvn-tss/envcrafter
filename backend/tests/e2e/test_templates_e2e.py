"""End-to-end validation of every template against the real control plane.

Requires `docker compose -f deploy/docker-compose.yml up -d --build` and Docker
on this machine. Opt-in (slow, pulls images):

    ENVCRAFTER_E2E=1 uv run pytest tests/e2e -v

For each template: deploy through the API, follow the WebSocket to the end,
check every web UI answers through Traefik, check network isolation with the
Docker CLI, check the environments API and a container log line, then remove
the environment through the API.

A separate lifecycle test stops and restarts one environment and checks it is
routable again. An opt-in test (`ENVCRAFTER_E2E_LLM=1`) exercises the full AI
flow (plan review, then deployment) against the real Claude API; it costs a
request and is skipped otherwise.

Every template test also measures its first start time and steady-state
memory against the `footprint` declared in its manifest, and warns (never
fails) when the manifest underestimates it. Run with `-s` to see the
`FOOTPRINT <template>: first_start=...s memory=...MB` lines.
"""

import json
import os
import socket
import subprocess
import time
import warnings
from datetime import datetime
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


def _environment(api: httpx.Client, project: str) -> dict[str, Any]:
    body: dict[str, Any] = api.get(f"/api/environments/{project}").json()
    return body


def _first_log_frame(project: str, service: str) -> dict[str, Any]:
    sock = socket.create_connection(("127.0.0.1", 80))
    url = f"ws://{UI_HOST}/ws/environments/{project}/logs?service={service}&tail=20"
    with connect(url, sock=sock, origin=ORIGIN) as ws:  # type: ignore[arg-type]
        frame: dict[str, Any] = json.loads(ws.recv(timeout=60))
        return frame


def _step_seconds(events: list[dict[str, Any]], key: str) -> float:
    stamps = {
        event["type"]: datetime.fromisoformat(event["timestamp"])
        for event in events
        if event.get("step_key") == key and event["type"] in {"step.started", "step.completed"}
    }
    return (stamps["step.completed"] - stamps["step.started"]).total_seconds()


def _memory_mb(project: str) -> float:
    names = _docker(
        "ps", "--filter", f"label=envcrafter.project={project}", "--format", "{{.Names}}"
    ).split()
    usage = _docker("stats", "--no-stream", "--format", "{{.MemUsage}}", *names)
    units = {
        "KiB": 1 / 1024,
        "MiB": 1.0,
        "GiB": 1024.0,
        "kB": 1 / 1000,
        "MB": 1.0,
        "GB": 1000.0,
        "B": 1 / 2**20,
    }
    total = 0.0
    for line in usage.splitlines():
        used = line.split("/")[0].strip()
        for unit, factor in units.items():
            if used.endswith(unit):
                total += float(used.removesuffix(unit)) * factor
                break
    return total


def _post(api: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = api.post(path, json=body, headers={"Origin": ORIGIN})
    assert response.status_code == 202, response.text
    job: dict[str, Any] = response.json()
    return job


def _remove(api: httpx.Client, project: str) -> None:
    removal = api.delete(f"/api/environments/{project}", headers={"Origin": ORIGIN})
    assert removal.status_code == 202, removal.text
    done = _follow(removal.json()["events_url"])
    assert done[-1]["type"] == "job.succeeded", done[-1]["message"]


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

            # Every web UI is announced in the final event and in the inventory.
            expected_hosts = {
                c["web_access"].replace("<project>", project)
                for c in template["components"]
                if c["web_access"] is not None
            }
            announced = {url["url"].removeprefix("http://") for url in events[-1]["urls"]}
            assert announced == expected_hosts
            environment = _environment(api, project)
            assert environment["state"] in {"running", "starting"}, environment
            assert _first_log_frame(project, events[-1]["urls"][0]["service"])["type"] == "line"

            # Footprint check: warn (never fail) when the manifest underestimates.
            footprint = template["footprint"]
            first_start = _step_seconds(events, "container_deploy")
            memory = _memory_mb(project)
            print(
                f"FOOTPRINT {template['id']}: first_start={first_start:.0f}s memory={memory:.0f}MB"
            )
            if first_start > 2 * footprint["first_start_seconds"]:
                warnings.warn(
                    f"{template['id']}: first start took {first_start:.0f}s", stacklevel=1
                )
            if memory > 1.5 * footprint["memory_mb"]:
                warnings.warn(f"{template['id']}: stack uses {memory:.0f} MB", stacklevel=1)
        finally:
            _remove(api, project)

    leftovers = _docker("ps", "-a", "--filter", f"label=envcrafter.project={project}", "-q")
    assert leftovers.strip() == ""


def test_stop_and_start_keep_the_environment_routable() -> None:
    with _api() as api:
        job = _post(api, "/api/jobs", {"mode": "template", "template_id": "owasp-juice-shop"})
        project = job["project_name"]
        try:
            assert _follow(job["events_url"])[-1]["type"] == "job.succeeded"
            stop = _post(api, f"/api/environments/{project}/actions", {"action": "stop"})
            assert _follow(stop["events_url"])[-1]["type"] == "job.succeeded"
            assert _environment(api, project)["state"] == "stopped"
            start = _post(api, f"/api/environments/{project}/actions", {"action": "start"})
            assert _follow(start["events_url"])[-1]["type"] == "job.succeeded"
            assert _environment(api, project)["state"] in {"running", "starting"}
            assert _wait_for_ui(f"{project}.localhost") < 400
        finally:
            _remove(api, project)


@pytest.mark.skipif(
    not os.environ.get("ENVCRAFTER_E2E_LLM"),
    reason="set ENVCRAFTER_E2E_LLM=1 to call the Claude API (costs a request)",
)
def test_ai_plan_is_reviewed_then_deployed() -> None:
    with _api() as api:
        planning = _post(
            api,
            "/api/plans",
            {"prompt": "A vulnerable web application to practise the OWASP Top 10"},
        )
        done = _follow(planning["events_url"])[-1]
        assert done["type"] == "job.succeeded", done["message"]
        plan = api.get(f"/api/plans/{done['plan_id']}").json()
        assert plan["services"]
        job = _post(api, "/api/jobs", {"mode": "plan", "plan_id": plan["plan_id"]})
        project = job["project_name"]
        try:
            assert _follow(job["events_url"])[-1]["type"] == "job.succeeded"
        finally:
            _remove(api, project)
