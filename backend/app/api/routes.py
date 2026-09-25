"""REST endpoints: health, template catalog, deployment jobs and the environment inventory."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status

from app import __version__
from app.api.deps import CatalogDep, InventoryDep, OrchestratorDep, SettingsDep
from app.core.security import require_trusted_json_request, require_trusted_origin
from app.models.capabilities import CapabilitiesResponse
from app.models.common import PROJECT_NAME_PATTERN, TEMPLATE_ID_PATTERN
from app.models.deployment import (
    DeploymentRequest,
    JobListResponse,
    JobSummary,
    LifecycleRequest,
    PlanRequest,
)
from app.models.environment import EnvironmentListResponse, EnvironmentView
from app.models.plan import PlanView
from app.models.template import CATEGORY_LABELS, CategoryInfo, TemplateCatalogResponse
from app.services.orchestrator import (
    Job,
    ProjectNameConflictError,
    TranslatorUnavailableError,
    UnknownEnvironmentError,
    UnknownPlanError,
    UnknownTemplateError,
    job_events_url,
)

router = APIRouter(prefix="/api")

_LLM_UNAVAILABLE = "Natural-language requests need an LLM API key (ENVCRAFTER_LLM_API_KEY)."


def _summary(job: Job) -> JobSummary:
    return JobSummary(
        job_id=job.id,
        status=job.status,
        mode=job.mode,
        project_name=job.project_name,
        created_at=job.created_at,
        url=job.url,
        urls=job.urls,
        # Same rule as the job.succeeded event: only a "planning" job announces a plan.
        # Job.plan_id is also set on a "plan" deployment job (the plan it consumed),
        # but that is internal pipeline state (_load_plan), never client-facing.
        plan_id=job.plan_id if job.mode == "planning" else None,
        events_url=job_events_url(job.id),
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def get_config(settings: SettingsDep, orchestrator: OrchestratorDep) -> CapabilitiesResponse:
    """What the UI needs to adapt itself (simulated banner, prompt availability, rules)."""
    return CapabilitiesResponse(
        version=__version__,
        engine=settings.engine,
        llm_available=orchestrator.has_translator,
        public_domain=settings.public_domain,
        project_name_pattern=PROJECT_NAME_PATTERN,
    )


@router.get("/templates")
async def list_templates(catalog: CatalogDep, settings: SettingsDep) -> TemplateCatalogResponse:
    return TemplateCatalogResponse(
        categories=[CategoryInfo(id=cat, label=label) for cat, label in CATEGORY_LABELS.items()],
        templates=[template.view(settings.public_domain) for template in catalog.all()],
    )


@router.get("/templates/{template_id}/logo")
async def template_logo(
    template_id: Annotated[str, Path(pattern=TEMPLATE_ID_PATTERN)],
    catalog: CatalogDep,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """Serve a template logo. The id is resolved through the catalog allow-list and the
    bytes were validated at startup: no path is ever built from the request."""
    template = catalog.get(template_id)
    if template is None or template.logo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No logo for this template")
    logo = template.logo
    headers = {
        "ETag": logo.etag,
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if if_none_match == logo.etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=logo.data, media_type=logo.media_type, headers=headers)


@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_trusted_json_request)],
)
async def create_job(payload: DeploymentRequest, orchestrator: OrchestratorDep) -> JobSummary:
    """Validate and schedule a deployment; progress is streamed on `events_url`.

    By the time this body runs, pydantic has already rejected anything that
    does not match the tagged union exactly (unknown mode, unknown field,
    malformed slug, oversized prompt...) with a 422.
    """
    try:
        job = await orchestrator.submit(payload.root)
    except UnknownTemplateError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown template") from None
    except UnknownPlanError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired plan") from None
    except ProjectNameConflictError:
        raise HTTPException(status.HTTP_409_CONFLICT, "This project name is already used") from None
    except TranslatorUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _LLM_UNAVAILABLE) from None
    return _summary(job)


@router.get("/jobs")
async def list_jobs(orchestrator: OrchestratorDep, active: bool = False) -> JobListResponse:
    """Retained jobs, newest first; `?active=true` keeps the queued and running ones."""
    return JobListResponse(
        jobs=[_summary(job) for job in orchestrator.list_jobs(active_only=active)]
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, orchestrator: OrchestratorDep) -> JobSummary:
    job = orchestrator.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown job")
    return _summary(job)


@router.post(
    "/plans",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_trusted_json_request)],
)
async def create_plan(payload: PlanRequest, orchestrator: OrchestratorDep) -> JobSummary:
    """Analyse a request into a reviewable plan; progress is streamed on `events_url` and
    the succeeded event carries the `plan_id`. Nothing is deployed."""
    try:
        job = await orchestrator.submit_planning(payload)
    except TranslatorUnavailableError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _LLM_UNAVAILABLE) from None
    return _summary(job)


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: UUID, orchestrator: OrchestratorDep) -> PlanView:
    stored = orchestrator.get_plan(plan_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired plan")
    return stored.view


@router.get("/environments")
async def list_environments(inventory: InventoryDep) -> EnvironmentListResponse:
    return EnvironmentListResponse(environments=await inventory.list())


@router.get("/environments/{project}")
async def get_environment(
    project: Annotated[str, Path(pattern=PROJECT_NAME_PATTERN)], inventory: InventoryDep
) -> EnvironmentView:
    view = await inventory.get(project)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown environment")
    return view


@router.post(
    "/environments/{project}/actions",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_trusted_json_request)],
)
async def run_environment_action(
    project: Annotated[str, Path(pattern=PROJECT_NAME_PATTERN)],
    payload: LifecycleRequest,
    orchestrator: OrchestratorDep,
) -> JobSummary:
    """Stop, start or restart an environment; progress is streamed on `events_url`."""
    try:
        job = await orchestrator.submit_lifecycle(project, payload.action)
    except UnknownEnvironmentError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown environment") from None
    except ProjectNameConflictError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A job is already running for this environment"
        ) from None
    return _summary(job)


@router.delete(
    "/environments/{project}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_trusted_origin)],
)
async def remove_environment(
    # Same slug rule as at creation: the value is used in paths and Docker names.
    project: Annotated[str, Path(pattern=PROJECT_NAME_PATTERN)],
    orchestrator: OrchestratorDep,
) -> JobSummary:
    """Remove containers, networks, volumes and the workspace of an environment."""
    try:
        job = await orchestrator.submit_removal(project)
    except UnknownEnvironmentError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown environment") from None
    except ProjectNameConflictError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A job is already running for this environment"
        ) from None
    return _summary(job)
