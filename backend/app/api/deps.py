"""Dependency providers. Shared services live on `app.state`, created in the lifespan.

`HTTPConnection` is the common base of `Request` and `WebSocket`, so the same
providers serve HTTP routes and WebSocket endpoints.
"""

from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from app.core.config import Settings
from app.services.event_bus import JobEventBus
from app.services.inventory import EnvironmentInventory
from app.services.llm_settings import LLMSettings
from app.services.log_streams import LogStreamer
from app.services.orchestrator import Orchestrator
from app.services.readiness_probe import ReadinessProbe
from app.services.template_catalog import TemplateCatalog


def get_settings_from_app(conn: HTTPConnection) -> Settings:
    settings: Settings = conn.app.state.settings
    return settings


def get_event_bus(conn: HTTPConnection) -> JobEventBus:
    bus: JobEventBus = conn.app.state.event_bus
    return bus


def get_catalog(conn: HTTPConnection) -> TemplateCatalog:
    catalog: TemplateCatalog = conn.app.state.catalog
    return catalog


def get_orchestrator(conn: HTTPConnection) -> Orchestrator:
    orchestrator: Orchestrator = conn.app.state.orchestrator
    return orchestrator


def get_inventory(conn: HTTPConnection) -> EnvironmentInventory:
    inventory: EnvironmentInventory = conn.app.state.inventory
    return inventory


def get_log_streamer(conn: HTTPConnection) -> LogStreamer:
    streamer: LogStreamer = conn.app.state.log_streamer
    return streamer


def get_readiness(conn: HTTPConnection) -> ReadinessProbe:
    readiness: ReadinessProbe = conn.app.state.readiness
    return readiness


def get_llm_settings(conn: HTTPConnection) -> LLMSettings:
    llm: LLMSettings = conn.app.state.llm_settings
    return llm


SettingsDep = Annotated[Settings, Depends(get_settings_from_app)]
EventBusDep = Annotated[JobEventBus, Depends(get_event_bus)]
CatalogDep = Annotated[TemplateCatalog, Depends(get_catalog)]
OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
InventoryDep = Annotated[EnvironmentInventory, Depends(get_inventory)]
LogStreamerDep = Annotated[LogStreamer, Depends(get_log_streamer)]
LLMSettingsDep = Annotated[LLMSettings, Depends(get_llm_settings)]
ReadinessDep = Annotated[ReadinessProbe, Depends(get_readiness)]
