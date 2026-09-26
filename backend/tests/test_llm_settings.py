"""The Claude API key saved from the UI: checked, stored owner-only, never returned."""

import asyncio
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.services.llm_settings import SettingsStore
from app.translator.client import TranslatorError
from app.translator.spec import StackSpec
from tests.conftest import ClientFactory, FakeTranslator, collect_events, spec

UI_KEY = "sk-ant-api03-saved-from-the-ui-9f3c"
ENV_KEY = SecretStr("sk-ant-api03-from-the-environment-7b21")
ORIGIN = {"Origin": "http://localhost:8000"}


class _Keys:
    """Translator factory recording the keys it was given; `bad-` keys are rejected."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, key: SecretStr) -> FakeTranslator:
        self.seen.append(key.get_secret_value())
        return FakeTranslator(spec(), rejected="bad-" in key.get_secret_value())


class _Unreachable(FakeTranslator):
    async def verify(self) -> None:
        raise TranslatorError("The Claude API is unreachable. Check the network, then try again.")


def _save(client: TestClient, key: str) -> dict[str, object]:
    response = client.put("/api/settings/llm-key", json={"api_key": key}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()["llm"]
    return body


def test_no_key_by_default(client: TestClient, settings: Settings) -> None:
    assert client.get("/api/settings").json() == {
        "llm": {"configured": False, "source": None, "key_hint": None, "model": settings.llm_model}
    }
    assert client.get("/api/config").json()["llm_available"] is False


def test_environment_key_is_reported_without_leaking(
    make_client: ClientFactory, settings: Settings
) -> None:
    settings.llm_api_key = ENV_KEY
    client = make_client(translator_factory=_Keys())

    response = client.get("/api/settings")

    assert response.json()["llm"] | {"model": None} == {
        "configured": True,
        "source": "environment",
        "key_hint": "…7b21",
        "model": None,
    }
    assert ENV_KEY.get_secret_value() not in response.text
    assert client.get("/api/config").json()["llm_available"] is True


def test_saved_key_enables_ai_requests_and_survives_a_restart(
    make_client: ClientFactory, settings: Settings
) -> None:
    keys = _Keys()
    client = make_client(translator_factory=keys)
    assert client.post("/api/plans", json={"prompt": "A service desk"}).status_code == 503

    status = _save(client, UI_KEY)

    assert status == {
        "configured": True,
        "source": "settings",
        "key_hint": "…9f3c",
        "model": settings.llm_model,
    }
    assert keys.seen == [UI_KEY]
    assert client.get("/api/config").json()["llm_available"] is True
    assert client.post("/api/plans", json={"prompt": "A service desk"}).status_code == 202
    assert UI_KEY not in client.get("/api/settings").text

    # Stored like the .env file: owner-only, in an owner-only directory. Windows has no
    # POSIX modes; the control plane that runs EnvCrafter for real is Linux.
    stored = settings.settings_file
    assert json.loads(stored.read_text(encoding="utf-8"))["llm_api_key"] == UI_KEY
    if os.name == "posix":
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    assert [path.name for path in stored.parent.iterdir()] == [stored.name]  # no temp file left

    restarted = make_client(translator_factory=_Keys())
    assert restarted.get("/api/settings").json()["llm"]["source"] == "settings"


def test_a_saved_key_overrides_the_environment_key_until_it_is_removed(
    make_client: ClientFactory, settings: Settings
) -> None:
    settings.llm_api_key = ENV_KEY
    keys = _Keys()
    client = make_client(translator_factory=keys)

    assert _save(client, UI_KEY)["source"] == "settings"
    removed = client.delete("/api/settings/llm-key", headers=ORIGIN)

    assert removed.status_code == 200
    assert removed.json()["llm"]["source"] == "environment"
    assert keys.seen == [ENV_KEY.get_secret_value(), UI_KEY, ENV_KEY.get_secret_value()]
    assert json.loads(settings.settings_file.read_text(encoding="utf-8"))["llm_api_key"] is None


def test_removing_the_only_key_turns_ai_requests_off(make_client: ClientFactory) -> None:
    client = make_client(translator_factory=_Keys())
    _save(client, UI_KEY)

    response = client.delete("/api/settings/llm-key", headers=ORIGIN)

    assert response.json()["llm"]["configured"] is False
    assert client.get("/api/config").json()["llm_available"] is False
    assert client.post("/api/plans", json={"prompt": "A service desk"}).status_code == 503


def test_a_rejected_key_is_neither_stored_nor_used(
    make_client: ClientFactory, settings: Settings
) -> None:
    client = make_client(translator_factory=_Keys())

    response = client.put(
        "/api/settings/llm-key", json={"api_key": "sk-ant-bad-key-0123456789"}, headers=ORIGIN
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "The Claude API rejected this key."
    assert not settings.settings_file.exists()
    assert client.get("/api/config").json()["llm_available"] is False


def test_an_unreachable_api_is_a_bad_gateway(
    make_client: ClientFactory, settings: Settings
) -> None:
    def unreachable(key: SecretStr) -> FakeTranslator:
        return _Unreachable(spec())

    client = make_client(translator_factory=unreachable)
    response = client.put("/api/settings/llm-key", json={"api_key": UI_KEY}, headers=ORIGIN)

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]
    assert not settings.settings_file.exists()


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": "short"},
        {"api_key": "sk-ant-api03 with spaces inside"},
        {"api_key": "sk-ant-api03-line\nbreak-0123456789"},
        {"api_key": UI_KEY, "model": "another-model"},
        {},
    ],
)
def test_malformed_keys_are_refused(make_client: ClientFactory, body: dict[str, str]) -> None:
    client = make_client(translator_factory=_Keys())
    response = client.put("/api/settings/llm-key", json=body, headers=ORIGIN)
    assert response.status_code == 422


def test_key_endpoints_refuse_cross_site_requests(make_client: ClientFactory) -> None:
    client = make_client(translator_factory=_Keys())
    evil = {"Origin": "https://evil.example"}

    put = client.put("/api/settings/llm-key", json={"api_key": UI_KEY}, headers=evil)
    no_json = client.put(
        "/api/settings/llm-key",
        content=json.dumps({"api_key": UI_KEY}),
        headers={**ORIGIN, "Content-Type": "text/plain"},
    )
    delete = client.delete("/api/settings/llm-key", headers=evil)

    assert (put.status_code, no_json.status_code, delete.status_code) == (403, 415, 403)
    assert client.get("/api/settings").json()["llm"]["configured"] is False


def test_an_untrustworthy_settings_file_is_ignored(
    make_client: ClientFactory, settings: Settings
) -> None:
    settings.settings_file.parent.mkdir(parents=True)
    settings.settings_file.write_text('{"llm_api_key": "not a key!", "extra": 1}', encoding="utf-8")

    client = make_client(translator_factory=_Keys())

    assert client.get("/api/settings").json()["llm"]["configured"] is False


def test_store_round_trip_and_size_cap(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "data" / "settings.json")
    assert store.load_api_key() is None
    store.clear_api_key()  # nothing to clear: no file is created
    assert not (tmp_path / "data").exists()

    store.save_api_key(SecretStr(UI_KEY))
    loaded = store.load_api_key()
    assert loaded is not None and loaded.get_secret_value() == UI_KEY

    (tmp_path / "data" / "settings.json").write_text(" " * 20_000, encoding="utf-8")
    assert store.load_api_key() is None


def test_a_running_analysis_keeps_the_key_it_started_with(make_client: ClientFactory) -> None:
    """Removing the key while a plan is being analysed does not break that analysis."""
    release = threading.Event()

    class _Blocking(FakeTranslator):
        async def translate(self, prompt: str) -> StackSpec:
            # Set by the test thread: wait in a worker thread, never on the event loop.
            await asyncio.to_thread(release.wait, 10)
            return await super().translate(prompt)

    translator = _Blocking(spec(decision="template", template_id="owasp-juice-shop"))
    client = make_client(translator_factory=lambda key: translator)
    _save(client, UI_KEY)
    job = client.post("/api/plans", json={"prompt": "A vulnerable web app"}).json()
    try:
        removed = client.delete("/api/settings/llm-key", headers=ORIGIN)
    finally:
        release.set()

    assert removed.json()["llm"]["configured"] is False
    with client.websocket_connect(job["events_url"]) as ws:
        events = collect_events(ws)
    assert events[-1]["type"] == "job.succeeded", events[-1]
    assert translator.prompts == ["A vulnerable web app"]


def test_an_unwritable_settings_file_is_reported_and_changes_nothing(
    make_client: ClientFactory, settings: Settings
) -> None:
    blocker = settings.settings_file.parent
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("a file where the settings directory should be", encoding="utf-8")
    client = make_client(translator_factory=_Keys())

    response = client.put("/api/settings/llm-key", json={"api_key": UI_KEY}, headers=ORIGIN)

    assert response.status_code == 500
    assert "cannot write its settings file" in response.json()["detail"]
    assert str(blocker) not in response.text  # paths stay server-side
    assert client.get("/api/settings").json()["llm"]["configured"] is False
