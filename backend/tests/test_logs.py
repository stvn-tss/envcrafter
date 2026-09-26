from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.engine.base import LogLine, StackHandle
from app.engine.simulated import SimulatedEngine
from app.services.log_streams import (
    MAX_LOG_LINE_LENGTH,
    LogStreamer,
    TooManyLogStreamsError,
    redact,
)
from app.workspace.manager import WorkspaceManager
from tests.conftest import ClientFactory, run_job

SHOP_LOGS = "/ws/environments/shop/logs?service=juice-shop"


def deploy_shop(client: TestClient) -> None:
    run_job(client, {"mode": "template", "template_id": "owasp-juice-shop", "project_name": "shop"})


def test_logs_stream_the_requested_service(client: TestClient) -> None:
    deploy_shop(client)
    with client.websocket_connect(f"{SHOP_LOGS}&tail=3") as ws:
        frames = [ws.receive_json() for _ in range(3)]
    assert [frame["type"] for frame in frames] == ["line"] * 3
    assert frames[0]["text"] == "[simulated] juice-shop: log line 1"
    assert frames[0]["timestamp"] is not None


def test_logs_redact_generated_secrets(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_job(client, {"mode": "template", "template_id": "glpi", "project_name": "desk"})
    env = (settings.workspaces_dir / "desk" / ".env").read_text(encoding="utf-8")
    secret = next(
        line.split("=", 1)[1] for line in env.splitlines() if line.startswith("DB_PASSWORD=")
    )

    async def leaky_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        yield LogLine(timestamp=None, text=f"password is {secret}!")
        yield LogLine(timestamp=None, text="plain line")

    monkeypatch.setattr(SimulatedEngine, "logs", leaky_logs)
    with client.websocket_connect("/ws/environments/desk/logs?service=mariadb") as ws:
        first, second, end = ws.receive_json(), ws.receive_json(), ws.receive_json()

    assert first["text"] == "password is [redacted]!"
    assert second["text"] == "plain line"
    assert end == {"type": "end", "message": "Log stream ended."}


@pytest.mark.parametrize(
    "url", ["/ws/environments/ghost/logs?service=app", "/ws/environments/shop/logs?service=mariadb"]
)
def test_unknown_environment_or_service_closes_with_4404(client: TestClient, url: str) -> None:
    deploy_shop(client)
    with client.websocket_connect(url) as ws, pytest.raises(WebSocketDisconnect) as exc_info:
        ws.receive_json()
    assert exc_info.value.code == 4404


@pytest.mark.parametrize(
    "url",
    [
        "/ws/environments/Bad_Name/logs?service=app",
        "/ws/environments/shop/logs?service=../etc",
        f"{SHOP_LOGS}&tail=-1",
        "/ws/environments/shop/logs",
    ],
)
def test_malformed_log_requests_are_rejected(client: TestClient, url: str) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(url) as ws:
        ws.receive_json()
    assert exc_info.value.code == 1008


def test_logs_reject_foreign_origins(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(SHOP_LOGS, headers={"Origin": "https://evil.example"}),
    ):
        pass
    assert exc_info.value.code == 1008


def test_client_frames_close_the_log_stream(client: TestClient) -> None:
    deploy_shop(client)
    with client.websocket_connect(f"{SHOP_LOGS}&tail=1") as ws:
        ws.receive_json()
        ws.send_text("exec sh")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
    assert exc_info.value.code == 1008


def test_oversized_tail_is_clamped_to_the_configured_maximum(
    make_client: ClientFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.log_tail_max = 25
    client = make_client()
    deploy_shop(client)
    tails: list[int] = []

    async def recording_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        tails.append(tail)
        yield LogLine(timestamp=None, text="line")

    monkeypatch.setattr(SimulatedEngine, "logs", recording_logs)
    with client.websocket_connect(f"{SHOP_LOGS}&tail={10**6}") as ws:
        assert ws.receive_json()["text"] == "line"

    assert tails == [25]


def test_log_streams_are_capped(make_client: ClientFactory, settings: Settings) -> None:
    settings.max_log_streams = 1
    client = make_client()
    deploy_shop(client)
    with client.websocket_connect(f"{SHOP_LOGS}&tail=1") as first:
        first.receive_json()
        with (
            client.websocket_connect(SHOP_LOGS) as second,
            pytest.raises(WebSocketDisconnect) as exc_info,
        ):
            second.receive_json()
    assert exc_info.value.code == 1013


def test_redact_replaces_every_occurrence() -> None:
    assert redact("a S3CRETVALUE b S3CRETVALUE", ["S3CRETVALUE"]) == "a [redacted] b [redacted]"
    assert redact("nothing here", []) == "nothing here"


def _workspace(root: Path) -> WorkspaceManager:
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "compose.yaml").write_text(
        "name: ec-demo\nservices:\n  app:\n    image: busybox\n", encoding="utf-8"
    )
    # EC_HOSTNAME and EC_TZ are long enough (>= 8) to be taken for secrets if builtins were
    # not excluded: every "Europe/Paris" in the logs would then read [redacted].
    (root / "demo" / ".env").write_text(
        "EC_PROJECT=demo\nEC_HOSTNAME=demo.localhost\nEC_TZ=Europe/Paris\nAPP_TOKEN=abcdefghijkl\n",
        encoding="utf-8",
    )
    return WorkspaceManager(root)


def _workspace_with_secret(root: Path, secret: str) -> WorkspaceManager:
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "compose.yaml").write_text(
        "name: ec-demo\nservices:\n  app:\n    image: busybox\n", encoding="utf-8"
    )
    (root / "demo" / ".env").write_text(f"EC_PROJECT=demo\nAPP_TOKEN={secret}\n", encoding="utf-8")
    return WorkspaceManager(root)


