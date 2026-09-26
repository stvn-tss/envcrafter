"""Settings the user changes from the UI (GET/PUT/DELETE /api/settings...).

The API key travels in one direction only: from the browser to the server. Responses say
whether a key is configured and where it comes from, never the key itself.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

from app.models.common import StrictModel

# Anthropic keys are URL-safe tokens ("sk-ant-api03-..."). The alphabet rules out
# whitespace, quotes and control characters: nothing that could break a header or a file.
ApiKey = Annotated[
    str, StringConstraints(min_length=20, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")
]

KeySource = Literal["settings", "environment"]


class LLMKeyRequest(StrictModel):
    """Body of PUT /api/settings/llm-key."""

    api_key: ApiKey


class LLMKeyStatus(BaseModel):
    configured: bool
    # "settings": saved from the UI; "environment": ENVCRAFTER_LLM_API_KEY (.env).
    source: KeySource | None
    # Last characters of the key in use, so the user can tell which key it is.
    key_hint: str | None
    model: str


class SettingsView(BaseModel):
    llm: LLMKeyStatus
