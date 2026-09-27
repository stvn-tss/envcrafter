"""Public API contracts: deployment requests and real-time job events.

Every field coming from the client is validated here, at the edge, before any
business logic runs. Nothing downstream re-parses raw client input.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    RootModel,
    StrictBool,
    StringConstraints,
    model_validator,
)

from app.core.timezones import TIMEZONE_PATTERN
from app.models.common import (
    JobMode,
    ProjectName,
    StrictModel,
    TemplateId,
    reject_invisible_characters,
)
from app.models.environment import WebEndpoint
from app.models.template import ParameterValue, SecretName, ServiceName

# --- Requests -----------------------------------------------------------------

# Untrusted free text that will reach the LLM. Validating it does NOT make the LLM
# output trustworthy: that output goes through its own schema + policy validation.
Prompt = Annotated[
    str,
    StringConstraints(min_length=3, max_length=2000),
    AfterValidator(reject_invisible_characters),
]

# The browser's IANA time zone ("Europe/Paris"), written to the workspace as ${EC_TZ}. Only
# the syntax is checked here: a well-formed name the server's tz database does not know
# falls back to the server default instead of failing the deployment.
TimeZoneName = Annotated[str, StringConstraints(max_length=64, pattern=TIMEZONE_PATTERN)]


class TemplateDeploymentRequest(StrictModel):
    mode: Literal["template"]
    # Only a syntactic check here. The id is then looked up in the catalog
    # allow-list and is never used to build a filesystem path directly.
    template_id: TemplateId
    project_name: ProjectName | None = None
    timezone: TimeZoneName | None = None
    # Keep what was created if the deployment fails, for debugging; a cancellation
    # still removes it.
    keep_on_failure: bool = False
    # Values of the template's parameters (see its manifest), checked against the catalog
    # when the job is submitted; omitted ones take their default.
    parameters: dict[SecretName, StrictBool | ParameterValue] = Field(
        default_factory=dict, max_length=8
    )


class PromptDeploymentRequest(StrictModel):
    mode: Literal["prompt"]
    # The prompt is untrusted free text that will reach the LLM. Validating it
    # does NOT make the LLM output trustworthy: that output goes through its
    # own schema + policy validation before anything is written or executed.
    prompt: Prompt
    project_name: ProjectName | None = None
    timezone: TimeZoneName | None = None
    # Keep what was created if the deployment fails, for debugging; a cancellation
    # still removes it.
    keep_on_failure: bool = False


class PlanRequest(StrictModel):
    """POST /api/plans: analyse a request into a reviewable plan; nothing is deployed."""

    prompt: Prompt


class PlanDeploymentRequest(StrictModel):
    """Deploy a plan the user reviewed. Only its id travels: the stack stays server-side."""

    mode: Literal["plan"]
    plan_id: UUID
    project_name: ProjectName | None = None
    timezone: TimeZoneName | None = None
    # Keep what was created if the deployment fails, for debugging; a cancellation
    # still removes it.
    keep_on_failure: bool = False


class DeploymentRequest(
    RootModel[
        Annotated[
            TemplateDeploymentRequest | PromptDeploymentRequest | PlanDeploymentRequest,
            Field(discriminator="mode"),
        ]
    ]
):
    """Tagged union on `mode`: pydantic picks the sub-model from the tag alone,
    so an unknown mode (e.g. "shell") is rejected instead of being coerced into
    whichever model happens to match."""


LifecycleAction = Literal["stop", "start", "restart"]


class LifecycleRequest(StrictModel):
    """Body of POST /api/environments/{project}/actions. With `service`, a restart only
    stops and starts that service; the others keep running."""

    action: LifecycleAction
    service: ServiceName | None = None

    @model_validator(mode="after")
    def _service_only_restarts(self) -> "LifecycleRequest":
        if self.service is not None and self.action != "restart":
            raise ValueError("only a restart can target one service")
        return self


# --- Jobs & events ------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    JOB_ACCEPTED = "job.accepted"  # carries the full step plan
    STEP_STARTED = "step.started"
    STEP_LOG = "step.log"
    STEP_PROGRESS = "step.progress"  # measurable progress of the running step (throttled)
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"  # stopped by the user; a deployment was rolled back


TERMINAL_EVENTS = frozenset(
    {EventType.JOB_SUCCEEDED, EventType.JOB_FAILED, EventType.JOB_CANCELLED}
)


class PlanStep(BaseModel):
    key: str
    label: str


class JobEvent(BaseModel):
    """One message on the real-time channel (serialized as a JSON text frame).

    `seq` is a per-job, strictly increasing counter assigned by the event bus.
    Clients remember the last `seq` they rendered and send it back when they
    reconnect (`?after_seq=N`): the server replays only what they missed, so
    the UI never shows gaps or duplicates.
    """

    job_id: UUID
    seq: int
    type: EventType
    timestamp: datetime
    message: str
    step_key: str | None = None
    step_index: int | None = None  # 1-based, rendered as "2/5" in the UI
    step_total: int | None = None
    plan: list[PlanStep] | None = None  # only on job.accepted
    url: str | None = None  # only on job.succeeded, when the stack has a web UI
    urls: list[WebEndpoint] | None = None  # every web UI on job.succeeded, main one first
    # Only on step.progress: 0-100, or None when the progress cannot be measured.
    percent: Annotated[int, Field(ge=0, le=100)] | None = None
    plan_id: UUID | None = None  # only on job.succeeded of planning jobs
    # Only on job.failed: whether the same request may succeed if sent again (a refusal of
    # the policy or of the AI will not).
    retryable: bool | None = None
    kept: bool | None = None  # only on job.failed: the deployment was kept for debugging


class JobSummary(BaseModel):
    job_id: UUID
    status: JobStatus
    mode: JobMode
    project_name: str | None  # None for "planning"
    created_at: datetime
    url: str | None
    urls: list[WebEndpoint] = Field(default_factory=list)
    plan_id: UUID | None = None  # set by a succeeded "planning" job
    cancel_requested: bool = False  # a cancellation is under way
    service: str | None = None  # the one service a restart targets
    retryable: bool | None = None  # set once the job failed, as on its job.failed event
    events_url: str


class JobListResponse(BaseModel):
    jobs: list[JobSummary]  # newest first
