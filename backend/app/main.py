"""EnvCrafter API entrypoint: app factory and lifespan.

Run from backend/:  uv run uvicorn app.main:app --reload
(single worker: the event bus is in-process, see services/event_bus.py)
"""

import asyncio
import logging
import mimetypes
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.api.websocket import router as ws_router
from app.core.config import Settings, get_settings
from app.engine.base import Engine
from app.engine.docker_compose import DockerComposeEngine
from app.engine.simulated import SimulatedEngine
from app.policy.images import ImageAllowlist
from app.services.event_bus import JobEventBus
from app.services.orchestrator import Orchestrator
from app.services.template_catalog import TemplateCatalog
from app.translator.client import LLMTranslator, Translator
from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


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
    )


TranslatorFactory = Callable[[TemplateCatalog, ImageAllowlist], Translator]


def create_app(
    settings: Settings | None = None, translator_factory: TranslatorFactory | None = None
) -> FastAPI:
    """`translator_factory` replaces the Claude-backed translator (tests, offline use)."""
    settings = settings or get_settings()
    is_dev = settings.environment == "development"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Blocking disk I/O goes to worker threads: the event loop never blocks.
        # A template or allow-list error stops the startup here, on purpose.
        allowlist = await asyncio.to_thread(ImageAllowlist.load, settings.image_allowlist)
        catalog = await asyncio.to_thread(TemplateCatalog.load, settings.templates_dir, allowlist)
        translator: Translator | None = None
        if translator_factory is not None:
            translator = translator_factory(catalog, allowlist)
        elif settings.llm_api_key is not None:
            translator = LLMTranslator(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                catalog=catalog,
                allowlist=allowlist,
            )
        else:
            logger.warning("No ENVCRAFTER_LLM_API_KEY: natural-language requests are disabled")
        bus = JobEventBus(
            history_size=settings.event_history_size,
            queue_size=settings.subscriber_queue_size,
        )
        orchestrator = Orchestrator(
            bus=bus,
            catalog=catalog,
            allowlist=allowlist,
            workspaces=WorkspaceManager(settings.workspaces_dir),
            engine=build_engine(settings),
            translator=translator,
            settings=settings,
        )

        app.state.settings = settings
        app.state.catalog = catalog
        app.state.event_bus = bus
        app.state.orchestrator = orchestrator
        try:
            yield
        finally:
            await orchestrator.shutdown()

    app = FastAPI(
        title="EnvCrafter",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/api/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if is_dev else None,
    )
    # Rejects requests whose Host header is not ours: defeats DNS rebinding,
    # where a malicious domain re-resolves to 127.0.0.1 to reach this API.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    app.include_router(api_router)
    app.include_router(ws_router)

    if settings.serve_frontend and settings.frontend_dir.is_dir():
        # Some Windows registries map .js to text/plain, which browsers refuse for ES modules.
        mimetypes.add_type("text/javascript", ".js")
        # Mounted last so that /api and /ws routes take precedence.
        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
