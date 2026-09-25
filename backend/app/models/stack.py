"""A validated, deployable stack, whatever its origin (template or LLM)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models.template import ExposedPort
from app.policy.compose_policy import ComposeSpec, PolicyContext


@dataclass(frozen=True)
class StackBlueprint:
    """Only ever built from a ComposeSpec returned by `validate_compose()`."""

    title: str
    compose: ComposeSpec
    # First entry = main web UI (<project>.<domain>); others = <service>.<project>.<domain>.
    expose: tuple[ExposedPort, ...]
    secrets: tuple[str, ...]
    # Display name of each service ("MariaDB 11.4 LTS"), for the UI and meta.json.
    service_names: Mapping[str, str] = field(default_factory=dict)
    template_id: str | None = None

    @property
    def uses_egress(self) -> bool:
        return any("egress" in service.networks for service in self.compose.services.values())


@dataclass
class Candidate:
    """A stack proposal (from a template or the LLM) that is NOT validated yet."""

    title: str
    source: dict[str, Any]
    context: PolicyContext
    expose: tuple[ExposedPort, ...]
    secrets: tuple[str, ...]
    # Display name per service, shown in the UI and recorded in meta.json.
    service_names: dict[str, str] = field(default_factory=dict)
    template_id: str | None = None
