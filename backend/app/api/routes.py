"""REST endpoints: health, template catalog, deployment jobs and environment removal."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.api.deps import CatalogDep, OrchestratorDep, SettingsDep
from app.core.security import require_trusted_json_request, require_trusted_origin
from app.models.common import PROJECT_NAME_PATTERN
from app.models.deployment import DeploymentRequest, JobSummary
from app.models.template import CATEGORY_LABELS, CategoryInfo, TemplateCatalogResponse
from app.services.orchestrator import (
    Job,
    ProjectNameConflictError,
    TranslatorUnavailableError,
    UnknownEnvironmentError,
    UnknownTemplateError,
)

router = APIRouter(prefix="/api")


def _summary(job: Job) -> JobSummary:
    return JobSummary(
        job_id=job.id,
        status=job.status,
        mode=job.mode,
        project_name=job.project_name,
        created_at=job.created_at,
        url=job.url,
        events_url=f"/ws/jobs/{job.id}",
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/templates")
async def list_templates(catalog: CatalogDep, settings: SettingsDep) -> TemplateCatalogResponse:
    return TemplateCatalogResponse(
        categories=[CategoryInfo(id=cat, label=label) for cat, label in CATEGORY_LABELS.items()],
        templates=[template.view(settings.public_domain) for template in catalog.all()],
    )


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
    except ProjectNameConflictError:
        raise HTTPException(status.HTTP_409_CONFLICT, "This project name is already used") from None
    except TranslatorUnavailableError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Natural-language requests need an LLM API key (ENVCRAFTER_LLM_API_KEY).",
        ) from None
    return _summary(job)


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, orchestrator: OrchestratorDep) -> JobSummary:
    job = orchestrator.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown job")
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
