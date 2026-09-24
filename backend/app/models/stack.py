"""A validated, deployable stack, whatever its origin (template or LLM)."""

from dataclasses import dataclass

from app.models.template import ExposedPort
from app.policy.compose_policy import ComposeSpec


@dataclass(frozen=True)
class StackBlueprint:
    """Only ever built from a ComposeSpec returned by `validate_compose()`."""

    title: str
    compose: ComposeSpec
    # First entry = main web UI (<project>.<domain>); others = <service>.<project>.<domain>.
    expose: tuple[ExposedPort, ...]
    secrets: tuple[str, ...]

    @property
    def uses_egress(self) -> bool:
        return any("egress" in service.networks for service in self.compose.services.values())
