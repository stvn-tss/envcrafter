"""Capability discovery: what this server can do, so the UI adapts before the user acts."""

from typing import Literal

from pydantic import BaseModel


class CapabilitiesResponse(BaseModel):
    """Public facts only: never a secret, a path or a model credential."""

    version: str
    engine: Literal["simulated", "docker"]
    llm_available: bool
    public_domain: str
    project_name_pattern: str
