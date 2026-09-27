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
# Jobs a user may cancel: a deployment (rolled back like a failure) or an AI analysis.
# Removal and lifecycle jobs are short and must not stop halfway.
CANCELLABLE_MODES: frozenset[str] = frozenset({"template", "prompt", "plan", "planning"})


# C0 control characters (except tab, LF and CR), DEL, and Unicode bidirectional
# embeddings/overrides/isolates (U+202A-U+202E, U+2066-U+2069). Controls can
# corrupt logs and terminals; bidi characters can make text render differently
# from what is actually stored or sent to the LLM ("Trojan Source"). Declared as
# code points so the source file itself never contains invisible characters.
FORBIDDEN_CODEPOINTS = frozenset(
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
# Tab, LF, CR, NEL and the Unicode line and paragraph separators.
_LINE_BREAKS = frozenset([0x09, 0x0A, 0x0D, 0x85, 0x2028, 0x2029])


def reject_invisible_characters(value: str) -> str:
    if any(ord(char) in FORBIDDEN_CODEPOINTS for char in value):
        raise ValueError("text contains control or bidirectional override characters")
    return value


def reject_line_breaks(value: str) -> str:
    """Single-line text: no invisible character, no tab or line break either."""
    reject_invisible_characters(value)
    if any(ord(char) in _LINE_BREAKS for char in value):
        raise ValueError("text must fit on one line")
    return value


class StrictModel(BaseModel):
    """Base for every model built from untrusted input.

    extra="forbid": unknown fields are rejected, so a client cannot smuggle
    options the orchestrator never meant to accept (e.g. "privileged": true).
    frozen=True: validated data cannot be mutated after the security checks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
