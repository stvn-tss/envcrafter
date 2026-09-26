from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("stop_timeout_seconds", 0),
        ("plan_ttl_seconds", 0),
        ("plan_ttl_seconds", -1),
        ("max_plans", 0),
        ("inventory_cache_seconds", -0.5),
        ("health_poll_seconds", 0),
        ("health_poll_seconds", -5),
        ("progress_interval_seconds", 0),
        ("log_tail_max", -1),
        ("max_log_streams", 0),
    ],
)
def test_out_of_range_settings_are_rejected(name: str, value: float) -> None:
    overrides: dict[str, Any] = {name: value}
    with pytest.raises(ValidationError) as exc_info:
        Settings(**overrides)
    assert exc_info.value.errors()[0]["loc"] == (name,)


def test_out_of_range_environment_variables_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A zero poll period would spawn `docker ps` in a tight loop for the whole startup.
    monkeypatch.setenv("ENVCRAFTER_HEALTH_POLL_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_boundary_settings_are_accepted() -> None:
    settings = Settings(
        inventory_cache_seconds=0,  # no cache: used by the browser tests
        log_tail_max=0,
        max_plans=1,
        max_log_streams=1,
        health_poll_seconds=0.05,
        progress_interval_seconds=0.01,
        plan_ttl_seconds=1,
        stop_timeout_seconds=1,
    )
    assert (settings.inventory_cache_seconds, settings.log_tail_max) == (0, 0)
