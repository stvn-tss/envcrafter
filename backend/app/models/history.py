"""Activity of an environment (GET /api/environments/{project}/activity)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ActivityEntry(BaseModel):
    at: datetime
    kind: Literal["job", "audit"]
    # The job mode (template, stop, restart...) or the audit action (edit, cancel...).
    action: str
    status: str | None  # a job's status: running, succeeded, failed, cancelled
    message: str  # written for users: never a secret or a prompt
    service: str | None = None
    job_id: UUID | None = None


class ActivityResponse(BaseModel):
    available: bool  # False when the history database is off
    entries: list[ActivityEntry]  # newest first
