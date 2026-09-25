import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.plan import PlanView
from app.services.plan_store import PlanStore, StoredPlan
from tests.conftest import ClientFactory, FakeTranslator, collect_events, run_job, spec

BOOKS_SERVICE: dict[str, Any] = {
    "name": "books",
    "image": "docker.io/advplyr/audiobookshelf:2.36.1",
    "purpose": "Audiobooks",
    "environment": [],
    "volumes": [{"name": "config", "mount_path": "/config"}],
    "depends_on": [],
    "needs_internet": False,
}
BOOKS = spec(
    title="Books",
    summary="An audiobook server.",
    services=[BOOKS_SERVICE],
    expose={"service": "books", "port": 80},
)
DEPLOY_STEPS = [
    "security_validation",
    "workspace_setup",
    "image_pull",
    "network_setup",
    "container_deploy",
]


def plan(client: TestClient, prompt: str = "books please") -> list[dict[str, Any]]:
    response = client.post("/api/plans", json={"prompt": prompt})
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["mode"], job["project_name"], job["plan_id"]) == ("planning", None, None)
    with client.websocket_connect(job["events_url"]) as ws:
        return collect_events(ws)


def test_planning_analyses_and_validates_without_deploying(
    make_client: ClientFactory, settings: Settings
) -> None:
    translator = FakeTranslator(BOOKS)
    client = make_client(translator)

    events = plan(client)

    assert [step["key"] for step in events[0]["plan"]] == ["ai_translation", "security_validation"]
    assert events[0]["message"] == "AI analysis of the request accepted"
    done = events[-1]
    assert done["type"] == "job.succeeded"
    assert done["message"] == "Plan ready: Books"
    review = client.get(f"/api/plans/{done['plan_id']}").json()
    assert review["decision"] == "custom"
    assert (review["title"], review["summary"]) == ("Books", "An audiobook server.")
    assert review["services"] == [
        {
            "service": "books",
            "name": "Audiobookshelf 2.36",
            "image": "docker.io/advplyr/audiobookshelf:2.36.1",
            "purpose": "Audiobooks",
            "internet": False,
            "vulnerable": False,
            "web_access": "<project>.localhost",
        }
    ]
    assert (review["volumes"], review["secrets"], review["needs_internet"]) == (
        ["config"],
        0,
        False,
    )
    assert translator.prompts == ["books please"]
    assert not settings.workspaces_dir.exists() or not any(settings.workspaces_dir.iterdir())


def test_reviewed_plan_deploys_after_revalidation(
    make_client: ClientFactory, settings: Settings
) -> None:
    client = make_client(FakeTranslator(BOOKS))
    plan_id = plan(client)[-1]["plan_id"]

    events = run_job(client, {"mode": "plan", "plan_id": plan_id, "project_name": "library"})

    assert [step["key"] for step in events[0]["plan"]] == ["plan_loading", *DEPLOY_STEPS]
    assert "passed the schema and policy checks" in "\n".join(e["message"] for e in events)
    assert events[-1]["type"] == "job.succeeded"
    assert events[-1]["urls"][0]["url"] == "http://library.localhost"
    assert events[-1].get("plan_id") is None  # only planning jobs announce a plan
    meta = json.loads(
        (settings.workspaces_dir / "library" / "meta.json").read_text(encoding="utf-8")
    )
    assert (meta["origin"], meta["title"]) == ("prompt", "Books")


def test_a_plan_can_be_deployed_twice_under_different_names(make_client: ClientFactory) -> None:
    client = make_client(FakeTranslator(BOOKS))
    plan_id = plan(client)[-1]["plan_id"]

    for project in ("first", "second"):
        events = run_job(client, {"mode": "plan", "plan_id": plan_id, "project_name": project})
        assert events[-1]["type"] == "job.succeeded"
    again = client.post(
        "/api/jobs", json={"mode": "plan", "plan_id": plan_id, "project_name": "first"}
    )
    assert again.status_code == 409


