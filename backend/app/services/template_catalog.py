"""Loads the template catalog and validates every template at startup.

A template that fails the compose policy, or whose manifest disagrees with its
compose file, stops the application from starting: a broken or unsafe template
can never be offered to users.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.template import (
    ComponentView,
    TemplateCategory,
    TemplateManifest,
    TemplateView,
)
from app.policy.compose_policy import (
    ComposePolicyError,
    ComposeSpec,
    PolicyContext,
    validate_compose,
)
from app.policy.images import ImageAllowlist
from app.policy.yaml_loader import StrictYAMLError, load_yaml
from app.workspace.renderer import web_hostname


class TemplateCatalogError(ValueError):
    """The catalog on disk is inconsistent; the app refuses to start."""


@dataclass(frozen=True)
class Template:
    manifest: TemplateManifest
    # The parsed source document is kept so every deployment re-runs the policy
    # on it: templates are trusted, never exempt.
    compose_source: dict[str, Any]
    compose: ComposeSpec

    def policy_context(self, allowlist: ImageAllowlist) -> PolicyContext:
        return _policy_context(self.manifest, allowlist)

    def view(self, domain: str) -> TemplateView:
        exposed = [item.service for item in self.manifest.expose]
        components = [
            ComponentView(
                service=component.service,
                name=component.name,
                role=component.role,
                image=self.compose.services[component.service].image,
                web_access=(
                    None
                    if component.service not in exposed
                    else web_hostname(
                        "<project>",
                        domain,
                        None if component.service == exposed[0] else component.service,
                    )
                ),
            )
            for component in self.manifest.components
        ]
        return TemplateView(
            id=self.manifest.id,
            name=self.manifest.name,
            category=self.manifest.category,
            summary=self.manifest.summary,
            description=self.manifest.description,
            components=components,
            volumes=sorted(self.compose.volumes),
            needs_internet=self.manifest.needs_internet,
            access_notes=self.manifest.access_notes,
            tags=self.manifest.tags,
        )


class TemplateCatalog:
    def __init__(self, templates: dict[str, Template]) -> None:
        self._templates = templates

    @classmethod
    def load(cls, root: Path, allowlist: ImageAllowlist) -> "TemplateCatalog":
        """Blocking disk I/O: call through `asyncio.to_thread()` from async code."""
        templates: dict[str, Template] = {}
        for manifest_path in sorted(root.glob("*/*/manifest.yaml")):
            template = _load_template(manifest_path, allowlist)
            if template.manifest.id in templates:
                raise TemplateCatalogError(f"duplicate template id '{template.manifest.id}'")
            templates[template.manifest.id] = template
        return cls(templates)

    def get(self, template_id: str) -> Template | None:
        return self._templates.get(template_id)

    def all(self) -> list[Template]:
        return list(self._templates.values())


def _load_template(manifest_path: Path, allowlist: ImageAllowlist) -> Template:
    template_dir = manifest_path.parent
    try:
        manifest = TemplateManifest.model_validate(
            load_yaml(manifest_path.read_text(encoding="utf-8"))
        )
        compose_source = load_yaml((template_dir / "compose.yaml").read_text(encoding="utf-8"))
    except (OSError, StrictYAMLError, ValidationError) as exc:
        raise TemplateCatalogError(f"{template_dir}: {exc}") from None

    # The folder layout must match the declared identity, so a template can
    # never masquerade as another one or another category.
    if template_dir.name != manifest.id or template_dir.parent.name != manifest.category:
        raise TemplateCatalogError(
            f"{manifest_path}: expected templates/{manifest.category}/{manifest.id}/"
        )
    if manifest.category == TemplateCategory.SECURITY_LAB and manifest.needs_internet:
        raise TemplateCatalogError(f"{manifest.id}: lab templates never get Internet access")

    try:
        compose = validate_compose(compose_source, _policy_context(manifest, allowlist))
    except ComposePolicyError as exc:
        raise TemplateCatalogError(f"{manifest.id}: compose policy violation: {exc}") from None

    documented = [component.service for component in manifest.components]
    if sorted(documented) != sorted(compose.services):
        raise TemplateCatalogError(
            f"{manifest.id}: manifest components {sorted(documented)} must list exactly "
            f"the compose services {sorted(compose.services)}"
        )
    exposed = [item.service for item in manifest.expose]
    if len(set(exposed)) != len(exposed) or not set(exposed) <= set(compose.services):
        raise TemplateCatalogError(
            f"{manifest.id}: exposed services must be unique compose services"
        )
    return Template(manifest=manifest, compose_source=compose_source, compose=compose)


def _policy_context(manifest: TemplateManifest, allowlist: ImageAllowlist) -> PolicyContext:
    return PolicyContext(
        allowlist=allowlist,
        allow_egress=manifest.needs_internet,
        secret_names=frozenset(manifest.secrets),
    )
