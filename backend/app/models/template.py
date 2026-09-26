"""Template catalog models.

A template lives in `templates/<category>/<id>/` and has two files:
* `manifest.yaml`: what the user sees (TemplateManifest below);
* `compose.yaml`: the stack, validated by the compose policy at startup.
"""

import re
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, WithJsonSchema

from app.models.common import StrictModel, TemplateId


class TemplateCategory(StrEnum):
    ITSM = "itsm"
    MEDIA = "media"
    SECURITY_LAB = "security-lab"


CATEGORY_LABELS: dict[TemplateCategory, str] = {
    TemplateCategory.ITSM: "ITSM & Administration",
    TemplateCategory.MEDIA: "Multimedia",
    TemplateCategory.SECURITY_LAB: "Security & Lab",
}

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=60)]
SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,39}$"
ServiceName = Annotated[str, StringConstraints(pattern=SERVICE_NAME_PATTERN)]
SECRET_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{2,63}$"  # noqa: S105 - a name pattern, not a secret
# Built-in workspace variables (EC_PROJECT, EC_HOSTNAME, EC_TZ) use this prefix. A secret
# named like one would silently replace it in the workspace .env and escape the log
# redaction, which skips built-in variables.
RESERVED_SECRET_PREFIX = "EC_"  # noqa: S105 - a name prefix, not a secret
_SECRET_NAME_RE = re.compile(SECRET_NAME_PATTERN)


def is_valid_secret_name(name: str) -> bool:
    """fullmatch, not match: `$` alone would also accept a trailing newline."""
    return bool(_SECRET_NAME_RE.fullmatch(name)) and not name.startswith(RESERVED_SECRET_PREFIX)


def _not_reserved(name: str) -> str:
    if name.startswith(RESERVED_SECRET_PREFIX):
        raise ValueError(f"secret names starting with {RESERVED_SECRET_PREFIX} are reserved")
    return name


SecretName = Annotated[
    str,
    StringConstraints(pattern=SECRET_NAME_PATTERN),
    AfterValidator(_not_reserved),
    # Editors validate manifests with this schema: it states the reserved prefix too.
    WithJsonSchema({"type": "string", "pattern": r"^(?!EC_)[A-Z][A-Z0-9_]{2,63}$"}),
]


class TemplateComponent(StrictModel):
    """One installed tool, shown in the template details."""

    service: ServiceName
    name: ShortText
    role: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ExposedPort(StrictModel):
    service: ServiceName
    port: Annotated[int, Field(ge=1, le=65535)]


LOGO_FILE_PATTERN = r"^logo\.(png|webp)$"


class TemplateFootprint(StrictModel):
    """Approximate weight of a template on linux/amd64 (see README, "Adding a template")."""

    download_mb: Annotated[int, Field(ge=1, le=100_000)]  # compressed images
    memory_mb: Annotated[int, Field(ge=1, le=100_000)]  # steady-state RAM of the stack
    first_start_seconds: Annotated[int, Field(ge=1, le=3600)]  # started -> healthy, cached images


class TemplateManifest(StrictModel):
    id: TemplateId
    name: ShortText
    category: TemplateCategory
    summary: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=800)]
    components: list[TemplateComponent] = Field(min_length=1)
    # Web UIs routed by Traefik. The first one is the main entry point
    # (<project>.localhost); the others get <service>.<project>.localhost.
    expose: list[ExposedPort] = Field(min_length=1, max_length=6)
    # Random values generated per project into the workspace .env file.
    secrets: list[SecretName] = Field(default_factory=list)
    needs_internet: bool = False
    access_notes: list[Annotated[str, StringConstraints(max_length=300)]] = Field(
        default_factory=list
    )
    tags: list[ShortText] = Field(default_factory=list)
    footprint: TemplateFootprint
    # Optional application logo next to the manifest. PNG or WebP only: an SVG is a
    # document that can carry scripts.
    logo: Annotated[str, StringConstraints(pattern=LOGO_FILE_PATTERN)] | None = None


# --- API views ------------------------------------------------------------------


class ComponentView(BaseModel):
    service: str
    name: str
    role: str
    image: str  # exact, pinned image reference from compose.yaml
    # Host name pattern of its web UI, e.g. "<project>.localhost", if exposed.
    web_access: str | None


class TemplateView(BaseModel):
    id: str
    name: str
    category: TemplateCategory
    summary: str
    description: str
    components: list[ComponentView]
    volumes: list[str]
    needs_internet: bool
    access_notes: list[str]
    tags: list[str]
    vulnerable: bool  # at least one image is flagged `vulnerable` in the allow-list
    footprint: TemplateFootprint
    logo_url: str | None


class CategoryInfo(BaseModel):
    id: TemplateCategory
    label: str


class TemplateCatalogResponse(BaseModel):
    categories: list[CategoryInfo]
    templates: list[TemplateView]
