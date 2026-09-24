import json
from typing import Any

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.workspace.manager import WorkspaceManager
from tests.conftest import ClientFactory, FakeTranslator, run_job, spec

MEDIA_UIS = ["jellyfin", "sonarr", "radarr", "prowlarr", "qbittorrent"]
BOOKS: dict[str, Any] = {
    "name": "books",
    "image": "docker.io/advplyr/audiobookshelf:2.36.1",
    "purpose": "Audiobooks",
    "environment": [],
    "volumes": [],
    "depends_on": [],
    "needs_internet": False,
}


def _meta(settings: Settings, project: str) -> dict[str, Any]:
    text = (settings.workspaces_dir / project / "meta.json").read_text(encoding="utf-8")
    meta: dict[str, Any] = json.loads(text)
    return meta


def test_template_deployment_records_metadata_and_every_url(
    client: TestClient, settings: Settings
) -> None:
    events = run_job(
        client, {"mode": "template", "template_id": "media-stack", "project_name": "media"}
    )

    meta = _meta(settings, "media")
    assert meta["schema_version"] == 1
    assert meta["origin"] == "template"
    assert meta["template_id"] == "media-stack"
    assert meta["title"] == "Media Stack"
    assert [url["service"] for url in meta["urls"]] == MEDIA_UIS
    assert meta["urls"][0] == {
        "service": "jellyfin",
        "name": "Jellyfin 10.11",
        "url": "http://media.localhost",
    }
    assert meta["urls"][1]["url"] == "http://sonarr.media.localhost"
    assert {service["service"] for service in meta["services"]} == set(MEDIA_UIS)
    assert "media" in meta["volumes"]

    done = events[-1]
    assert done["type"] == "job.succeeded"
    assert done["url"] == "http://media.localhost"
    assert done["urls"] == meta["urls"]


def test_metadata_never_contains_generated_secrets(client: TestClient, settings: Settings) -> None:
    run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "desk"})

    env = (settings.workspaces_dir / "desk" / ".env").read_text(encoding="utf-8")
    secrets = [line.split("=", 1)[1] for line in env.splitlines() if line.startswith("DB_")]
    meta = (settings.workspaces_dir / "desk" / "meta.json").read_text(encoding="utf-8")
    assert secrets
    assert all(value not in meta for value in secrets)


def test_ai_custom_stack_uses_allowlist_titles(
    make_client: ClientFactory, settings: Settings
) -> None:
    llm_output = spec(title="Books", services=[BOOKS], expose={"service": "books", "port": 80})
    client = make_client(FakeTranslator(llm_output))

    run_job(client, {"mode": "prompt", "prompt": "books", "project_name": "books"})

    meta = _meta(settings, "books")
    assert meta["origin"] == "prompt"
    assert meta["template_id"] is None
    assert meta["title"] == "Books"
    assert meta["services"] == [
        {
            "service": "books",
            "name": "Audiobookshelf 2.36",
            "image": "docker.io/advplyr/audiobookshelf:2.36.1",
        }
    ]
    assert meta["urls"] == [
        {"service": "books", "name": "Audiobookshelf 2.36", "url": "http://books.localhost"}
    ]


def test_ai_selected_template_is_recorded_with_prompt_origin(
    make_client: ClientFactory, settings: Settings
) -> None:
    client = make_client(FakeTranslator(spec(decision="template", template_id="dvwa")))

    run_job(client, {"mode": "prompt", "prompt": "a lab", "project_name": "lab"})

    meta = _meta(settings, "lab")
    assert (meta["origin"], meta["template_id"], meta["title"]) == ("prompt", "dvwa", "DVWA")


def test_job_summary_lists_urls_after_success(client: TestClient) -> None:
    events = run_job(
        client, {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"}
    )

    summary = client.get(f"/api/jobs/{events[0]['job_id']}").json()

    assert summary["urls"] == [
        {"service": "juice-shop", "name": "OWASP Juice Shop 20", "url": "http://shop.localhost"}
    ]


def test_unreadable_metadata_is_ignored(settings: Settings) -> None:
    manager = WorkspaceManager(settings.workspaces_dir)
    workspace = settings.workspaces_dir / "broken"
    workspace.mkdir(parents=True)

    (workspace / "meta.json").write_text("{not json", encoding="utf-8")
    assert manager.read_meta("broken") is None

    forged = {"schema_version": 1, "project": "broken", "unexpected": True}
    (workspace / "meta.json").write_text(json.dumps(forged), encoding="utf-8")
    assert manager.read_meta("broken") is None

    assert manager.read_meta("absent") is None
