import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.engine.base import EngineError, LogLine, LogSink, ServiceStatus, StackHandle
from app.engine.docker_compose import (
    DockerComposeEngine,
    _describe,
    _drain_stderr,
    parse_log_line,
    parse_ps_output,
)


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


def _engine() -> DockerComposeEngine:
    return DockerComposeEngine(
        docker_binary="docker",
        docker_host="tcp://socket-proxy-api:2375",
        traefik_container="envcrafter-traefik",
        pull_timeout=10,
        start_timeout=30,
        stop_timeout=10,
    )


def _stack(tmp_path: Path) -> StackHandle:
    return StackHandle(
        project="demo",
        compose_project="ec-demo",
        compose_file=tmp_path / "compose.yaml",
        edge_network="ec-demo-edge",
    )


class _Recorder:
    """Fake `_capture`/`_run`: records the verb of every attempted command, including one
    that is about to raise, and plays back canned `_capture` results in call order."""

    def __init__(self, capture_effects: Sequence[str | Exception]) -> None:
        self.calls: list[str] = []
        self._capture_effects = list(capture_effects)

    async def capture(self, args: list[str], *, timeout: float) -> str:  # noqa: ASYNC109
        self.calls.append(_describe(args))
        effect = self._capture_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    async def run(self, args: list[str], log: LogSink, *, timeout: float) -> None:  # noqa: ASYNC109
        self.calls.append(_describe(args))


def _wire(
    engine: DockerComposeEngine, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "_capture", recorder.capture)
    monkeypatch.setattr(engine, "_run", recorder.run)


@pytest.mark.anyio
async def test_start_only_ups_when_the_network_exists_and_traefik_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine()
    recorder = _Recorder(["envcrafter-traefik "])  # inspect: Traefik already a member
    _wire(engine, recorder, monkeypatch)

    await engine.start(_stack(tmp_path), lambda _message: None)

    assert recorder.calls == ["docker network inspect", "docker compose up"]


@pytest.mark.anyio
async def test_start_reattaches_traefik_before_up_when_the_network_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine()
    recorder = _Recorder([""])  # inspect: network exists, Traefik is not a member
    _wire(engine, recorder, monkeypatch)

    await engine.start(_stack(tmp_path), lambda _message: None)

    assert recorder.calls == [
        "docker network inspect",
        "docker network connect",
        "docker compose up",
    ]


@pytest.mark.anyio
async def test_start_tolerates_a_missing_network_and_attaches_traefik_after_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network removed outside EnvCrafter must not fail Start: `compose up` recreates
    it, and Traefik is attached once the network is guaranteed to exist."""
    engine = _engine()
    recorder = _Recorder(
        [
            EngineError("`docker network inspect` failed (exit code 1)"),  # pre-check: missing
            "",  # post-check: network now exists (created by `up`), Traefik not a member yet
        ]
    )
    _wire(engine, recorder, monkeypatch)

    await engine.start(_stack(tmp_path), lambda _message: None)

    assert recorder.calls == [
        "docker network inspect",
        "docker compose up",
        "docker network inspect",
        "docker network connect",
    ]


@pytest.mark.anyio
async def test_start_still_raises_when_traefik_cannot_be_attached_after_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine failure of the post-`up` attach (network still unreachable, or the
    Traefik container itself is gone) must fail the job, unlike the tolerated pre-check."""
    engine = _engine()
    recorder = _Recorder(
        [
            EngineError("`docker network inspect` failed (exit code 1)"),
            EngineError("`docker network inspect` failed (exit code 1)"),
        ]
    )
    _wire(engine, recorder, monkeypatch)

    with pytest.raises(EngineError):
        await engine.start(_stack(tmp_path), lambda _message: None)

    assert recorder.calls == [
        "docker network inspect",
        "docker compose up",
        "docker network inspect",
    ]


