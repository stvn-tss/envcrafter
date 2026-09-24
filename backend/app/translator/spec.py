"""The LLM's output contract (StackSpec) and its conversion to a Compose source.

The LLM never writes YAML, commands or file paths. It fills a small JSON
structure: pick a template, or assemble allow-listed images with environment
variables and named volumes. Structured outputs guarantee the JSON *shape*;
everything else (names, images, variables, volumes, networks) is enforced
afterwards by the same compose policy that guards templates.
"""

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, create_model

from app.models.template import ExposedPort


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecEnvVar(_SpecModel):
    name: str
    value: str


class SpecVolume(_SpecModel):
    name: str
    mount_path: str


class SpecService(_SpecModel):
    name: str
    image: str
    purpose: str
    environment: list[SpecEnvVar]
    volumes: list[SpecVolume]
    depends_on: list[str]
    needs_internet: bool


class SpecExpose(_SpecModel):
    service: str
    port: int


class StackSpec(_SpecModel):
    decision: Literal["template", "custom", "unsupported"]
    template_id: str | None
    title: str
    summary: str
    services: list[SpecService]
    expose: SpecExpose | None
    secrets: list[str]
    explanation: str


def build_spec_model(image_refs: list[str], template_ids: list[str]) -> type[StackSpec]:
    """StackSpec whose JSON schema enumerates the allowed images and templates.

    The enums make the model's choices schema-valid by construction; the policy
    still re-checks both lists, since the enum is a convenience, not a control.
    """
    image_choice = cast(Any, Literal).__getitem__(tuple(image_refs))
    template_choice = cast(Any, Literal).__getitem__(tuple(template_ids))
    service_model = create_model(
        "SpecServiceChoice", __base__=SpecService, image=(image_choice, ...)
    )
    return create_model(
        "StackSpecChoice",
        __base__=StackSpec,
        template_id=(template_choice | None, ...),
        services=(list[service_model], ...),
    )


class SpecConversionError(ValueError):
    pass


def spec_to_compose_source(spec: StackSpec) -> tuple[dict[str, Any], ExposedPort | None]:
    """Build a Compose *source* document; validate_compose() runs on it next."""
    services: dict[str, Any] = {}
    volumes: dict[str, None] = {}
    for service in spec.services:
        if service.name in services:
            raise SpecConversionError(f"duplicate service name {service.name!r}")
        document: dict[str, Any] = {
            "image": service.image,
            "networks": ["internal", "egress"] if service.needs_internet else ["internal"],
        }
        if service.environment:
            document["environment"] = {var.name: var.value for var in service.environment}
        if service.volumes:
            document["volumes"] = [f"{vol.name}:{vol.mount_path}" for vol in service.volumes]
            volumes.update(dict.fromkeys(vol.name for vol in service.volumes))
        if service.depends_on:
            document["depends_on"] = list(service.depends_on)
        services[service.name] = document

    expose = None
    if spec.expose is not None:
        if spec.expose.service not in services or not 0 < spec.expose.port < 65536:
            raise SpecConversionError("the exposed service or port is invalid")
        expose = ExposedPort(service=spec.expose.service, port=spec.expose.port)
    return {"services": services, "volumes": volumes}, expose
