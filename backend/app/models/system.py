"""Readiness contracts: GET /api/system (first-run checklist) and
GET /api/templates/{template_id}/readiness (capacity check before deploying)."""

from typing import Literal

from pydantic import BaseModel

CheckName = Literal["docker", "proxy", "memory", "disk", "llm"]
CheckStatus = Literal["ok", "warning", "error", "info"]
ReadinessWarning = Literal["memory", "disk"]


class ResourcesView(BaseModel):
    memory_total_mb: int | None
    memory_available_mb: int | None
    disk_free_mb: int | None
    disk_is_virtual: bool  # Docker Desktop's disk image: the host drive may have less room


class SystemCheck(BaseModel):
    name: CheckName
    status: CheckStatus
    detail: str


class SystemView(BaseModel):
    checks: list[SystemCheck]
    resources: ResourcesView


class TemplateReadiness(BaseModel):
    template_id: str
    images_total: int
    images_missing: int | None  # None: Docker cannot tell
    download_mb: int | None  # estimated download left: 0 when every image is already local
    memory_mb: int
    first_start_seconds: int
    resources: ResourcesView
    warnings: list[ReadinessWarning]