@pytest.mark.anyio
async def test_streamer_releases_its_slot(tmp_path: Path) -> None:
    streamer = LogStreamer(
        workspaces=_workspace(tmp_path), engine=SimulatedEngine(delay=0), max_streams=1
    )
    async with streamer.open("demo", "app", tail=1) as lines:
        assert (await anext(lines)).text == "[simulated] app: log line 1"
        with pytest.raises(TooManyLogStreamsError):
            async with streamer.open("demo", "app", tail=1):
                pass
    async with streamer.open("demo", "app", tail=1) as lines:
        assert await anext(lines)


def test_secret_values_exclude_builtin_variables(tmp_path: Path) -> None:
    assert _workspace(tmp_path).read_secret_values("demo") == ["abcdefghijkl"]


def test_legacy_workspace_without_env_has_no_secrets(tmp_path: Path) -> None:
    workspaces = _workspace(tmp_path)
    (tmp_path / "demo" / ".env").unlink()
    assert workspaces.read_secret_values("demo") == []


def _unreadable_env(root: Path) -> WorkspaceManager:
    workspaces = _workspace(root)
    env = root / "demo" / ".env"
    env.unlink()
    env.mkdir()  # reading it fails with an OSError other than FileNotFoundError
    return workspaces


def test_unreadable_env_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(OSError) as exc_info:
        _unreadable_env(tmp_path).read_secret_values("demo")
    assert not isinstance(exc_info.value, FileNotFoundError)


@pytest.mark.anyio
async def test_streamer_refuses_to_stream_without_its_secrets(tmp_path: Path) -> None:
    streamer = LogStreamer(
        workspaces=_unreadable_env(tmp_path), engine=SimulatedEngine(delay=0), max_streams=1
    )
    with pytest.raises(OSError):
        async with streamer.open("demo", "app", tail=1) as lines:
            pytest.fail(f"streamed unredacted logs: {await anext(lines)}")
    assert streamer._open == 0  # the slot is released


def test_logs_websocket_closes_when_secrets_are_unreadable(
    client: TestClient, settings: Settings
) -> None:
    deploy_shop(client)
    env = settings.workspaces_dir / "shop" / ".env"
    env.unlink()
    env.mkdir()
    # The error escapes the handler: the ASGI server logs it and closes the socket without
    # details (the test client re-raises it instead). No log line is ever sent.
    with pytest.raises(OSError), client.websocket_connect(SHOP_LOGS) as ws:
        pytest.fail(f"streamed unredacted logs: {ws.receive_json()}")


@pytest.mark.anyio
async def test_streamer_caps_line_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "x" * 5000

    async def long_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        yield LogLine(timestamp=None, text=long_text)

    monkeypatch.setattr(SimulatedEngine, "logs", long_logs)
    streamer = LogStreamer(
        workspaces=_workspace(tmp_path), engine=SimulatedEngine(delay=0), max_streams=1
    )
    async with streamer.open("demo", "app", tail=1) as lines:
        line = await anext(lines)

    assert len(line.text) == MAX_LOG_LINE_LENGTH


@pytest.mark.anyio
async def test_streamer_redacts_a_secret_straddling_the_length_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 25-character secret starting at index 1990 straddles the 2000-character cap:
    # capping before redaction would leave its first 10 characters ("[0:10]") exposed.
    secret = "TOPSECRETVALUE1234567890"  # noqa: S105 - a test fixture value, not a real secret
    text = ("x" * 1990) + secret + ("y" * 500)

    async def leaky_logs(
        self: SimulatedEngine, stack: StackHandle, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        yield LogLine(timestamp=None, text=text)

    monkeypatch.setattr(SimulatedEngine, "logs", leaky_logs)
    streamer = LogStreamer(
        workspaces=_workspace_with_secret(tmp_path, secret),
        engine=SimulatedEngine(delay=0),
        max_streams=1,
    )
    async with streamer.open("demo", "app", tail=1) as lines:
        line = await anext(lines)

    assert secret not in line.text
    assert secret[:10] not in line.text
    assert len(line.text) <= MAX_LOG_LINE_LENGTH