def test_log_lines_are_parsed_and_sanitized() -> None:
    # \u202e is RIGHT-TO-LEFT OVERRIDE: written as an escape, never as a literal
    # character, so this source file never carries a raw bidi control point.
    raw = "2026-09-24T20:34:45.398937136Z \x1b[32mGET /\x1b[0m\x07 ok\u202e\n"
    assert parse_log_line(raw) == LogLine(
        timestamp=datetime(2026, 9, 24, 20, 34, 45, 398937, tzinfo=UTC), text="GET / ok"
    )
    assert parse_log_line("no timestamp here") == LogLine(timestamp=None, text="no timestamp here")
    assert parse_log_line("\n") is None
    # Not length-capped here: capping is LogStreamer's job, after redaction (see
    # test_logs.py) so that a secret straddling the cap can't leak its surviving prefix.
    long_line = parse_log_line("x" * 5000)
    assert long_line is not None and len(long_line.text) == 5000


async def _aiter(items: list[bytes]) -> AsyncGenerator[bytes, None]:
    for item in items:
        await asyncio.sleep(0)  # a real pipe read suspends; give other tasks a turn too
        yield item


class _FakeProcess:
    """Stands in for `asyncio.subprocess.Process`: no real subprocess is spawned."""

    def __init__(self, *, stdout: list[bytes], stderr: list[bytes], returncode: int) -> None:
        self.stdout = _aiter(stdout)
        self.stderr = _aiter(stderr)
        self._returncode = returncode
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        await asyncio.sleep(0)  # a real `wait()` suspends until the process actually exits
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.anyio
async def test_drain_stderr_logs_bounded_lines_server_side(
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_line = ("x" * 1000).encode()
    process = _FakeProcess(stdout=[], stderr=[b"short warning\n", long_line + b"\n"], returncode=0)

    with caplog.at_level(logging.WARNING):
        await _drain_stderr(process, "demo", "app")  # type: ignore[arg-type]

    messages = [record.message for record in caplog.records]
    assert any("short warning" in message for message in messages)
    assert all(len(message) < 400 for message in messages), messages


@pytest.mark.anyio
async def test_logs_keeps_stderr_out_of_the_client_stream_and_logs_it_server_side(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    process = _FakeProcess(
        stdout=[b"2026-09-24T20:34:45.000000000Z hello\n"],
        stderr=[b"unable to reach tcp://socket-proxy-api:2375: connection refused\n"],
        returncode=1,
    )
    captured_kwargs: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    engine = _engine()

    with caplog.at_level(logging.WARNING):
        lines = [line async for line in engine.logs(_stack(tmp_path), "app", tail=10)]

    # stderr is on its own pipe, never merged into the stream the caller reads.
    assert captured_kwargs["stderr"] == asyncio.subprocess.PIPE
    # Only container output (stdout) reaches the caller; stderr never does.
    assert [line.text for line in lines] == ["hello"]
    server_log = "\n".join(record.message for record in caplog.records)
    assert "socket-proxy-api" in server_log
    assert "connection refused" in server_log
    assert "exited with code 1" in server_log
    assert not process.killed  # the process exited on its own; nothing to kill


@pytest.mark.anyio
async def test_recent_logs_reads_a_bounded_tail_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine()
    argv: list[list[str]] = []

    async def capture(args: list[str], *, timeout: float) -> str:  # noqa: ASYNC109
        argv.append(args)
        return "2026-09-26T10:00:00.123456789Z \x1b[31mboom\x1b[0m\n\n2026-09-26T10:00:01Z bye\n"

    monkeypatch.setattr(engine, "_capture", capture)

    lines = await engine.recent_logs(_stack(tmp_path), "app", tail=30)

    assert [line.text for line in lines] == ["boom", "bye"]
    assert lines[0].timestamp is not None
    [args] = argv
    assert args[-3:] == ["--tail", "30", "app"]
    assert "--follow" not in args


@pytest.mark.anyio
async def test_one_service_is_stopped_and_started_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine()
    runs: list[list[str]] = []

    async def run(args: list[str], log: LogSink, *, timeout: float) -> None:  # noqa: ASYNC109
        runs.append(args)

    monkeypatch.setattr(engine, "_run", run)
    await engine.stop(_stack(tmp_path), lambda _line: None, service="sonarr")
    await engine.start(_stack(tmp_path), lambda _line: None, service="sonarr")

    stop, start = runs
    assert stop[-4:] == ["stop", "--timeout", "10", "sonarr"]
    assert start[-7:] == ["up", "--detach", "--wait", "--wait-timeout", "30", "--no-deps", "sonarr"]
    with pytest.raises(EngineError):
        await engine.stop(_stack(tmp_path), lambda _line: None, service="../evil")
