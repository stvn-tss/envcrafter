"""Validated AI plans waiting for the user's review (in memory, bounded, short-lived).

The client only ever holds a plan id: the stack itself stays here, so it cannot be
altered between the review and the deployment, which re-validates it anyway.
"""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.models.plan import PlanView
from app.models.stack import Candidate
from app.services.template_catalog import Template


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
