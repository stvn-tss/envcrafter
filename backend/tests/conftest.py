from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from app.core.config import Settings
from app.main import TranslatorFactory, create_app
from app.policy.images import ImageAllowlist
from app.translator.spec import StackSpec

TERMINAL_TYPES = {"job.succeeded", "job.failed"}


@pytest.fixture(scope="session")
def allowlist() -> ImageAllowlist:
    return ImageAllowlist.load(Settings().image_allowlist)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        engine="simulated",
        simulated_step_delay=0,
        serve_frontend=False,
        allowed_hosts=["testserver"],
        workspaces_dir=tmp_path / "workspaces",
        llm_api_key=None,
        inventory_cache_seconds=0,
    )


class FakeTranslator:
    """Returns a canned StackSpec: no network, fully deterministic."""

    model = "fake-model"

    def __init__(self, spec: StackSpec) -> None:
        self.spec = spec
        self.prompts: list[str] = []

    async def translate(self, prompt: str) -> StackSpec:
        self.prompts.append(prompt)
        return self.spec


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(settings: Settings) -> Iterator[ClientFactory]:
    clients: list[TestClient] = []

    def factory(translator: FakeTranslator | None = None) -> TestClient:
        translator_factory: TranslatorFactory | None = (
            (lambda catalog, allowlist: translator) if translator is not None else None
        )
        # The context manager runs the lifespan (catalog, bus, orchestrator) and
        # keeps one event loop alive, so background job tasks can run.
        client = TestClient(create_app(settings, translator_factory))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


def collect_events(ws: WebSocketTestSession) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in TERMINAL_TYPES:
            return events


def run_job(client: TestClient, payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 202, response.text
    with client.websocket_connect(response.json()["events_url"]) as ws:
        return collect_events(ws)


def spec(**overrides: Any) -> StackSpec:
    base: dict[str, Any] = {
        "decision": "custom",
        "template_id": None,
        "title": "Test stack",
        "summary": "A test stack.",
        "services": [],
        "expose": None,
        "secrets": [],
        "explanation": "test",
    }
    return StackSpec.model_validate({**base, **overrides})
