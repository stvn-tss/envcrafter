"""Is this machine ready to deploy? Rules over what the engine measured.

The thresholds are rough on purpose: they warn, they never block a deployment.
"""

import math
from collections.abc import Sequence

from app.engine.base import HostResources, RuntimeCheck
from app.models.system import (
    CheckStatus,
    ReadinessWarning,
    ResourcesView,
    SystemCheck,
    TemplateReadiness,
)
from app.services.template_catalog import Template

# Compressed layers take about this many times more room once extracted.
DISK_EXPANSION = 3
DISK_MARGIN_MB = 1024  # volumes, logs, temporary files
LOW_MEMORY_MB = 1024
LOW_DISK_MB = 5 * 1024


def resources_view(resources: HostResources) -> ResourcesView:
    return ResourcesView(
        memory_total_mb=resources.memory_total_mb,
        memory_available_mb=resources.memory_available_mb,
        disk_free_mb=resources.disk_free_mb,
        disk_is_virtual=resources.disk_is_virtual,
    )


def template_images(template: Template) -> list[str]:
    return list(dict.fromkeys(service.image for service in template.compose.services.values()))


def template_readiness(
    template: Template, missing: int, resources: HostResources
) -> TemplateReadiness:
    """`missing`: how many of the template's images are not downloaded yet. The download
    left is estimated in proportion to them: the manifest only knows the total."""
    footprint = template.manifest.footprint
    total = len(template_images(template))
    download = math.ceil(footprint.download_mb * missing / total) if total else 0
    warnings: list[ReadinessWarning] = []
    available = resources.memory_available_mb
    if available is not None and available < footprint.memory_mb:
        warnings.append("memory")
    disk = resources.disk_free_mb
    if disk is not None and disk < download * DISK_EXPANSION + DISK_MARGIN_MB:
        warnings.append("disk")
    return TemplateReadiness(
        template_id=template.manifest.id,
        images_total=total,
        images_missing=missing,
        download_mb=download,
        memory_mb=footprint.memory_mb,
        first_start_seconds=footprint.first_start_seconds,
        resources=resources_view(resources),
        warnings=warnings,
    )


def size(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def system_checks(
    runtime: Sequence[RuntimeCheck], resources: HostResources, llm_configured: bool
) -> list[SystemCheck]:
    """The first-run checklist: Docker, the reverse proxy, memory, disk and the AI key."""
    checks = [
        SystemCheck(name=check.name, status="ok" if check.ok else "error", detail=check.detail)
        for check in runtime
    ]
    memory = resources.memory_available_mb
    if memory is None:
        checks.append(
            SystemCheck(name="memory", status="info", detail="Free memory could not be measured.")
        )
    else:
        low = memory < LOW_MEMORY_MB
        detail = f"{size(memory)} available for environments" + (
            ": stop an environment before deploying another one." if low else "."
        )
        checks.append(SystemCheck(name="memory", status="warning" if low else "ok", detail=detail))
    disk = resources.disk_free_mb
    if disk is None:
        checks.append(
            SystemCheck(name="disk", status="info", detail="Free disk space could not be measured.")
        )
    else:
        low = disk < LOW_DISK_MB
        shortage = ": some templates download more than 1 GB."
        if resources.disk_is_virtual:
            # Docker Desktop's disk image reports its own maximum, not what the host drive
            # has left: plenty of room there proves nothing, only a shortage does.
            detail = f"{size(disk)} free in Docker Desktop's disk image" + (
                shortage if low else ": check the free space of the drive that holds it."
            )
            status: CheckStatus = "warning" if low else "info"
        else:
            detail = f"{size(disk)} free on Docker's disk" + (shortage if low else ".")
            status = "warning" if low else "ok"
        checks.append(SystemCheck(name="disk", status=status, detail=detail))
    checks.append(
        SystemCheck(name="llm", status="ok", detail="AI plans are on.")
        if llm_configured
        else SystemCheck(
            name="llm",
            status="info",
            detail="AI plans are off: templates work without a key. Add a Claude API key "
            "in Settings to describe environments in your own words.",
        )
    )
    return checks
