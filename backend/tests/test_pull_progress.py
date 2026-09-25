from typing import Any

import pytest

from app.engine.pull_progress import PullProgressTracker, format_bytes

BUSYBOX = "Image docker.io/library/busybox:1.37.0"
ALPINE = "Image docker.io/library/alpine:3.22.1"
# Captured from `docker compose --progress json pull` (Compose v5.5.1), trimmed.
SAMPLE: list[dict[str, Any]] = [
    {"id": ALPINE, "status": "Working", "text": "Pulling"},
    {"id": BUSYBOX, "status": "Working", "text": "Pulling"},
    {
        "id": "68fe9bff2ad4",
        "parent_id": BUSYBOX,
        "status": "Working",
        "text": "Pulling fs layer",
        "details": "0B",
    },
    {
        "id": "9824c27679d3",
        "parent_id": ALPINE,
        "status": "Working",
        "text": "Pulling fs layer",
        "details": "0B",
    },
    {
        "id": "ff5e5806f15b",
        "parent_id": ALPINE,
        "status": "Done",
        "text": "Download complete",
        "details": "0B",
        "percent": 100,
    },
    {
        "id": "68fe9bff2ad4",
        "parent_id": BUSYBOX,
        "status": "Working",
        "text": "Downloading",
        "details": "1.049MB",
        "current": 1048576,
        "total": 2211514,
        "percent": 47,
    },
    {
        "id": "9824c27679d3",
        "parent_id": ALPINE,
        "status": "Working",
        "text": "Downloading",
        "details": "2.097MB",
        "current": 2097152,
        "total": 3799689,
        "percent": 55,
    },
    {
        "id": "68fe9bff2ad4",
        "parent_id": BUSYBOX,
        "status": "Working",
        "text": "Extracting",
        "details": "1B",
        "current": 1,
    },
    {
        "id": "68fe9bff2ad4",
        "parent_id": BUSYBOX,
        "status": "Done",
        "text": "Pull complete",
        "details": "0B",
        "percent": 100,
    },
    {"id": BUSYBOX, "status": "Done", "text": "Pulled"},
    {
        "id": "9824c27679d3",
        "parent_id": ALPINE,
        "status": "Done",
        "text": "Pull complete",
        "details": "0B",
        "percent": 100,
    },
    {"id": ALPINE, "status": "Done", "text": "Pulled"},
]


def test_compose_events_become_logs_and_monotonic_progress() -> None:
    tracker = PullProgressTracker()
    logs: list[str] = []
    snapshots = []
    for event in SAMPLE:
        logs.extend(tracker.feed(event))
        snapshots.append(tracker.snapshot())

    assert logs == [
        "Pulling docker.io/library/alpine:3.22.1",
        "Pulling docker.io/library/busybox:1.37.0",
        "Pulled docker.io/library/busybox:1.37.0",
        "Pulled docker.io/library/alpine:3.22.1",
    ]
    percents = [percent for percent, _ in snapshots]
    assert percents == sorted(percents)
    assert 0 < percents[6] < 100
    assert snapshots[-1] == (100, "2 of 2 images · 6.0 MB downloaded")


def test_cached_images_complete_without_layer_events() -> None:
    tracker = PullProgressTracker()
    tracker.feed({"id": BUSYBOX, "status": "Working", "text": "Pulling"})
    tracker.feed({"id": BUSYBOX, "status": "Done", "text": "Pulled"})
    assert tracker.snapshot() == (100, "1 of 1 images")


def test_pull_errors_keep_upstream_details_server_side(caplog: pytest.LogCaptureFixture) -> None:
    tracker = PullProgressTracker()
    image = "Image docker.io/library/busybox:0.0.0"
    failure = {"id": image, "status": "Error", "text": "Error", "details": "secret upstream detail"}

    assert tracker.feed(failure) == ["Could not pull docker.io/library/busybox:0.0.0"]
    assert tracker.feed({"error": True, "message": "Error response from daemon: secret"}) == []
    assert "secret upstream detail" in caplog.text


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "Pulling",
        {"id": 1, "text": "Pulled"},
        {"id": "Image ../../etc passwd", "text": "Pulling"},
        {"id": "x" * 200, "parent_id": "Image a:b", "text": "Downloading", "current": True},
    ],
)
def test_hostile_events_are_ignored(event: object) -> None:
    tracker = PullProgressTracker()
    assert tracker.feed(event) == []
    assert tracker.snapshot() == (0, "Waiting for the registry")


@pytest.mark.parametrize(
    ("count", "text"),
    [(999, "999 B"), (1_048_576, "1.0 MB"), (245_000_000, "245.0 MB"), (1_500_000_000, "1.5 GB")],
)
def test_format_bytes(count: int, text: str) -> None:
    assert format_bytes(count) == text
