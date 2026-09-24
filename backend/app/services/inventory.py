"""Environment inventory: workspaces (what was deployed) merged with Docker (what runs).

`meta.json` records intent; the engine reports reality through container labels.
One engine call serves every project, and concurrent callers share it for
`cache_seconds` (single-flight), so several polling tabs never multiply `docker ps`.
"""

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.engine.base import EngineError, ServiceStatus, StackHandle
from app.models.common import DEPLOY_MODES
from app.models.environment import (
    ActiveJobRef,
    EnvironmentMeta,
    EnvironmentService,
    EnvironmentState,
    EnvironmentView,
    ServiceView,
)
from app.services.orchestrator import Job, job_events_url
from app.workspace.manager import WorkspaceManager
from app.workspace.renderer import compose_project_name

logger = logging.getLogger(__name__)


class StatusSource(Protocol):
    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]: ...


class JobDirectory(Protocol):
    def list_jobs(self, *, active_only: bool = False) -> list[Job]: ...

    def active_job(self, project: str) -> Job | None: ...


def summarize_state(expected: Iterable[str], observed: Sequence[ServiceStatus]) -> EnvironmentState:
    if not observed:
        return EnvironmentState.MISSING
    running = {status.service: status for status in observed if status.state == "running"}
    if not running:
        return EnvironmentState.STOPPED
    wanted = set(expected) or {status.service for status in observed}
    if not wanted <= running.keys() or any(s.health == "unhealthy" for s in running.values()):
        return EnvironmentState.DEGRADED
    if any(status.health == "starting" for status in running.values()):
        return EnvironmentState.STARTING
    return EnvironmentState.RUNNING


@dataclass(frozen=True)
class _Record:
    project: str
    meta: EnvironmentMeta | None
    stack: StackHandle


class EnvironmentInventory:
    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        engine: StatusSource,
        jobs: JobDirectory,
        cache_seconds: float,
    ) -> None:
        self._workspaces = workspaces
        self._engine = engine
        self._jobs = jobs
        self._cache_seconds = cache_seconds
        self._lock = asyncio.Lock()
        self._cache: tuple[float, frozenset[str], dict[str, list[ServiceStatus]] | None] | None = (
            None
        )

    def _load_records(self) -> list[_Record]:
        records: list[_Record] = []
        for project in self._workspaces.list_projects():
            workspace = self._workspaces.get(project)
            if workspace is None:
                continue
            stack = StackHandle(
                project=project,
                compose_project=compose_project_name(project),
                compose_file=workspace.compose_file,
                edge_network=None,
            )
            records.append(_Record(project, self._workspaces.read_meta(project), stack))
        return records

    async def _observe(self, stacks: list[StackHandle]) -> dict[str, list[ServiceStatus]] | None:
        """Engine state for these stacks, or None when the engine cannot be queried."""
        key = frozenset(stack.project for stack in stacks)
        async with self._lock:
            if self._cache is not None:
                at, cached_key, cached = self._cache
                if cached_key == key and time.monotonic() - at < self._cache_seconds:
                    return cached
            observed: dict[str, list[ServiceStatus]] | None
            try:
                observed = await self._engine.status(stacks) if stacks else {}
            except (EngineError, OSError):
                logger.warning("Environment states are unavailable", exc_info=True)
                observed = None
            self._cache = (time.monotonic(), key, observed)
            return observed

    def _view(
        self, record: _Record, observed: dict[str, list[ServiceStatus]] | None
    ) -> EnvironmentView:
        meta = record.meta
        statuses = None if observed is None else observed.get(record.project, [])
        by_service = {status.service: status for status in statuses or []}
        if meta is not None:
            services = []
            for service in meta.services:
                status = by_service.get(service.service)
                services.append(self._service_view(service, status))
        else:
            services = [
                ServiceView(
                    service=status.service,
                    name=status.service,
                    image=None,
                    state=status.state,
                    health=status.health,
                )
                for status in statuses or []
            ]
        state = (
            EnvironmentState.UNKNOWN
            if statuses is None
            else summarize_state([service.service for service in services], statuses)
        )
        job = self._jobs.active_job(record.project)
        return EnvironmentView(
            project=record.project,
            title=meta.title if meta is not None else record.project,
            origin=meta.origin if meta is not None else None,
            template_id=meta.template_id if meta is not None else None,
            created_at=meta.created_at if meta is not None else None,
            state=state,
            services=services,
            urls=list(meta.urls) if meta is not None else [],
            volumes=list(meta.volumes) if meta is not None else [],
            job=_job_ref(job) if job is not None else None,
        )

    @staticmethod
    def _service_view(service: EnvironmentService, status: ServiceStatus | None) -> ServiceView:
        return ServiceView(
            service=service.service,
            name=service.name,
            image=service.image,
            state=status.state if status is not None else None,
            health=status.health if status is not None else None,
        )

    def _pending(self, known: set[str]) -> list[EnvironmentView]:
        """Deployments whose workspace is not written yet (AI analysis, validation)."""
        views: list[EnvironmentView] = []
        for job in self._jobs.list_jobs(active_only=True):
            project = job.project_name
            if job.mode not in DEPLOY_MODES or project is None or project in known:
                continue
            views.append(
                EnvironmentView(
                    project=project,
                    title=job.template.manifest.name if job.template else "AI request",
                    origin="template" if job.mode == "template" else "prompt",
                    template_id=job.template.manifest.id if job.template else None,
                    created_at=job.created_at,
                    state=EnvironmentState.PENDING,
                    services=[],
                    urls=[],
                    volumes=[],
                    job=_job_ref(job),
                )
            )
        return views

    # These two are defined last: naming one of them "list" shadows the builtin
    # for annotations written below it in this class body (Python resolves a bare
    # `list[...]` through the class namespace first), so nothing here may follow it.
    async def list(self) -> list[EnvironmentView]:
        records = await asyncio.to_thread(self._load_records)
        observed = await self._observe([record.stack for record in records])
        views = [self._view(record, observed) for record in records]
        views.extend(self._pending({view.project for view in views}))
        return sorted(views, key=_newest_first)

    async def get(self, project: str) -> EnvironmentView | None:
        return next((view for view in await self.list() if view.project == project), None)


def _job_ref(job: Job) -> ActiveJobRef:
    return ActiveJobRef(job_id=job.id, mode=job.mode, events_url=job_events_url(job.id))


def _newest_first(view: EnvironmentView) -> tuple[bool, float]:
    created = view.created_at
    return created is None, -(created.timestamp() if created is not None else 0.0)
