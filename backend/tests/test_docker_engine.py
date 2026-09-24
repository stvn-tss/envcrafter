import pytest

from app.engine.base import ServiceStatus
from app.engine.docker_compose import _describe, parse_ps_output


def test_ps_output_is_grouped_by_project() -> None:
    output = "demo\tglpi\trunning\thealthy\ndemo\tmariadb\trunning\t\nlab\tdvwa\texited\t\n"
    assert parse_ps_output(output) == {
        "demo": [
            ServiceStatus(service="glpi", state="running", health="healthy"),
            ServiceStatus(service="mariadb", state="running", health=None),
        ],
        "lab": [ServiceStatus(service="dvwa", state="exited", health=None)],
    }


@pytest.mark.parametrize(
    "line",
    [
        "../etc\tglpi\trunning\t",  # project label is not a slug
        "demo\tGLPI; rm -rf /\trunning\t",  # service label is not a slug
        "demo\tglpi\trunning\thealthy\textra",
        "demo glpi running",
        "\tglpi\trunning\t",  # no project label
        "demo\tglpi\trun ning\t",
    ],
)
def test_lines_that_are_not_envcrafter_shaped_are_dropped(line: str) -> None:
    # Labels are data: a container cannot inject another project or service name.
    assert parse_ps_output(line + "\n") == {}


def test_unknown_health_values_are_ignored() -> None:
    assert parse_ps_output("demo\tapp\tRunning\tweird\n") == {
        "demo": [ServiceStatus(service="app", state="running", health=None)]
    }


def test_command_labels_never_leak_paths() -> None:
    assert _describe(["ps", "--all"]) == "docker ps"
    assert _describe(["network", "inspect", "ec-x-edge"]) == "docker network inspect"
    compose = ["compose", "--progress", "plain", "--file", "C:/secret/compose.yaml", "up"]
    assert _describe(compose) == "docker compose up"
