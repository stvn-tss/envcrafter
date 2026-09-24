"""Public API contracts: deployment requests and real-time job events.

Every field coming from the client is validated here, at the edge, before any
business logic runs. Nothing downstream re-parses raw client input.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, RootModel, StringConstraints, field_validator

from app.models.common import ProjectName, StrictModel, TemplateId

# --- Requests -----------------------------------------------------------------

# C0 control characters (except tab, LF and CR), DEL, and Unicode bidirectional
# embeddings/overrides/isolates (U+202A-U+202E, U+2066-U+2069). Controls can
# corrupt logs and terminals; bidi characters can make text render differently
# from what is actually sent to the LLM ("Trojan Source"). Declared as code
# points so the source file itself never contains invisible characters.
_FORBIDDEN_CODEPOINTS = frozenset(
    [
        *range(0x00, 0x09),
        0x0B,
        0x0C,
        *range(0x0E, 0x20),
        0x7F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    ]
)


class TemplateDeploymentRequest(StrictModel):
    mode: Literal["template"]
    # Only a syntactic check here. The id is then looked up in the catalog
    # allow-list and is never used to build a filesystem path directly.
    template_id: TemplateId
    project_name: ProjectName | None = None


class PromptDeploymentRequest(StrictModel):
    mode: Literal["prompt"]
    # The prompt is untrusted free text that will reach the LLM. Validating it
    # does NOT make the LLM output trustworthy: that output goes through its
    # own schema + policy validation before anything is written or executed.
    prompt: Annotated[str, StringConstraints(min_length=3, max_length=2000)]
    project_name: ProjectName | None = None

    @field_validator("prompt")
    @classmethod
    def reject_invisible_characters(cls, value: str) -> str:
        if any(ord(char) in _FORBIDDEN_CODEPOINTS for char in value):
            raise ValueError("prompt contains control or bidirectional override characters")
        return value


class DeploymentRequest(
    RootModel[
        Annotated[
            TemplateDeploymentRequest | PromptDeploymentRequest,
            Field(discriminator="mode"),
        ]
    ]
):
    """Tagged union on `mode`: pydantic picks the sub-model from the tag alone,
    so an unknown mode (e.g. "shell") is rejected instead of being coerced into
    whichever model happens to match."""


# --- Jobs & events ------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EventType(StrEnum):
    JOB_ACCEPTED = "job.accepted"  # carries the full step plan
    STEP_STARTED = "step.started"
    STEP_LOG = "step.log"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_FAILED = "job.failed"


TERMINAL_EVENTS = frozenset({EventType.JOB_SUCCEEDED, EventType.JOB_FAILED})


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


class JobSummary(BaseModel):
    job_id: UUID
    status: JobStatus
    mode: Literal["template", "prompt", "removal"]
    project_name: str
    created_at: datetime
    url: str | None
    events_url: str
