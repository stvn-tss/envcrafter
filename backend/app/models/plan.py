"""Review of an AI plan before anything is deployed (GET /api/plans/{plan_id})."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class PlanServiceView(BaseModel):
    service: str
    name: str
    image: str
    purpose: str | None  # LLM or manifest text: rendered with textContent only
    internet: bool
    vulnerable: bool
    web_access: str | None  # host pattern, e.g. "<project>.localhost"


class PlanView(BaseModel):
    plan_id: UUID
    created_at: datetime
    expires_at: datetime
    decision: Literal["template", "custom"]
    title: str
    summary: str
    explanation: str
    template_id: str | None
    services: list[PlanServiceView]
    volumes: list[str]
    secrets: int  # how many passwords will be generated (values never leave the server)
    needs_internet: bool
