"""EnvCrafter API entrypoint: app factory and lifespan.

Run from backend/:  uv run uvicorn app.main:app --reload
(single worker: the event bus is in-process, see services/event_bus.py)
"""

import asyncio
import logging
import mimetypes
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr
from starlette.responses import Response
from starlette.staticfiles import PathLike
from starlette.types import Scope

from app import __version__
from app.api.routes import router as api_router
from app.api.websocket import router as ws_router
from app.core.config import Settings, get_settings
from app.core.security import SecurityHeadersMiddleware
from app.engine.base import Engine
from app.engine.docker_compose import DockerComposeEngine
from app.engine.simulated import SimulatedEngine
from app.policy.images import ImageAllowlist
from app.services.event_bus import JobEventBus
from app.services.history import HistoryStore
from app.services.inventory import EnvironmentInventory
from app.services.llm_settings import LLMSettings, SettingsStore, TranslatorFactory
from app.services.log_streams import LogStreamer
from app.services.orchestrator import Orchestrator
from app.services.plan_store import PlanStore, restore_plan
from app.services.readiness_probe import ReadinessProbe
from app.services.template_catalog import TemplateCatalog
from app.translator.client import LLMTranslator, Translator
from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class NoCacheStaticFiles(StaticFiles):
    """Static files that browsers revalidate (ETag / Last-Modified) on every load.

    Without it a browser can keep old ES modules next to new ones after an
    update, and a mix of versions breaks the UI.
    """

    def file_response(
        self,
        full_path: PathLike,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


def build_engine(settings: Settings) -> Engine:
    if settings.engine == "simulated":
        return SimulatedEngine(delay=settings.simulated_step_delay)
    docker = shutil.which(settings.docker_binary)
    if docker is None:
        raise RuntimeError(f"docker CLI not found ({settings.docker_binary})")
    return DockerComposeEngine(
        docker_binary=docker,
        docker_host=settings.docker_host,
        traefik_container=settings.traefik_container,
        pull_timeout=settings.pull_timeout_seconds,
        start_timeout=settings.start_timeout_seconds,
        stop_timeout=settings.stop_timeout_seconds,
        disk_path=settings.workspaces_dir,
    )


def create_app(
    settings: Settings | None = None, translator_factory: TranslatorFactory | None = None
) -> FastAPI:
    """`translator_factory` replaces the Claude-backed translator (tests, offline use): it
    receives the active Claude API key and is called again whenever the key changes."""
    settings = settings or get_settings()
    is_dev = settings.environment == "development"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Blocking disk I/O goes to worker threads: the event loop never blocks.
        # A template or allow-list error stops the startup here, on purpose.
        allowlist = await asyncio.to_thread(ImageAllowlist.load, settings.image_allowlist)
        catalog = await asyncio.to_thread(TemplateCatalog.load, settings.templates_dir, allowlist)

        def claude_translator(api_key: SecretStr) -> Translator:
            return LLMTranslator(
                api_key=api_key,
                model=settings.llm_model,
                effort=settings.llm_effort,
                timeout=settings.llm_timeout_seconds,
                catalog=catalog,
                allowlist=allowlist,
            )

        bus = JobEventBus(
            history_size=settings.event_history_size,
            queue_size=settings.subscriber_queue_size,
        )
        workspaces = WorkspaceManager(settings.workspaces_dir)
        engine = build_engine(settings)
        plans = PlanStore(ttl_seconds=settings.plan_ttl_seconds, max_plans=settings.max_plans)
        history = HistoryStore(
            settings.history_file, retention_days=settings.history_retention_days
        )
        await history.open()
        # Plans still waiting for their review survive a restart of the server.
        for body in await history.load_plans():
            stored = restore_plan(body, catalog, allowlist)
            if stored is not None:
                plans.add(stored)
        orchestrator = Orchestrator(
            bus=bus,
            catalog=catalog,
            allowlist=allowlist,
            workspaces=workspaces,
            engine=engine,
            translator=None,  # installed by LLMSettings.load() from the active key
            settings=settings,
            plans=plans,
            history=history,
        )
        llm_settings = LLMSettings(
            store=SettingsStore(settings.settings_file),
            environment_key=settings.llm_api_key,
            model=settings.llm_model,
            factory=translator_factory or claude_translator,
            target=orchestrator,
            history=history,
        )
        await llm_settings.load()

        app.state.settings = settings
        app.state.history = history
        app.state.readiness = ReadinessProbe(
            engine=engine, cache_seconds=settings.readiness_cache_seconds
        )
        app.state.catalog = catalog
        app.state.event_bus = bus
        app.state.orchestrator = orchestrator
        app.state.llm_settings = llm_settings
        app.state.inventory = EnvironmentInventory(
            workspaces=workspaces,
            engine=engine,
            jobs=orchestrator,
            cache_seconds=settings.inventory_cache_seconds,
        )
        app.state.log_streamer = LogStreamer(
            workspaces=workspaces, engine=engine, max_streams=settings.max_log_streams
        )
        try:
            yield
        finally:
            await orchestrator.shutdown()
            await history.close()

    app = FastAPI(
        title="EnvCrafter",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if is_dev else None,
    )
    # Rejects requests whose Host header is not ours: defeats DNS rebinding,
    # where a malicious domain re-resolves to 127.0.0.1 to reach this API.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    # Added last = outermost: even a rejected Host gets the headers.
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(api_router)
    app.include_router(ws_router)

    if settings.serve_frontend and settings.frontend_dir.is_dir():
        # Some Windows registries map .js to text/plain, which browsers refuse for ES modules.
        mimetypes.add_type("text/javascript", ".js")
        # Mounted last so that /api and /ws routes take precedence.
        app.mount(
            "/", NoCacheStaticFiles(directory=settings.frontend_dir, html=True), name="frontend"
        )
    return app


app = create_app()
