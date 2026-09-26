"""A failed startup keeps its cause: the logs of the services that never became ready are
copied into the job, secrets redacted, before the rollback deletes the containers."""

from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.engine.base import EngineError, LogLine, LogSink, ProgressSink, ServiceStatus, StackHandle
from app.engine.simulated import SimulatedEngine
from tests.conftest import run_job

GLPI = {"mode": "template", "template_id": "glpi", "project_name": "desk"}


async def _broken_start(
    self: SimulatedEngine, stack: StackHandle, log: LogSink, *, service: str | None = None
) -> None:
    raise EngineError("`docker compose up` failed (exit code 1)")


async def _unhealthy_glpi(
    self: SimulatedEngine, stacks: Sequence[StackHandle]
) -> dict[str, list[ServiceStatus]]:
    return {
        stack.project: [
            ServiceStatus(service="glpi", state="running", health="unhealthy"),
            ServiceStatus(service="mariadb", state="running", health="healthy"),
        ]
        for stack in stacks
    }


def _messages(events: list[dict[str, object]]) -> list[str]:
    return [str(event["message"]) for event in events]


def test_logs_of_unready_services_are_kept_and_redacted(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    passwords: list[str] = []

    async def recent_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> list[LogLine]:
        env = (stack.compose_file.parent / ".env").read_text(encoding="utf-8")
        password = next(
            line.split("=", 1)[1] for line in env.splitlines() if line.startswith("DB_PASSWORD=")
        )
        passwords.append(password)
        return [
            LogLine(timestamp=None, text=f"Connecting with password {password}"),
            LogLine(timestamp=None, text="Fatal: database is not reachable"),
        ]

    monkeypatch.setattr(SimulatedEngine, "start", _broken_start)
    monkeypatch.setattr(SimulatedEngine, "status", _unhealthy_glpi)
    monkeypatch.setattr(SimulatedEngine, "recent_logs", recent_logs)

    events = run_job(client, GLPI)
    messages = _messages(events)

    assert events[-1]["type"] == "job.failed"
    report = messages.index("Last log lines of glpi (unhealthy):")
    assert messages[report + 1 : report + 3] == [
        "  glpi | Connecting with password [redacted]",
        "  glpi | Fatal: database is not reachable",
    ]
    assert report < messages.index("Rolling back: removing everything created for this project")
    assert not any("mariadb (" in message for message in messages)  # healthy: not reported
    assert passwords and not any(passwords[0] in message for message in messages)
    assert not (settings.workspaces_dir / "desk").exists()  # the rollback still ran


def test_failures_before_startup_read_no_logs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def broken_pull(
        self: SimulatedEngine, stack: StackHandle, log: LogSink, progress: ProgressSink
    ) -> None:
        raise EngineError("`docker compose pull` failed (exit code 1)")

    async def recent_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> list[LogLine]:
        calls.append(service)
        return []

    monkeypatch.setattr(SimulatedEngine, "pull", broken_pull)
    monkeypatch.setattr(SimulatedEngine, "recent_logs", recent_logs)

    events = run_job(client, GLPI)

    assert events[-1]["type"] == "job.failed"
    assert calls == []


def test_unreadable_logs_never_block_the_rollback(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def recent_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> list[LogLine]:
        raise EngineError("`docker compose logs` failed (exit code 1)")

    monkeypatch.setattr(SimulatedEngine, "start", _broken_start)
    monkeypatch.setattr(SimulatedEngine, "status", _unhealthy_glpi)
    monkeypatch.setattr(SimulatedEngine, "recent_logs", recent_logs)

    events = run_job(client, GLPI)

    assert events[-1]["type"] == "job.failed"
    assert events[-1]["message"] == "`docker compose up` failed (exit code 1)"
    assert not (settings.workspaces_dir / "desk").exists()
