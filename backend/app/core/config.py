"""Application settings, loaded from environment variables and the git-ignored `.env` file.

Secrets are never hard-coded: they come from the environment and are typed as
`SecretStr`, so they are masked in logs, reprs and tracebacks.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> the repository root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = Path(__file__).resolve().parents[1]

LLMEffort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENVCRAFTER_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    environment: Literal["development", "production"] = "development"

    # Browsers do not apply CORS to WebSockets, so the Origin header is checked
    # explicitly (see core/security.py). Host allow-list defeats DNS rebinding.
    allowed_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "*.localhost"]

    templates_dir: Path = REPO_ROOT / "templates"
    image_allowlist: Path = APP_DIR / "policy" / "image_allowlist.yaml"
    workspaces_dir: Path = REPO_ROOT / "workspaces"

    # Dev convenience: serve the static frontend from the API process (same origin).
    serve_frontend: bool = True
    frontend_dir: Path = REPO_ROOT / "frontend"

    # "simulated" runs the whole pipeline except the Docker calls (local dev,
    # tests). "docker" is used by deploy/docker-compose.yml.
    engine: Literal["simulated", "docker"] = "simulated"
    # The API reaches Docker through a filtering socket proxy, never the raw socket.
    docker_host: str = "tcp://socket-proxy-api:2375"
    docker_binary: str = "docker"
    traefik_container: str = "envcrafter-traefik"
    # Environments are served at http://<project>.<public_domain>.
    public_domain: str = "localhost"
    pull_timeout_seconds: float = 1800.0
    start_timeout_seconds: int = 600
    stop_timeout_seconds: int = 20

    # Only used for natural-language requests. Without a key, prompt mode is off.
    llm_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    # Opus 5.5 thinks at "medium" effort unless told otherwise. A deployment plan
    # is intelligence-sensitive, so EnvCrafter asks for "high" explicitly.
    llm_effort: LLMEffort = "high"
    llm_timeout_seconds: float = 180.0

    # Real-time channel sizing (see services/event_bus.py).
    event_history_size: int = 2000
    subscriber_queue_size: int = 500
    job_retention_seconds: float = 3600.0
    # Reviewed AI plans wait this long for their deployment.
    plan_ttl_seconds: float = 900.0
    max_plans: int = 32
    # One `docker ps` serves every dashboard poll during this window.
    inventory_cache_seconds: float = 2.0
    # Health watcher period while `up --wait` runs, and step.progress throttle.
    health_poll_seconds: float = 5.0
    progress_interval_seconds: float = 0.5

    # Delay between simulated engine actions.
    simulated_step_delay: float = 0.4

    # Container logs: largest accepted `tail`, and concurrent `logs --follow` processes.
    log_tail_max: int = 1000
    max_log_streams: int = 4


@lru_cache
def get_settings() -> Settings:
    return Settings()
