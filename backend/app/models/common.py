"""Constrained identifier types reused across models.

Slugs end up in filesystem paths (`workspaces/<project>/`), Docker Compose
project names and network names (`ec-<project>-internal`). Restricting them to
a tiny alphabet makes path traversal (`../`), shell metacharacters, and Docker
name collisions impossible by construction, instead of sanitizing them later.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

# 3-32 chars, starts with a letter, ends with a letter or digit.
PROJECT_NAME_PATTERN = r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$"
ProjectName = Annotated[str, StringConstraints(pattern=PROJECT_NAME_PATTERN)]

# Template ids double as folder names under templates/<category>/<id>/.
TEMPLATE_ID_PATTERN = r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$"
TemplateId = Annotated[str, StringConstraints(pattern=TEMPLATE_ID_PATTERN)]

# Every kind of job the orchestrator runs.
JobMode = Literal["template", "prompt", "plan", "planning", "removal", "stop", "start", "restart"]
# Jobs that create an environment: the only ones that roll back on failure.
DEPLOY_MODES: frozenset[str] = frozenset({"template", "prompt", "plan"})


class StrictModel(BaseModel):
    """Base for every model built from untrusted input.

    extra="forbid": unknown fields are rejected, so a client cannot smuggle
    options the orchestrator never meant to accept (e.g. "privileged": true).
    frozen=True: validated data cannot be mutated after the security checks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
