from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.testclient import WebSocketTestSession

from app.core.config import Settings
from app.main import TranslatorFactory, create_app
from app.policy.images import ImageAllowlist
from app.translator.client import KeyRejectedError
from app.translator.spec import StackSpec

TERMINAL_TYPES = {"job.succeeded", "job.failed", "job.cancelled"}


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
        settings_file=tmp_path / "data" / "settings.json",
        llm_api_key=None,
        inventory_cache_seconds=0,
        readiness_cache_seconds=0,
    )


# Stands in for ENVCRAFTER_LLM_API_KEY when a test needs a translator from the start.
TEST_API_KEY = SecretStr("sk-ant-test-environment-key")


class FakeTranslator:
    """Returns a canned StackSpec: no network, fully deterministic."""

    model = "fake-model"

    def __init__(self, spec: StackSpec, *, rejected: bool = False) -> None:
        self.spec = spec
        self.rejected = rejected
        self.prompts: list[str] = []

    async def translate(self, prompt: str) -> StackSpec:
        self.prompts.append(prompt)
        return self.spec

    async def verify(self) -> None:
        if self.rejected:
            raise KeyRejectedError("The Claude API rejected this key.")


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(settings: Settings) -> Iterator[ClientFactory]:
    clients: list[TestClient] = []

    def factory(
        translator: FakeTranslator | None = None,
        *,
        translator_factory: TranslatorFactory | None = None,
    ) -> TestClient:
        """`translator`: served from the start, as with an environment key.
        `translator_factory`: only used once a key is saved (or with settings.llm_api_key)."""
        app_settings = settings
        if translator is not None:
            translator_factory = lambda key: translator  # noqa: E731
            if settings.llm_api_key is None:
                app_settings = settings.model_copy(update={"llm_api_key": TEST_API_KEY})
        # The context manager runs the lifespan (catalog, bus, orchestrator) and
        # keeps one event loop alive, so background job tasks can run.
        client = TestClient(create_app(app_settings, translator_factory))
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
