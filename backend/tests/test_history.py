"""Durable history in SQLite: job summaries, reviewable AI plans and an audit log."""

import asyncio
import json
import os
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import LogSink, ProgressSink, StackHandle
from app.engine.simulated import SimulatedEngine
from app.main import create_app
from app.services.history import HistoryStore, JobStart
from app.translator.spec import StackSpec
from tests.conftest import (
    TEST_API_KEY,
    ClientFactory,
    FakeTranslator,
    collect_events,
    run_job,
    spec,
)

ORIGIN = {"Origin": "http://localhost:8000"}
LAB = {"mode": "template", "template_id": "dvwa", "project_name": "lab"}


def _finish(client: TestClient, job: dict[str, Any]) -> list[dict[str, Any]]:
    with client.websocket_connect(job["events_url"]) as ws:
        return collect_events(ws)


def _action(client: TestClient, project: str, action: str) -> list[dict[str, Any]]:
    response = client.post(f"/api/environments/{project}/actions", json={"action": action})
    assert response.status_code == 202, response.text
    return _finish(client, response.json())


def _activity(client: TestClient, project: str = "lab") -> dict[str, Any]:
    response = client.get(f"/api/environments/{project}/activity")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _rows(settings: Settings, query: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(settings.history_file) as db:
        return db.execute(query).fetchall()


def test_jobs_are_recorded_with_their_outcome(client: TestClient, settings: Settings) -> None:
    events = run_job(client, LAB)

    [row] = _rows(settings, "SELECT mode, project, status, message, finished_at FROM jobs")
    assert row[:4] == ("template", "lab", "succeeded", events[-1]["message"])
    assert row[4] is not None


def test_activity_lists_jobs_and_edits_newest_first(client: TestClient) -> None:
    run_job(client, LAB)
    assert _action(client, "lab", "stop")[-1]["type"] == "job.succeeded"
    edit = client.patch("/api/environments/lab", json={"notes": "SQLi done."}, headers=ORIGIN)
    assert edit.status_code == 200

    body = _activity(client)

    assert body["available"] is True
    entries = body["entries"]
    assert [(entry["kind"], entry["action"]) for entry in entries] == [
        ("audit", "edit"),
        ("job", "stop"),
        ("job", "template"),
    ]
    assert entries[0]["message"] == "Notes edited"
    assert entries[1]["status"] == "succeeded"
    assert entries[2]["message"] == "Environment 'lab' is ready at http://lab.localhost"


def test_activity_of_a_reused_name_starts_with_its_new_deployment(client: TestClient) -> None:
    run_job(client, LAB)
    client.patch("/api/environments/lab", json={"title": "First life"}, headers=ORIGIN)
    removal = client.delete("/api/environments/lab", headers=ORIGIN)
    assert _finish(client, removal.json())[-1]["type"] == "job.succeeded"
    run_job(client, {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "lab"})

    entries = _activity(client)["entries"]

    assert [(entry["kind"], entry["action"]) for entry in entries] == [("job", "template")]
    assert entries[0]["status"] == "succeeded"


def test_activity_needs_an_existing_environment(client: TestClient) -> None:
    assert client.get("/api/environments/nothing-here/activity").status_code == 404
    assert client.get("/api/environments/Bad_Name/activity").status_code == 422


def test_activity_reads_a_bounded_number_of_entries(client: TestClient) -> None:
    run_job(client, LAB)
    assert client.get("/api/environments/lab/activity?limit=200").status_code == 200
    assert client.get("/api/environments/lab/activity?limit=0").status_code == 422
    assert client.get("/api/environments/lab/activity?limit=201").status_code == 422


def test_plans_survive_a_restart(settings: Settings) -> None:
    translator = FakeTranslator(spec(decision="template", template_id="owasp-juice-shop"))
    keyed = settings.model_copy(update={"llm_api_key": TEST_API_KEY})

    with TestClient(create_app(keyed, lambda key: translator)) as first:
        planning = first.post("/api/plans", json={"prompt": "a vulnerable shop"}, headers=ORIGIN)
        done = _finish(first, planning.json())[-1]
        assert done["type"] == "job.succeeded", done["message"]
        plan_id = done["plan_id"]

    with TestClient(create_app(keyed, lambda key: translator)) as second:
        plan = second.get(f"/api/plans/{plan_id}")
        assert plan.status_code == 200, plan.text
        assert plan.json()["template_id"] == "owasp-juice-shop"
        job = second.post("/api/jobs", json={"mode": "plan", "plan_id": plan_id})
        assert job.status_code == 202, job.text
        assert _finish(second, job.json())[-1]["type"] == "job.succeeded"


BOOKS = spec(
    title="Books",
    summary="An audiobook server.",
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


def test_a_plan_with_many_secrets_is_kept_across_a_restart(settings: Settings) -> None:
    """The stored record accepts whatever the pipeline accepts: no limit of its own."""
    many = BOOKS.model_copy(update={"secrets": [f"SECRET_{index}" for index in range(17)]})
    translator = FakeTranslator(many)
    keyed = settings.model_copy(update={"llm_api_key": TEST_API_KEY})

    with TestClient(create_app(keyed, lambda key: translator)) as first:
        planning = first.post("/api/plans", json={"prompt": "books"}, headers=ORIGIN)
        done = _finish(first, planning.json())[-1]
        assert done["type"] == "job.succeeded", done["message"]

    with TestClient(create_app(keyed, lambda key: translator)) as second:
        plan = second.get(f"/api/plans/{done['plan_id']}")
        assert plan.status_code == 200, plan.text
        assert plan.json()["secrets"] == 17


def _plan_on_disk(settings: Settings, answer: StackSpec) -> tuple[Settings, str]:
    """Plan with a first server, then stop it: the plan is left in the database only."""
    keyed = settings.model_copy(update={"llm_api_key": TEST_API_KEY})
    translator = FakeTranslator(answer)
    with TestClient(create_app(keyed, lambda key: translator)) as first:
        planning = first.post("/api/plans", json={"prompt": "books"}, headers=ORIGIN)
        done = _finish(first, planning.json())[-1]
        assert done["type"] == "job.succeeded", done["message"]
    return keyed, done["plan_id"]


def _rewrite_stored_plan(settings: Settings, plan_id: str, body: str) -> None:
    with closing(sqlite3.connect(settings.history_file)) as db, db:
        db.execute("UPDATE plans SET body = ? WHERE plan_id = ?", (body, plan_id))


def _stored_plan(settings: Settings, plan_id: str) -> dict[str, Any]:
    with closing(sqlite3.connect(settings.history_file)) as db:
        (body,) = db.execute("SELECT body FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
    record: dict[str, Any] = json.loads(body)
    return record


def test_a_tampered_stored_plan_is_refused_at_deployment(settings: Settings) -> None:
    """The database is not trusted more than a request: a restored stack goes through the
    security policy again before anything is created."""
    keyed, plan_id = _plan_on_disk(settings, BOOKS)
    record = _stored_plan(keyed, plan_id)
    record["candidate"]["source"]["services"]["books"]["privileged"] = True
    _rewrite_stored_plan(keyed, plan_id, json.dumps(record))

    with TestClient(create_app(keyed, lambda key: FakeTranslator(BOOKS))) as client:
        job = client.post("/api/jobs", json={"mode": "plan", "plan_id": plan_id}, headers=ORIGIN)
        assert job.status_code == 202, job.text
        events = _finish(client, job.json())
        assert events[-1]["type"] == "job.failed"
        assert events[-1]["message"] == "The stack was rejected by the security policy."
        assert any("privileged" in event["message"] for event in events)
        assert client.get("/api/environments").json()["environments"] == []


def test_an_unreadable_stored_plan_is_ignored_at_startup(settings: Settings) -> None:
    keyed, plan_id = _plan_on_disk(settings, BOOKS)
    _rewrite_stored_plan(keyed, plan_id, '{"view": "not a plan"}')

    with TestClient(create_app(keyed, lambda key: FakeTranslator(BOOKS))) as client:
        assert client.get(f"/api/plans/{plan_id}").status_code == 404
        deploy = client.post("/api/jobs", json={"mode": "plan", "plan_id": plan_id}, headers=ORIGIN)
        assert deploy.status_code == 404


def test_a_stored_plan_whose_template_left_the_catalog_is_dropped(settings: Settings) -> None:
    keyed, plan_id = _plan_on_disk(
        settings, spec(decision="template", template_id="owasp-juice-shop")
    )
    record = _stored_plan(keyed, plan_id)
    record["template_id"] = "retired-shop"
    _rewrite_stored_plan(keyed, plan_id, json.dumps(record))

    with TestClient(create_app(keyed, lambda key: FakeTranslator(BOOKS))) as client:
        assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_a_plan_the_history_cannot_keep_still_reaches_review(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History is best effort: a plan it cannot record stays reviewable from memory."""

    def unrecordable(plan: object) -> str:
        raise ValueError("this plan does not fit the stored record")

    monkeypatch.setattr("app.services.orchestrator.plan_body", unrecordable)
    translator = FakeTranslator(BOOKS)
    keyed = settings.model_copy(update={"llm_api_key": TEST_API_KEY})
    with TestClient(create_app(keyed, lambda key: translator)) as client:
        planning = client.post("/api/plans", json={"prompt": "books"}, headers=ORIGIN)
        done = _finish(client, planning.json())[-1]
        assert done["type"] == "job.succeeded", done["message"]
        assert client.get(f"/api/plans/{done['plan_id']}").status_code == 200


@pytest.mark.anyio
async def test_a_job_cut_short_by_a_crash_is_marked_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path, retention_days=90)
    await store.open()
    await store.job_started(
        JobStart(
            job_id=uuid4(),
            mode="template",
            project="lab",
            service=None,
            message="Deployment of 'lab' accepted (template)",
            created_at=datetime.now(UTC),
        )
    )
    await store.close()  # the process died before the job ended

    reopened = HistoryStore(path, retention_days=90)
    await reopened.open()
    entries = await reopened.activity("lab", created_at=None)
    await reopened.close()

    assert entries is not None
    [entry] = entries
    assert (entry.status, entry.message) == ("failed", "Interrupted: the server stopped.")


@pytest.mark.anyio
async def test_old_entries_are_purged(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    then = datetime(2026, 1, 1, tzinfo=UTC)
    store = HistoryStore(path, retention_days=90, now=lambda: then)
    await store.open()
    await store.job_started(
        JobStart(
            job_id=uuid4(),
            mode="stop",
            project="lab",
            service=None,
            message="Stop of 'lab' accepted",
            created_at=then,
        )
    )
    await store.audit("edit", "Notes edited", project="lab")
    assert len(await store.activity("lab", created_at=None) or []) == 2
    await store.close()

    later = HistoryStore(path, retention_days=90, now=lambda: then + timedelta(days=91))
    await later.open()
    assert await later.activity("lab", created_at=None) == []
    await later.close()


def test_a_corrupt_history_file_is_set_aside(settings: Settings) -> None:
    settings.history_file.parent.mkdir(parents=True, exist_ok=True)
    settings.history_file.write_bytes(b"this is not a database " * 64)

    with TestClient(create_app(settings)) as client:
        assert run_job(client, LAB)[-1]["type"] == "job.succeeded"
        body = _activity(client)

    assert body["available"] is True
    assert [entry["action"] for entry in body["entries"]] == ["template"]
    names = [path.name for path in settings.history_file.parent.iterdir()]
    assert any(name.startswith("history.sqlite3.corrupt-") for name in names), names


def test_history_holds_no_secret_and_no_prompt(
    make_client: ClientFactory, settings: Settings
) -> None:
    database = {
        "name": "db",
        "image": "docker.io/library/mariadb:11.4.13",
        "purpose": "A database",
        "environment": [{"name": "MARIADB_ROOT_PASSWORD", "value": "${DB_ROOT_PASSWORD}"}],
        "volumes": [],
        "depends_on": [],
        "needs_internet": False,
    }
    translator = FakeTranslator(spec(services=[database], secrets=["DB_ROOT_PASSWORD"]))
    client = make_client(translator)
    prompt = "my private note: the budget database of the Lyon office"
    run_job(client, {"mode": "prompt", "prompt": prompt, "project_name": "budget"})

    env = (settings.workspaces_dir / "budget" / ".env").read_text(encoding="utf-8")
    secret = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("DB_"))
    with sqlite3.connect(settings.history_file) as db:
        dump = "\n".join(db.iterdump())
    assert secret not in dump
    assert "Lyon" not in dump


def test_the_history_file_is_owner_only(client: TestClient, settings: Settings) -> None:
    run_job(client, LAB)
    if os.name != "posix":
        pytest.skip("POSIX permissions")
    assert stat.S_IMODE(settings.history_file.stat().st_mode) == 0o600


def test_cancellations_and_key_changes_are_audited(
    make_client: ClientFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(translator_factory=lambda key: FakeTranslator(spec()))
    key = "sk-ant-api03-saved-from-the-ui-9f3c"
    saved = client.put("/api/settings/llm-key", json={"api_key": key}, headers=ORIGIN)
    assert saved.status_code == 200, saved.text
    assert client.delete("/api/settings/llm-key", headers=ORIGIN).status_code == 200

    pulling, release = threading.Event(), threading.Event()

    async def slow_pull(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, progress: ProgressSink
    ) -> None:
        pulling.set()
        await asyncio.to_thread(release.wait, 10)

    monkeypatch.setattr(SimulatedEngine, "pull", slow_pull)
    job = client.post("/api/jobs", json=LAB).json()
    try:
        assert pulling.wait(10)
        assert client.post(f"/api/jobs/{job['job_id']}/cancel", headers=ORIGIN).status_code == 202
        assert _finish(client, job)[-1]["type"] == "job.cancelled"
    finally:
        release.set()

    rows = _rows(settings, "SELECT action, project, message FROM audit ORDER BY id")
    assert ("settings", None, "Claude API key saved (ending …9f3c)") in rows
    assert ("settings", None, "Saved Claude API key removed") in rows
    assert ("cancel", "lab", "Cancellation requested") in rows
    with sqlite3.connect(settings.history_file) as db:
        assert key[:-4] not in "\n".join(db.iterdump())
