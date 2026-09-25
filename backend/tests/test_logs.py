from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.engine.base import LogLine, StackHandle
from app.engine.simulated import SimulatedEngine
from app.services.log_streams import LogStreamer, TooManyLogStreamsError, redact
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
    (root / "demo" / ".env").write_text(
        "EC_PROJECT=demo\nAPP_TOKEN=abcdefghijkl\n", encoding="utf-8"
    )
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