def test_template_plans_list_the_template_components(make_client: ClientFactory) -> None:
    client = make_client(FakeTranslator(spec(decision="template", template_id="glpi")))
    review = client.get(f"/api/plans/{plan(client)[-1]['plan_id']}").json()
    assert (review["decision"], review["template_id"], review["secrets"]) == ("template", "glpi", 2)
    assert {service["service"] for service in review["services"]} == {"glpi", "mariadb"}
    glpi = next(service for service in review["services"] if service["service"] == "glpi")
    assert glpi["purpose"].startswith("Web application")


def test_rejected_plans_are_never_stored(make_client: ClientFactory) -> None:
    rogue = spec(services=[{**BOOKS_SERVICE, "image": "docker.io/library/nginx:1.29.0"}])
    events = plan(make_client(FakeTranslator(rogue)))
    assert events[-1]["type"] == "job.failed"
    assert events[-1]["message"] == "The stack was rejected by the security policy."
    assert events[-1].get("plan_id") is None


def test_plan_endpoints_validate_input(make_client: ClientFactory) -> None:
    client = make_client(FakeTranslator(BOOKS))
    assert client.get(f"/api/plans/{uuid4()}").status_code == 404
    assert client.get("/api/plans/not-a-uuid").status_code == 422
    unknown = client.post("/api/jobs", json={"mode": "plan", "plan_id": str(uuid4())})
    assert unknown.status_code == 404
    plan_id = plan(client)[-1]["plan_id"]
    # The client can never send the stack itself: only the plan id travels.
    smuggled = {"mode": "plan", "plan_id": plan_id, "services": {"x": {"privileged": True}}}
    assert client.post("/api/jobs", json=smuggled).status_code == 422
    for body in (
        {"prompt": "hi"},
        {"prompt": "deploy" + chr(0x202E)},
        {"prompt": "ok", "mode": "prompt"},
    ):
        assert client.post("/api/plans", json=body).status_code == 422
    foreign = client.post(
        "/api/plans", json={"prompt": "books"}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    not_json = client.post(
        "/api/plans", content=b'{"prompt": "books"}', headers={"Content-Type": "text/plain"}
    )
    assert not_json.status_code == 415


def test_planning_without_llm_is_unavailable(client: TestClient) -> None:
    assert client.post("/api/plans", json={"prompt": "books"}).status_code == 503


def test_expired_plans_cannot_be_reviewed_or_deployed(
    make_client: ClientFactory, settings: Settings
) -> None:
    settings.plan_ttl_seconds = 0
    client = make_client(FakeTranslator(BOOKS))
    plan_id = plan(client)[-1]["plan_id"]
    assert client.get(f"/api/plans/{plan_id}").status_code == 404
    assert client.post("/api/jobs", json={"mode": "plan", "plan_id": plan_id}).status_code == 404


def _stored(store: PlanStore) -> StoredPlan:
    created_at, expires_at = store.window()
    view = PlanView(
        plan_id=uuid4(),
        created_at=created_at,
        expires_at=expires_at,
        decision="custom",
        title="t",
        summary="",
        explanation="",
        template_id=None,
        services=[],
        volumes=[],
        secrets=0,
        needs_internet=False,
    )
    return StoredPlan(view=view, candidate=None, template=None)  # type: ignore[arg-type]


def test_plan_store_expires_and_evicts_the_oldest() -> None:
    clock = [datetime(2026, 9, 24, tzinfo=UTC)]
    store = PlanStore(ttl_seconds=60, max_plans=2, now=lambda: clock[0])
    first, second, third = _stored(store), _stored(store), _stored(store)
    for item in (first, second, third):
        store.add(item)
    assert store.get(first.view.plan_id) is None  # evicted: only two plans are kept
    assert store.get(third.view.plan_id) is third
    clock[0] += timedelta(seconds=61)
    assert store.get(third.view.plan_id) is None  # expired
