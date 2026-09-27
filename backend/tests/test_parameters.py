"""Template parameters: typed options chosen before deploying, written to the workspace .env."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.policy.images import ImageAllowlist
from app.services.template_catalog import (
    InvalidParametersError,
    TemplateCatalog,
    TemplateCatalogError,
)
from app.workspace.manager import WorkspaceManager
from tests.conftest import ClientFactory, FakeTranslator, run_job, spec

REAL_TEMPLATES = Settings().templates_dir
DVWA = "security-lab/dvwa"
LAB = {"mode": "template", "template_id": "dvwa", "project_name": "lab"}
LEVEL: dict[str, Any] = {
    "type": "enum",
    "name": "SECURITY_LEVEL",
    "label": "Security level",
    "options": [{"value": "low", "label": "Low"}, {"value": "high", "label": "High"}],
    "default": "low",
}


def _env(settings: Settings, project: str = "lab") -> dict[str, str]:
    text = (settings.workspaces_dir / project / ".env").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines())


def _dvwa_with(
    tmp_path: Path, parameters: list[dict[str, Any]], environment: dict[str, str]
) -> Path:
    """A one-template catalog: DVWA with these parameters and extra environment values."""
    root = tmp_path / "templates"
    target = root / DVWA
    shutil.copytree(REAL_TEMPLATES / DVWA, target)
    manifest = yaml.safe_load((target / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["parameters"] = parameters
    (target / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    compose = yaml.safe_load((target / "compose.yaml").read_text(encoding="utf-8"))
    compose["services"]["dvwa"]["environment"].update(environment)
    (target / "compose.yaml").write_text(yaml.safe_dump(compose), encoding="utf-8")
    return root


# --- Deployments -----------------------------------------------------------------


def test_the_chosen_level_reaches_the_workspace(client: TestClient, settings: Settings) -> None:
    events = run_job(client, {**LAB, "parameters": {"SECURITY_LEVEL": "high"}})

    assert events[-1]["type"] == "job.succeeded", events[-1]["message"]
    assert _env(settings)["SECURITY_LEVEL"] == "high"
    compose = yaml.safe_load(
        (settings.workspaces_dir / "lab" / "compose.yaml").read_text(encoding="utf-8")
    )
    # Compose interpolates it from the .env file at deployment, like the secrets.
    assert compose["services"]["dvwa"]["environment"]["DEFAULT_SECURITY_LEVEL"] == (
        "${SECURITY_LEVEL}"
    )
    meta = json.loads((settings.workspaces_dir / "lab" / "meta.json").read_text(encoding="utf-8"))
    assert meta["parameters"] == {"SECURITY_LEVEL": "high"}
    assert client.get("/api/environments/lab").json()["parameters"] == {"SECURITY_LEVEL": "high"}
    assert "Options: Security level: High" in "\n".join(event["message"] for event in events)


def test_defaults_apply_when_nothing_is_chosen(client: TestClient, settings: Settings) -> None:
    run_job(client, LAB)
    assert _env(settings)["SECURITY_LEVEL"] == "low"


def test_ai_chosen_templates_use_the_defaults(
    make_client: ClientFactory, settings: Settings
) -> None:
    translator = FakeTranslator(spec(decision="template", template_id="owasp-juice-shop"))
    run_job(make_client(translator), {"mode": "prompt", "prompt": "a shop", "project_name": "shop"})
    assert _env(settings, "shop")["JUICE_SHOP_MODE"] == "default"


@pytest.mark.parametrize(
    "parameters",
    [
        {"SECURITY_LEVEL": "extreme"},  # not an option
        {"LEVEL": "low"},  # not a parameter of this template
        {"SECURITY_LEVEL": True},  # not an enum value
        {"EC_TZ": "Etc/UTC"},  # reserved prefix
        {f"PARAM_{index}": "x" for index in range(9)},  # more than eight
    ],
)
def test_invalid_parameters_are_refused(client: TestClient, parameters: dict[str, Any]) -> None:
    response = client.post("/api/jobs", json={**LAB, "parameters": parameters})
    assert response.status_code == 422, response.text
    assert client.get("/api/jobs").json()["jobs"] == []


@pytest.mark.parametrize(
    "value", ["low\nEVIL=1", "low EVIL", "$HOME", "a=b", "'low'", "", "x" * 65]
)
def test_parameter_values_cannot_inject_env_lines(client: TestClient, value: str) -> None:
    response = client.post("/api/jobs", json={**LAB, "parameters": {"SECURITY_LEVEL": value}})
    assert response.status_code == 422
    assert client.get("/api/jobs").json()["jobs"] == []


def test_parameter_values_are_not_redacted_from_logs(
    client: TestClient, settings: Settings
) -> None:
    run_job(client, {**LAB, "parameters": {"SECURITY_LEVEL": "impossible"}})
    values = WorkspaceManager(settings.workspaces_dir).read_secret_values("lab")
    assert "impossible" not in values
    assert len(values) == 2  # DB_PASSWORD and DB_ROOT_PASSWORD


def test_the_catalog_describes_the_parameters(client: TestClient) -> None:
    templates = {t["id"]: t for t in client.get("/api/templates").json()["templates"]}
    [level] = templates["dvwa"]["parameters"]
    assert (level["type"], level["name"], level["default"]) == ("enum", "SECURITY_LEVEL", "low")
    values = [option["value"] for option in level["options"]]
    assert values == ["low", "medium", "high", "impossible"]
    assert templates["glpi"]["parameters"] == []


# --- Catalog rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parameter", "environment", "message"),
    [
        (
            {**LEVEL, "default": "medium"},
            {"DEFAULT_SECURITY_LEVEL": "${SECURITY_LEVEL}"},
            "default",
        ),
        (
            {
                **LEVEL,
                "options": [{"value": "low", "label": "Low"}, {"value": "low", "label": "Again"}],
            },
            {"DEFAULT_SECURITY_LEVEL": "${SECURITY_LEVEL}"},
            "unique",
        ),
        (
            {**LEVEL, "options": [{"value": "low", "label": "Low"}]},
            {"DEFAULT_SECURITY_LEVEL": "${SECURITY_LEVEL}"},
            "at least 2",
        ),
        (LEVEL, {"DEFAULT_SECURITY_LEVEL": "low"}, "not used in compose.yaml"),
        (
            {**LEVEL, "name": "DB_PASSWORD"},
            {"DEFAULT_SECURITY_LEVEL": "${DB_PASSWORD}"},
            "differ from secret names",
        ),
        ({**LEVEL, "name": "EC_LEVEL"}, {"DEFAULT_SECURITY_LEVEL": "${EC_LEVEL}"}, "reserved"),
    ],
)
def test_inconsistent_parameters_stop_the_catalog(
    tmp_path: Path,
    allowlist: ImageAllowlist,
    parameter: dict[str, Any],
    environment: dict[str, str],
    message: str,
) -> None:
    root = _dvwa_with(tmp_path, [parameter], environment)
    with pytest.raises(TemplateCatalogError, match=message):
        TemplateCatalog.load(root, allowlist)


def test_a_variable_that_is_not_declared_is_still_refused(
    tmp_path: Path, allowlist: ImageAllowlist
) -> None:
    root = _dvwa_with(
        tmp_path,
        [LEVEL],
        {"DEFAULT_SECURITY_LEVEL": "${SECURITY_LEVEL}", "OTHER": "${UNDECLARED}"},
    )
    with pytest.raises(TemplateCatalogError, match="unknown variable"):
        TemplateCatalog.load(root, allowlist)


def test_boolean_parameters_resolve_to_true_or_false(
    tmp_path: Path, allowlist: ImageAllowlist
) -> None:
    flag = {"type": "boolean", "name": "HINTS", "label": "Show hints"}
    root = _dvwa_with(
        tmp_path,
        [LEVEL, flag],
        {"DEFAULT_SECURITY_LEVEL": "${SECURITY_LEVEL}", "SHOW_HINTS": "${HINTS}"},
    )
    template = TemplateCatalog.load(root, allowlist).get("dvwa")
    assert template is not None

    assert template.parameter_values({}) == {"SECURITY_LEVEL": "low", "HINTS": "false"}
    assert template.parameter_values({"HINTS": True})["HINTS"] == "true"
    with pytest.raises(InvalidParametersError, match="true or false"):
        template.parameter_values({"HINTS": "yes"})
    with pytest.raises(InvalidParametersError, match="one of: low, high"):
        template.parameter_values({"SECURITY_LEVEL": "medium"})
