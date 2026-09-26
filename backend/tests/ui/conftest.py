"""A real uvicorn server (simulated engine) for browser tests, one per test."""

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn

from app.core.config import Settings
from app.main import TranslatorFactory, create_app
from tests.conftest import TEST_API_KEY, FakeTranslator, spec


@dataclass(frozen=True)
class LiveServer:
    url: str
    workspaces: Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _serve(
    tmp_path: Path, *, delay: float, translator: FakeTranslator | None
) -> Iterator[LiveServer]:
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    settings = Settings(
        engine="simulated",
        simulated_step_delay=delay,
        serve_frontend=True,
        allowed_origins=[origin],
        allowed_hosts=["127.0.0.1"],
        workspaces_dir=tmp_path / "workspaces",
        settings_file=tmp_path / "data" / "settings.json",
        llm_api_key=TEST_API_KEY if translator is not None else None,
        inventory_cache_seconds=0,
        health_poll_seconds=0.2,
    )
    # Any key saved from the Settings dialog is accepted, unless it contains "rejected".
    factory: TranslatorFactory | None = (
        (lambda key: translator)
        if translator is not None
        else lambda key: FakeTranslator(spec(), rejected="rejected" in key.get_secret_value())
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, factory), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the UI test server did not start")
        time.sleep(0.05)
    try:
        yield LiveServer(url=origin, workspaces=settings.workspaces_dir)
    finally:
        server.should_exit = True
        thread.join(timeout=20)


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[LiveServer]:
    yield from _serve(tmp_path, delay=0.05, translator=None)


@pytest.fixture
def slow_live_server(tmp_path: Path) -> Iterator[LiveServer]:
    yield from _serve(tmp_path, delay=0.8, translator=None)


@pytest.fixture
def live_server_with_llm(tmp_path: Path) -> Iterator[LiveServer]:
    llm_output = spec(
        decision="template",
        template_id="owasp-juice-shop",
        title="Juice Shop lab",
        summary="A deliberately vulnerable shop to practise the OWASP Top 10.",
    )
    yield from _serve(tmp_path, delay=0.05, translator=FakeTranslator(llm_output))


@pytest.fixture
def live_server_unsupported(tmp_path: Path) -> Iterator[tuple[LiveServer, FakeTranslator]]:
    translator = FakeTranslator(
        spec(decision="unsupported", explanation="Public mail servers are out of scope.")
    )
    for server in _serve(tmp_path, delay=0.05, translator=translator):
        yield server, translator
