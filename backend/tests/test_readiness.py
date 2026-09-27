"""Readiness: what a deployment needs (capacity check) and whether the machine is set up."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import HostResources, RuntimeCheck
from app.engine.host import read_host_resources
from app.engine.simulated import SimulatedEngine
from tests.conftest import ClientFactory, run_job

MEDIA = {"mode": "template", "template_id": "media-stack", "project_name": "media"}


def test_readiness_counts_missing_images_until_a_deployment_pulls_them(
    client: TestClient,
) -> None:
    before = client.get("/api/templates/media-stack/readiness").json()
    assert (before["images_total"], before["images_missing"]) == (5, 5)
    assert before["download_mb"] == 1048
    assert (before["memory_mb"], before["first_start_seconds"]) == (750, 60)
    assert before["warnings"] == []

    run_job(client, MEDIA)

    after = client.get("/api/templates/media-stack/readiness").json()
    assert (after["images_missing"], after["download_mb"]) == (0, 0)


def test_readiness_warns_about_memory_and_disk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def tight(self: SimulatedEngine) -> HostResources:
        return HostResources(
            memory_total_mb=2048, memory_available_mb=500, disk_free_mb=2000, disk_is_virtual=False
        )

    monkeypatch.setattr(SimulatedEngine, "resources", tight)
    body = client.get("/api/templates/media-stack/readiness").json()

    assert body["warnings"] == ["memory", "disk"]
    assert body["resources"] == {
        "memory_total_mb": 2048,
        "memory_available_mb": 500,
        "disk_free_mb": 2000,
        "disk_is_virtual": False,
    }


def test_enough_room_raises_no_warning(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Right above both thresholds: 1048 MB to download needs 3 x 1048 + 1024 MB free."""

    async def roomy(self: SimulatedEngine) -> HostResources:
        return HostResources(
            memory_total_mb=4096, memory_available_mb=750, disk_free_mb=4168, disk_is_virtual=False
        )

    monkeypatch.setattr(SimulatedEngine, "resources", roomy)
    assert client.get("/api/templates/media-stack/readiness").json()["warnings"] == []


def test_readiness_input_validation(client: TestClient) -> None:
    assert client.get("/api/templates/ghost/readiness").status_code == 404
    assert client.get("/api/templates/Bad_Id/readiness").status_code == 422


def test_system_checks_on_the_simulated_engine(client: TestClient) -> None:
    checks = client.get("/api/system").json()["checks"]
    assert [(check["name"], check["status"]) for check in checks] == [
        ("docker", "ok"),
        ("proxy", "ok"),
        ("memory", "ok"),
        ("disk", "ok"),
        ("llm", "info"),
    ]


def test_system_checks_report_problems(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(self: SimulatedEngine) -> list[RuntimeCheck]:
        return [
            RuntimeCheck("docker", False, "Docker does not answer through the socket proxy."),
            RuntimeCheck("proxy", False, "Unknown while Docker does not answer."),
        ]

    async def unknown(self: SimulatedEngine) -> HostResources:
        return HostResources(
            memory_total_mb=None, memory_available_mb=None, disk_free_mb=3000, disk_is_virtual=True
        )

    monkeypatch.setattr(SimulatedEngine, "diagnose", down)
    monkeypatch.setattr(SimulatedEngine, "resources", unknown)
    checks = {check["name"]: check for check in client.get("/api/system").json()["checks"]}

    assert checks["docker"]["status"] == "error"
    assert checks["proxy"]["status"] == "error"
    assert checks["memory"]["status"] == "info"
    assert checks["disk"]["status"] == "warning"
    assert "Docker Desktop" in checks["disk"]["detail"]


def test_read_host_resources(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:        8000000 kB\nMemFree:  100 kB\nMemAvailable:    6000000 kB\n",
        encoding="ascii",
    )
    resources = read_host_resources(tmp_path / "missing" / "dir", meminfo, "6.8.0-generic")
    assert (resources.memory_total_mb, resources.memory_available_mb) == (7812, 5859)
    assert resources.disk_free_mb is not None and resources.disk_free_mb > 0
    assert resources.disk_is_virtual is False
    unknown = read_host_resources(tmp_path, tmp_path / "absent", "6.8.0-generic")
    assert (unknown.memory_total_mb, unknown.memory_available_mb) == (None, None)


@pytest.mark.parametrize("release", ["6.18.33.2-microsoft-standard-WSL2", "6.10.14-linuxkit"])
def test_read_host_resources_flags_docker_desktop(tmp_path: Path, release: str) -> None:
    assert read_host_resources(tmp_path, tmp_path / "absent", release).disk_is_virtual is True


def test_docker_desktop_disk_space_is_only_informative(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk image reports about 1 TB whatever the host drive has left: plenty of room
    there proves nothing, so the check informs instead of saying ok."""

    async def desktop(self: SimulatedEngine) -> HostResources:
        return HostResources(
            memory_total_mb=8192,
            memory_available_mb=6144,
            disk_free_mb=1_000_000,
            disk_is_virtual=True,
        )

    monkeypatch.setattr(SimulatedEngine, "resources", desktop)
    checks = {check["name"]: check for check in client.get("/api/system").json()["checks"]}

    assert checks["disk"]["status"] == "info"
    assert "drive that holds it" in checks["disk"]["detail"]


def test_readiness_reads_docker_once_per_interval(
    make_client: ClientFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any web page can send GET requests to the local API, and each reading starts docker
    processes: within the cache window, repeated requests reuse one reading."""
    settings.readiness_cache_seconds = 60
    inspected: list[int] = []
    diagnosed: list[int] = []
    missing_images, diagnose = SimulatedEngine.missing_images, SimulatedEngine.diagnose

    async def counting_missing(self: SimulatedEngine, images: list[str]) -> list[str]:
        inspected.append(len(images))
        return await missing_images(self, images)

    async def counting_diagnose(self: SimulatedEngine) -> list[RuntimeCheck]:
        diagnosed.append(1)
        return await diagnose(self)

    monkeypatch.setattr(SimulatedEngine, "missing_images", counting_missing)
    monkeypatch.setattr(SimulatedEngine, "diagnose", counting_diagnose)
    client = make_client()

    for _ in range(3):
        assert client.get("/api/templates/media-stack/readiness").status_code == 200
        assert client.get("/api/templates/glpi/readiness").status_code == 200
        assert client.get("/api/system").status_code == 200

    assert inspected == [5, 2]  # one reading per template
    assert diagnosed == [1]
