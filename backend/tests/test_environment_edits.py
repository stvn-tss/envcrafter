"""PATCH /api/environments/{project}: the display title and the notes of an environment."""

import asyncio
import json
import os
import stat
import threading
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import LogSink, StackHandle
from app.engine.simulated import SimulatedEngine
from app.models.environment import EnvironmentMeta
from app.workspace.manager import WorkspaceManager
from tests.conftest import run_job

ORIGIN = {"Origin": "http://localhost:8000"}
LAB = {"mode": "template", "template_id": "dvwa", "project_name": "lab"}


def _patch(client: TestClient, body: dict[str, Any], project: str = "lab") -> Any:
    return client.patch(f"/api/environments/{project}", json=body, headers=ORIGIN)


def test_title_and_notes_are_saved(client: TestClient, settings: Settings) -> None:
    run_job(client, LAB)
    notes = "Security level: high tomorrow.\nAsk Sam for the report."

    response = _patch(client, {"title": "  SQLi practice  ", "notes": notes})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"] == "lab"
    assert body["title"] == "SQLi practice"
    assert body["notes"] == notes
    workspace = settings.workspaces_dir / "lab"
    meta = json.loads((workspace / "meta.json").read_text(encoding="utf-8"))
    assert (meta["title"], meta["notes"]) == ("SQLi practice", notes)
    assert [path.name for path in workspace.iterdir() if path.name.endswith(".tmp")] == []
    if os.name == "posix":
        assert stat.S_IMODE((workspace / "meta.json").stat().st_mode) == 0o600
    listed = client.get("/api/environments").json()["environments"][0]
    assert (listed["title"], listed["notes"]) == ("SQLi practice", notes)


def test_notes_alone_keep_the_title(client: TestClient) -> None:
    run_job(client, LAB)
    assert _patch(client, {"notes": "first"}).json()["title"] == "DVWA"
    cleared = _patch(client, {"notes": ""}).json()
    assert (cleared["title"], cleared["notes"]) == ("DVWA", "")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": "a\tb"},
        {"title": "line\nbreak"},
        {"title": "x" * 81},
        {"notes": "x" * 2001},
        {"notes": "bidi " + chr(0x202E) + " override"},
        {"notes": "bell " + chr(0x07)},
        {"title": "ok", "owner": "me"},
        {"title": 42},
    ],
)
def test_invalid_edits_are_refused(client: TestClient, body: dict[str, Any]) -> None:
    run_job(client, LAB)
    assert _patch(client, body).status_code == 422
    assert client.get("/api/environments/lab").json()["title"] == "DVWA"


def test_edit_guards(client: TestClient) -> None:
    run_job(client, LAB)
    assert _patch(client, {"title": "x"}, project="unknown-env").status_code == 404
    assert _patch(client, {"title": "x"}, project="Bad_Name").status_code == 422
    foreign = client.patch(
        "/api/environments/lab", json={"title": "x"}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    plain = client.patch(
        "/api/environments/lab",
        content=b'{"title": "x"}',
        headers={**ORIGIN, "Content-Type": "text/plain"},
    )
    assert plain.status_code == 415


def test_edits_wait_for_a_running_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    run_job(client, LAB)
    stopping, release = threading.Event(), threading.Event()

    async def slow_stop(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
    ) -> None:
        stopping.set()
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(SimulatedEngine, "stop", slow_stop)
    assert client.post("/api/environments/lab/actions", json={"action": "stop"}).status_code == 202
    try:
        assert stopping.wait(10)
        busy = _patch(client, {"notes": "while stopping"})
    finally:
        release.set()
    assert busy.status_code == 409


def test_removal_waits_for_an_edit_in_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal starting during the rewrite would race it: rmtree against a new file."""
    run_job(client, LAB)
    editing, release = threading.Event(), threading.Event()
    original = WorkspaceManager.update_meta

    def slow_update(
        self: WorkspaceManager, project: str, changes: Mapping[str, object]
    ) -> EnvironmentMeta:
        editing.set()
        release.wait(10)
        return original(self, project, changes)

    monkeypatch.setattr(WorkspaceManager, "update_meta", slow_update)
    result: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: result.update(response=_patch(client, {"notes": "x"})))
    thread.start()
    try:
        assert editing.wait(10)
        removal = client.delete("/api/environments/lab", headers=ORIGIN)
    finally:
        release.set()
        thread.join(10)

    assert removal.status_code == 409
    assert result["response"].status_code == 200


def test_an_environment_without_metadata_cannot_be_edited(
    client: TestClient, settings: Settings
) -> None:
    run_job(client, LAB)
    (settings.workspaces_dir / "lab" / "meta.json").unlink()
    assert _patch(client, {"title": "x"}).status_code == 409
