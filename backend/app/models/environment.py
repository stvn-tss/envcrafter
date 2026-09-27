"""Environment inventory contracts: what a workspace records about itself (meta.json)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from app.models.common import (
    JobMode,
    ProjectName,
    StrictModel,
    TemplateId,
    reject_invisible_characters,
    reject_line_breaks,
)
from app.models.template import ServiceName

DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=60)]
# Written by the user from the environment drawer: rendered with textContent only.
EnvironmentTitle = Annotated[
    str, StringConstraints(min_length=1, max_length=80), AfterValidator(reject_line_breaks)
]
EnvironmentNotes = Annotated[
    str, StringConstraints(max_length=2000), AfterValidator(reject_invisible_characters)
]


class WebEndpoint(StrictModel):
    """One web UI of an environment. Lists put the main UI first."""

    service: ServiceName
    name: DisplayName
    url: Annotated[str, StringConstraints(min_length=1, max_length=255)]


class EnvironmentService(StrictModel):
    service: ServiceName
    name: DisplayName
    image: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class EnvironmentMeta(StrictModel):
    """`workspaces/<project>/meta.json`, written by the workspace step, then rewritten
    atomically when the user edits the title or the notes.

    It records what was asked for (template, title, web addresses); what is
    actually running comes from Docker. It never contains a secret. It is read
    back as untrusted input: strict schema, size-capped by the reader.
    """

    schema_version: Literal[1] = 1
    project: ProjectName
    title: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    # "prompt" covers every AI-derived deployment, even when the AI picked a template.
    origin: Literal["template", "prompt"]
    template_id: TemplateId | None = None
    created_at: datetime
    services: list[EnvironmentService] = Field(min_length=1, max_length=12)
    urls: list[WebEndpoint] = Field(default_factory=list, max_length=6)
    volumes: list[str] = Field(default_factory=list, max_length=16)
    notes: Annotated[str, StringConstraints(max_length=2000)] = ""


class EnvironmentUpdate(StrictModel):
    """Body of PATCH /api/environments/{project}: the display title, the notes, or both."""

    title: EnvironmentTitle | None = None
    notes: EnvironmentNotes | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "EnvironmentUpdate":
        if self.title is None and self.notes is None:
            raise ValueError("send a title, notes, or both")
        return self


class EnvironmentState(StrEnum):
    PENDING = "pending"  # a deployment job runs, the workspace does not exist yet
    RUNNING = "running"  # every service runs, none unhealthy or starting
    STARTING = "starting"  # every service runs, at least one healthcheck is starting
    DEGRADED = "degraded"  # some services run and some do not (or are unhealthy)
    STOPPED = "stopped"  # containers exist, none runs
    MISSING = "missing"  # no container at all
    UNKNOWN = "unknown"  # the engine could not be queried


class ServiceView(BaseModel):
    service: str
    name: str
    image: str | None
    state: str | None  # Docker state ("running", "exited"...); None without a container
    health: str | None  # "healthy" | "unhealthy" | "starting" | None


class ActiveJobRef(BaseModel):
    job_id: UUID
    mode: JobMode
    events_url: str
    service: str | None = None  # the one service a restart targets
    cancel_requested: bool = False  # a cancellation is under way (the rollback runs)


class EnvironmentView(BaseModel):
    project: str
    title: str
    origin: Literal["template", "prompt"] | None  # None: workspace without meta.json
    template_id: str | None
    created_at: datetime | None
    state: EnvironmentState
    services: list[ServiceView]
    urls: list[WebEndpoint]
    volumes: list[str]
    job: ActiveJobRef | None  # the job running on this environment, if any
    notes: str = ""  # written by the user


class EnvironmentListResponse(BaseModel):
    environments: list[EnvironmentView]  # newest first


class LogLineFrame(BaseModel):
    type: Literal["line"] = "line"
    timestamp: datetime | None
    text: str


class LogEndFrame(BaseModel):
    type: Literal["end"] = "end"
    message: str
