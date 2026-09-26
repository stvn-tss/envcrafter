"""${EC_TZ}: the browser's time zone, written to each workspace for the templates to use."""

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.timezones import is_known_timezone
from tests.conftest import run_job


def _env(settings: Settings, project: str) -> dict[str, str]:
    text = (settings.workspaces_dir / project / ".env").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines())


def test_the_browser_time_zone_reaches_the_workspace(
    client: TestClient, settings: Settings
) -> None:
    events = run_job(
        client,
        {
            "mode": "template",
            "template_id": "media-stack",
            "project_name": "media",
            "timezone": "Europe/Paris",
        },
    )

    assert events[-1]["type"] == "job.succeeded"
    assert _env(settings, "media")["EC_TZ"] == "Europe/Paris"
    assert "Time zone: Europe/Paris" in [event["message"] for event in events]
    # The compose file keeps the reference; Compose reads the value from .env at start.
    compose = yaml.safe_load(
        (settings.workspaces_dir / "media" / "compose.yaml").read_text(encoding="utf-8")
    )
    assert compose["services"]["sonarr"]["environment"]["TZ"] == "${EC_TZ}"


def test_without_a_time_zone_the_server_default_is_used(
    client: TestClient, settings: Settings
) -> None:
    run_job(client, {"mode": "template", "template_id": "zabbix", "project_name": "watch"})
    assert _env(settings, "watch")["EC_TZ"] == "Etc/UTC"


def test_an_unknown_time_zone_falls_back_instead_of_failing(
    client: TestClient, settings: Settings
) -> None:
    events = run_job(
        client,
        {
            "mode": "template",
            "template_id": "owasp-juice-shop",
            "project_name": "shop",
            "timezone": "Mars/Olympus_Mons",
        },
    )

    assert events[-1]["type"] == "job.succeeded"
    assert _env(settings, "shop")["EC_TZ"] == "Etc/UTC"
    assert "Unknown time zone 'Mars/Olympus_Mons': using Etc/UTC" in [
        event["message"] for event in events
    ]


@pytest.mark.parametrize(
    "timezone", ["../../etc/passwd", "Europe/Paris;id", "$(id)", "/etc/localtime", "a" * 65]
)
def test_malformed_time_zones_are_refused(client: TestClient, timezone: str) -> None:
    response = client.post(
        "/api/jobs", json={"mode": "template", "template_id": "glpi", "timezone": timezone}
    )
    assert response.status_code == 422


def test_the_default_time_zone_setting_is_checked() -> None:
    assert Settings(timezone="America/New_York").timezone == "America/New_York"
    with pytest.raises(ValidationError):
        Settings(timezone="Nowhere/Land")


@pytest.mark.parametrize(
    ("name", "known"),
    [
        ("Europe/Paris", True),
        ("UTC", True),
        ("America/Argentina/Buenos_Aires", True),
        ("Europe/Nowhere", False),
        ("../zoneinfo/UTC", False),
        ("", False),
    ],
)
def test_time_zones_are_looked_up_not_opened(name: str, known: bool) -> None:
    assert is_known_timezone(name) is known
