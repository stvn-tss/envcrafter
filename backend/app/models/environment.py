"""Environment inventory contracts: what a workspace records about itself (meta.json)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.models.common import ProjectName, StrictModel, TemplateId
from app.models.template import ServiceName

DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=60)]


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
    """`workspaces/<project>/meta.json`, written once by the workspace step.

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
