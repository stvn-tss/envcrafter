from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import __version__
from app.core.config import Settings
from app.models.common import PROJECT_NAME_PATTERN
from tests.conftest import ClientFactory, FakeTranslator, spec


def test_config_describes_the_server(client: TestClient) -> None:
    assert client.get("/api/config").json() == {
        "version": __version__,
        "engine": "simulated",
        "llm_available": False,
        "public_domain": "localhost",
        "project_name_pattern": PROJECT_NAME_PATTERN,
    }


def test_config_reports_the_llm_without_leaking_its_key(
    make_client: ClientFactory, settings: Settings
) -> None:
    settings.llm_api_key = SecretStr("test-api-key-must-never-leak")
    client = make_client(FakeTranslator(spec()))

    response = client.get("/api/config")

    assert response.json()["llm_available"] is True
    assert "must-never-leak" not in response.text


def test_every_http_response_carries_security_headers(client: TestClient) -> None:
    for response in (client.get("/api/health"), client.get("/api/does-not-exist")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_static_files_must_be_revalidated(
    make_client: ClientFactory, settings: Settings, tmp_path: Path
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "js").mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (frontend / "js" / "main.js").write_text("export {};", encoding="utf-8")
    settings.serve_frontend = True
    settings.frontend_dir = frontend
    client = make_client()

    for path in ("/", "/js/main.js"):
        response = client.get(path)
        assert response.status_code == 200
        # Browsers revalidate with the ETag on every load: never a stale mix of modules.
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["etag"]
    assert client.get("/js/main.js").headers["content-type"].startswith("text/javascript")
