"""Validated AI plans waiting for the user's review (in memory, bounded, short-lived).

The client only ever holds a plan id: the stack itself stays here, so it cannot be
altered between the review and the deployment, which re-validates it anyway. A copy is
kept in the history database (plan_body / restore_plan), so a restart of the server
within the review window keeps the plan.
"""

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.models.common import TemplateId
from app.models.plan import PlanView
from app.models.stack import Candidate
from app.models.template import (
    ExposedPort,
    ParameterValue,
    SecretName,
    ServiceName,
)
from app.policy.compose_policy import PolicyContext
from app.policy.images import ImageAllowlist
from app.services.template_catalog import Template, TemplateCatalog

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StoredPlan:
    view: PlanView
    candidate: Candidate  # never handed out: jobs receive a deep copy of its source
    template: Template | None


class PlanStore:
    def __init__(
        self, *, ttl_seconds: float, max_plans: int, now: Callable[[], datetime] = _utcnow
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_plans = max_plans
        self._now = now
        self._plans: OrderedDict[UUID, StoredPlan] = OrderedDict()

    def window(self) -> tuple[datetime, datetime]:
        """(created_at, expires_at) for a plan stored now."""
        now = self._now()
        return now, now + self._ttl

    def add(self, plan: StoredPlan) -> None:
        self._purge()
        self._plans[plan.view.plan_id] = plan
        while len(self._plans) > self._max_plans:
            self._plans.popitem(last=False)  # the oldest plan goes first

    def get(self, plan_id: UUID) -> StoredPlan | None:
        self._purge()
        return self._plans.get(plan_id)

    def _purge(self) -> None:
        now = self._now()
        for plan_id in [pid for pid, plan in self._plans.items() if plan.view.expires_at <= now]:
            del self._plans[plan_id]


# --- Persistence in the history database ------------------------------------------------


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CandidateRecord(_Record):
    title: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    source: dict[str, Any]
    expose: list[ExposedPort] = Field(max_length=6)
    secrets: list[SecretName] = Field(max_length=16)
    parameters: dict[SecretName, ParameterValue] = Field(default_factory=dict, max_length=8)
    service_names: dict[ServiceName, Annotated[str, StringConstraints(max_length=80)]]
    allow_egress: bool


class _PlanRecord(_Record):
    view: PlanView
    template_id: TemplateId | None
    candidate: _CandidateRecord


def plan_body(plan: StoredPlan) -> str:
    """What the history database keeps of a stored plan: its view and its stack."""
    candidate = plan.candidate
    return _PlanRecord(
        view=plan.view,
        template_id=plan.template.manifest.id if plan.template is not None else None,
        candidate=_CandidateRecord(
            title=candidate.title,
            source=candidate.source,
            expose=list(candidate.expose),
            secrets=list(candidate.secrets),
            parameters=dict(candidate.parameters),
            service_names=dict(candidate.service_names),
            allow_egress=candidate.context.allow_egress,
        ),
    ).model_dump_json()


def restore_plan(
    body: str, catalog: TemplateCatalog, allowlist: ImageAllowlist
) -> StoredPlan | None:
    """A plan read back from the history database, or None when it no longer applies
    (unreadable, or its template left the catalog). Its deployment goes through the policy
    again: the database is not trusted more than a request."""
    try:
        record = _PlanRecord.model_validate_json(body)
    except ValidationError:
        logger.warning("Ignoring an unreadable stored plan", exc_info=True)
        return None
    stack = record.candidate
    template: Template | None = None
    if record.template_id is not None:
        template = catalog.get(record.template_id)
        if template is None:
            return None
        context = template.policy_context(allowlist)
    else:
        context = PolicyContext(
            allowlist=allowlist,
            allow_egress=stack.allow_egress,
            secret_names=frozenset(stack.secrets),
        )
    return StoredPlan(
        view=record.view,
        candidate=Candidate(
            title=stack.title,
            source=stack.source,
            context=context,
            expose=tuple(stack.expose),
            secrets=tuple(stack.secrets),
            service_names=dict(stack.service_names),
            template_id=record.template_id,
            parameters=dict(stack.parameters),
        ),
        template=template,
    )
