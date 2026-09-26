"""Deployment orchestrator: turns a validated request into a background job.

The HTTP handler only *schedules* work and answers `202 Accepted` right away.
The pipeline runs as an asyncio task and reports progress exclusively through
the event bus, which makes the WebSocket the single source of truth for progress.

Deployment modes share every step after the first one:

    template -> template loading --+
    prompt ---> AI analysis -------+-> security validation -> workspace -> images
    plan -----> plan loading ------+       -> networks & containers -> startup

Other job modes:

    planning   AI analysis -> security validation; stores a reviewable plan, deploys nothing
    removal    container removal -> workspace removal
    stop       container shutdown
    start      container startup
    restart    container shutdown -> container startup

Only a deployment mode (template, prompt, plan) rolls back: a failure after the
workspace step removes the project (containers, networks, volumes and workspace),
so no half-deployed stack is left behind. When the containers themselves failed to
start, the last log lines of the services that never became ready are copied into
the job first (secrets redacted): the rollback deletes the containers and their logs.
The job stays active until its rollback is over, so no other job can start on the
project meanwhile. Removal and lifecycle jobs never roll back: a failed stop, start or
restart never deletes an environment.
"""

import asyncio
import copy
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import yaml

from app.core.config import Settings
from app.core.timezones import is_known_timezone
from app.engine.base import Engine, EngineError, ServiceStatus, StackHandle
from app.models.common import CANCELLABLE_MODES, DEPLOY_MODES, JobMode
from app.models.deployment import (
    EventType,
    JobStatus,
    LifecycleAction,
    PlanDeploymentRequest,
    PlanRequest,
    PlanStep,
    PromptDeploymentRequest,
    TemplateDeploymentRequest,
)
from app.models.environment import EnvironmentMeta, EnvironmentService, WebEndpoint
from app.models.plan import PlanServiceView, PlanView
from app.models.stack import Candidate, StackBlueprint
from app.models.template import is_valid_secret_name
from app.policy.compose_policy import ComposePolicyError, PolicyContext, validate_compose
from app.policy.images import ImageAllowlist, ImageRefError, parse_image_ref
from app.services.event_bus import JobEventBus
from app.services.log_streams import redact
from app.services.plan_store import PlanStore, StoredPlan
from app.services.template_catalog import Template, TemplateCatalog
from app.translator.client import Translator, TranslatorError
from app.translator.spec import SpecConversionError, StackSpec, spec_to_compose_source
from app.workspace.manager import WorkspaceError, WorkspaceManager
from app.workspace.renderer import (
    compose_project_name,
    edge_network_name,
    render_compose,
    web_hostname,
    web_hostnames,
)

logger = logging.getLogger(__name__)

AnyDeploymentRequest = TemplateDeploymentRequest | PromptDeploymentRequest | PlanDeploymentRequest

_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
# Steps a cancellation interrupts at once: waits on Docker or on the AI whose subprocess
# or HTTP call dies cleanly. The others (in-memory checks, workspace files written from
# a thread) finish first, and the job stops before its next step.
_INTERRUPTIBLE_STEPS = frozenset(
    {"ai_translation", "image_pull", "network_setup", "container_deploy"}
)
# Logs copied into a failed deployment before its rollback deletes the containers.
_FAILURE_LOG_SERVICES = 3
_FAILURE_LOG_LINES = 30
_FAILURE_LOG_LINE_LENGTH = 300
_FAILURE_LOG_TIMEOUT_SECONDS = 30


def job_events_url(job_id: UUID) -> str:
    return f"/ws/jobs/{job_id}"


class UnknownTemplateError(LookupError):
    pass


class UnknownPlanError(LookupError):
    pass


class UnknownEnvironmentError(LookupError):
    pass


class ProjectNameConflictError(ValueError):
    pass


class TranslatorUnavailableError(RuntimeError):
    pass


class UnknownJobError(LookupError):
    pass


class JobNotCancellableError(ValueError):
    """The job cannot be cancelled. The message is safe to show to users."""


class _JobCancelled(Exception):
    """Raised between steps once a cancellation was requested."""


class StepFailedError(Exception):
    """A step failed for a reason that is safe and useful to show to the user."""


@dataclass
class Job:
    id: UUID
    mode: JobMode
    project_name: str | None
    request: AnyDeploymentRequest | None = None
    template: Template | None = None
    # The translator of the key in use when the job was accepted: changing the key in
    # Settings never swaps it under a running analysis.
    translator: Translator | None = None
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    url: str | None = None
    urls: list[WebEndpoint] = field(default_factory=list)
    # Pipeline state, filled step by step.
    prompt: str | None = None
    plan_id: UUID | None = None
    spec: StackSpec | None = None
    candidate: Candidate | None = None
    blueprint: StackBlueprint | None = None
    stack: StackHandle | None = None
    # Cancellation (Orchestrator.cancel): requested by the user; `interruptible` while a
    # step that may be interrupted runs; `finishing` once a failure or cancellation
    # cleanup (the rollback) started.
    cancel_requested: bool = False
    interruptible: bool = False
    finishing: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass
class StepContext:
    """Everything a step handler may touch: its job and a way to report progress."""

    job: Job
    step: PlanStep
    index: int
    total: int
    bus: JobEventBus
    progress_interval: float = 0.5
    clock: Callable[[], float] = time.monotonic
    _last_progress: float | None = field(default=None, init=False, repr=False)

    def emit(self, event_type: EventType, message: str) -> None:
        # Every step event carries its position so the UI can render "2/6"
        # and the remaining steps without keeping its own counters.
        self.bus.publish(
            self.job.id,
            event_type,
            message,
            step_key=self.step.key,
            step_index=self.index,
            step_total=self.total,
        )

    def log(self, message: str) -> None:
        self.emit(EventType.STEP_LOG, message)

    def progress(self, percent: int | None, message: str) -> None:
        """Measurable progress, throttled to one event per interval; 100% always goes out."""
        if percent is not None:
            percent = max(0, min(100, percent))
        now = self.clock()
        recent = (
            self._last_progress is not None and now - self._last_progress < self.progress_interval
        )
        if recent and percent != 100:
            return
        self._last_progress = now
        self.bus.publish(
            self.job.id,
            EventType.STEP_PROGRESS,
            message,
            step_key=self.step.key,
            step_index=self.index,
            step_total=self.total,
            percent=percent,
        )


StepHandler = Callable[[StepContext], Awaitable[None]]
Plan = list[tuple[PlanStep, StepHandler]]


class Orchestrator:
    def __init__(
        self,
        *,
        bus: JobEventBus,
        catalog: TemplateCatalog,
        allowlist: ImageAllowlist,
        workspaces: WorkspaceManager,
        engine: Engine,
        translator: Translator | None,
        settings: Settings,
        plans: PlanStore,
    ) -> None:
        self._bus = bus
        self._catalog = catalog
        self._allowlist = allowlist
        self._workspaces = workspaces
        self._engine = engine
        self._translator = translator
        self._settings = settings
        self._plans = plans
        self._jobs: dict[UUID, Job] = {}
        # The event loop keeps only *weak* references to tasks: without this set,
        # a running job could be garbage-collected mid-flight. It also lets
        # shutdown() cancel in-flight jobs cleanly.
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, job_id: UUID) -> Job | None:
        return self._jobs.get(job_id)

    def get_plan(self, plan_id: UUID) -> StoredPlan | None:
        return self._plans.get(plan_id)

    def list_jobs(self, *, active_only: bool = False) -> list[Job]:
        """Retained jobs, newest first (finished ones are kept `job_retention_seconds`)."""
        jobs = [
            job for job in self._jobs.values() if not active_only or job.status in _ACTIVE_STATUSES
        ]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def active_job(self, project: str) -> Job | None:
        return next(
            (
                job
                for job in self._jobs.values()
                if job.project_name == project and job.status in _ACTIVE_STATUSES
            ),
            None,
        )

    @property
    def has_translator(self) -> bool:
        """True when natural-language requests can be served."""
        return self._translator is not None

    def set_translator(self, translator: Translator | None) -> None:
        """Use another translator (a key saved or removed in Settings) for new jobs."""
        self._translator = translator

    def cancel(self, job_id: UUID) -> Job:
        """Stop a deployment or an AI analysis. A step waiting on Docker or on the AI is
        interrupted at once; any other step finishes first. A deployment then rolls back
        like a failure, and the job ends with `job.cancelled`."""
        job = self._jobs.get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        if job.mode not in CANCELLABLE_MODES:
            raise JobNotCancellableError("Only deployments and AI analyses can be cancelled.")
        if job.status not in _ACTIVE_STATUSES:
            raise JobNotCancellableError("This job is already over.")
        if job.cancel_requested or job.finishing:
            raise JobNotCancellableError("This job is already stopping.")
        job.cancel_requested = True
        logger.info("Job %s: cancellation requested", job.id)
        if job.interruptible and job.task is not None:
            job.task.cancel()
        return job

    async def submit(self, request: AnyDeploymentRequest) -> Job:
        """Check business rules, register the job and schedule its pipeline.

        Cheap checks run before scheduling so the client gets a 4xx immediately,
        instead of a job that fails a second later.
        """
        template: Template | None = None
        stored: StoredPlan | None = None
        if isinstance(request, TemplateDeploymentRequest):
            # Allow-list lookup: the id never touches the filesystem directly.
            template = self._catalog.get(request.template_id)
            if template is None:
                raise UnknownTemplateError(request.template_id)
        elif isinstance(request, PlanDeploymentRequest):
            stored = self._plans.get(request.plan_id)
            if stored is None:
                raise UnknownPlanError(request.plan_id)
            template = stored.template
        elif self._translator is None:
            raise TranslatorUnavailableError

        project = request.project_name or self._generate_project_name(template)
        job = Job(
            id=uuid4(),
            mode=request.mode,
            project_name=project,
            request=request,
            template=template,
            prompt=request.prompt if isinstance(request, PromptDeploymentRequest) else None,
            plan_id=stored.view.plan_id if stored is not None else None,
            translator=self._translator if isinstance(request, PromptDeploymentRequest) else None,
        )
        await self._reserve(job, must_exist=False)

        first_step: tuple[PlanStep, StepHandler]
        if isinstance(request, TemplateDeploymentRequest):
            first_step = (
                PlanStep(key="template_resolution", label="Template loading"),
                self._load_template,
            )
        elif stored is not None:
            first_step = (PlanStep(key="plan_loading", label="Plan loading"), self._load_plan)
        else:
            first_step = (PlanStep(key="ai_translation", label="AI analysis"), self._translate)
        # Every path goes through security validation: templates and reviewed plans are
        # trusted, not exempt.
        return self._launch(job, [first_step, *self._deploy_steps()])

    def _deploy_steps(self) -> Plan:
        return [
            (PlanStep(key="security_validation", label="Security validation"), self._validate),
            (PlanStep(key="workspace_setup", label="Workspace creation"), self._write_workspace),
            (PlanStep(key="image_pull", label="Image download"), self._pull_images),
            (PlanStep(key="network_setup", label="Network provisioning"), self._create_stack),
            (PlanStep(key="container_deploy", label="Container startup"), self._start_stack),
        ]

    async def submit_planning(self, request: PlanRequest) -> Job:
        """Analyse and validate a request into a stored plan; nothing is deployed."""
        if self._translator is None:
            raise TranslatorUnavailableError
        job = Job(
            id=uuid4(),
            mode="planning",
            project_name=None,
            prompt=request.prompt,
            translator=self._translator,
        )
        self._jobs[job.id] = job  # no project to reserve
        return self._launch(
            job,
            [
                (PlanStep(key="ai_translation", label="AI analysis"), self._translate),
                (
                    PlanStep(key="security_validation", label="Security validation"),
                    self._validate_and_store_plan,
                ),
            ],
        )

    async def submit_removal(self, project: str) -> Job:
        job = Job(id=uuid4(), mode="removal", project_name=project)
        await self._reserve(job, must_exist=True)
        return self._launch(
            job,
            [
                (PlanStep(key="teardown", label="Container removal"), self._teardown),
                (
                    PlanStep(key="workspace_cleanup", label="Workspace removal"),
                    self._remove_workspace,
                ),
            ],
        )

    async def submit_lifecycle(self, project: str, action: LifecycleAction) -> Job:
        job = Job(id=uuid4(), mode=action, project_name=project)
        await self._reserve(job, must_exist=True)
        stop_step = (
            PlanStep(key="container_stop", label="Container shutdown"),
            self._stop_existing,
        )
        start_step = (
            PlanStep(key="container_start", label="Container startup"),
            self._start_existing,
        )
        plans: dict[LifecycleAction, Plan] = {
            "stop": [stop_step],
            "start": [start_step],
            "restart": [stop_step, start_step],
        }
        return self._launch(job, plans[action])

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- Scheduling -------------------------------------------------------------

    @staticmethod
    def _project(job: Job) -> str:
        if job.project_name is None:
            raise RuntimeError("this step needs a project")
        return job.project_name

    async def _reserve(self, job: Job, *, must_exist: bool) -> None:
        project = self._project(job)
        if any(
            other.project_name == project and other.status in _ACTIVE_STATUSES
            for other in self._jobs.values()
        ):
            raise ProjectNameConflictError(project)
        # Register before awaiting, so a concurrent request for the same name
        # sees this job and gets a conflict.
        self._jobs[job.id] = job
        exists = await asyncio.to_thread(self._workspaces.exists, project)
        if exists != must_exist:
            del self._jobs[job.id]
            if must_exist:
                raise UnknownEnvironmentError(project)
            raise ProjectNameConflictError(project)

    def _launch(self, job: Job, plan: Plan) -> Job:
        # Open the channel before the task starts, so a client that connects
        # right after the 202 always finds it (and gets the full history).
        self._bus.open_channel(job.id)
        task = asyncio.create_task(self._run(job, plan), name=f"job-{job.id}")
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, plan: Plan) -> None:
        job.status = JobStatus.RUNNING
        # The full plan is announced first: the UI can show remaining steps from t=0.
        self._bus.publish(
            job.id,
            EventType.JOB_ACCEPTED,
            _accepted_message(job),
            plan=[step for step, _ in plan],
        )

        ctx: StepContext | None = None
        try:
            for index, (step, handler) in enumerate(plan, start=1):
                if job.cancel_requested:
                    raise _JobCancelled
                ctx = StepContext(
                    job=job,
                    step=step,
                    index=index,
                    total=len(plan),
                    bus=self._bus,
                    progress_interval=self._settings.progress_interval_seconds,
                )
                ctx.emit(EventType.STEP_STARTED, f"[{index}/{len(plan)}] {step.label}")
                job.interruptible = step.key in _INTERRUPTIBLE_STEPS
                try:
                    await handler(ctx)
                finally:
                    job.interruptible = False
                ctx.emit(EventType.STEP_COMPLETED, f"[{index}/{len(plan)}] {step.label} done")
            if job.cancel_requested:  # accepted while the last step could not be interrupted
                raise _JobCancelled
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if not (job.cancel_requested and task is not None and task.cancelling() == 1):
                job.status = JobStatus.FAILED
                self._publish_failure(job, ctx, "Interrupted: the server is shutting down.")
                raise
            task.uncancel()  # our own cancel(): the job ends normally, as cancelled
            await self._finish_cancelled(job, ctx)
        except _JobCancelled:
            await self._finish_cancelled(job, ctx)
        except (StepFailedError, TranslatorError, EngineError) as exc:
            # These messages are written by us for users: safe to forward.
            await self._fail(job, ctx, str(exc))
        except Exception:
            # Full details (stack trace, paths, upstream errors) stay in server
            # logs. Clients get a generic message: internals never go on the wire.
            logger.exception("Job %s failed", job.id)
            await self._fail(job, ctx, "Unexpected error. See server logs for details.")
        else:
            job.status = JobStatus.SUCCEEDED
            self._bus.publish(
                job.id,
                EventType.JOB_SUCCEEDED,
                _success_message(job),
                url=job.url,
                urls=job.urls or None,
                plan_id=job.plan_id if job.mode == "planning" else None,
            )
        finally:
            # Keep the history for late viewers and reconnects, then free memory.
            asyncio.get_running_loop().call_later(
                self._settings.job_retention_seconds, self._forget, job.id
            )

    async def _fail(self, job: Job, ctx: StepContext | None, message: str) -> None:
        # The rollback runs while the job is still RUNNING: the project stays reserved,
        # so no start or removal can race `compose down` and the workspace deletion.
        # The status flips before the terminal event, which the inventory cache relies on.
        job.finishing = True
        try:
            # Only a deployment rolls back: a failed stop or start never deletes an environment.
            if ctx is not None and job.stack is not None and job.mode in DEPLOY_MODES:
                if ctx.step.key == "container_deploy":
                    await self._report_unready_services(job.stack, ctx)
                await self._rollback(job.stack, ctx)
        finally:
            job.status = JobStatus.FAILED
        self._publish_failure(job, ctx, message)

    async def _finish_cancelled(self, job: Job, ctx: StepContext | None) -> None:
        """Roll a cancelled deployment back like a failed one, then end the job. The job
        stays RUNNING until then, so the project stays reserved during the rollback."""
        job.finishing = True
        rolled_back: bool | None = None  # None: nothing had been created yet
        try:
            if ctx is not None and job.stack is not None and job.mode in DEPLOY_MODES:
                ctx.log("Cancelled by the user")
                rolled_back = await self._rollback(job.stack, ctx)
        finally:
            job.status = JobStatus.CANCELLED
        logger.info("Job %s cancelled", job.id)
        self._bus.publish(job.id, EventType.JOB_CANCELLED, _cancelled_message(job, rolled_back))

    async def _report_unready_services(self, stack: StackHandle, ctx: StepContext) -> None:
        """Copy the last log lines of every service that did not become ready into the job,
        before the rollback deletes the containers: "startup failed" alone has no cause.

        Best effort and bounded in time: it never delays or prevents the rollback. Secrets
        are redacted exactly as in the logs viewer; if they cannot be read, nothing is shown.
        """
        try:
            async with asyncio.timeout(_FAILURE_LOG_TIMEOUT_SECONDS):
                observed = (await self._engine.status([stack])).get(stack.project, [])
                unready = [status for status in observed if not is_ready(status)]
                if not unready:
                    return
                secret_values = await asyncio.to_thread(
                    self._workspaces.read_secret_values, stack.project
                )
                for status in unready[:_FAILURE_LOG_SERVICES]:
                    lines = await self._engine.recent_logs(
                        stack, status.service, tail=_FAILURE_LOG_LINES
                    )
                    state = status.health if status.health else status.state
                    ctx.log(f"Last log lines of {status.service} ({state}):")
                    for line in lines:
                        text = redact(line.text, secret_values)[:_FAILURE_LOG_LINE_LENGTH]
                        ctx.log(f"  {status.service} | {text}")
                    if not lines:
                        ctx.log(f"  {status.service} | (no output)")
        except Exception:
            logger.warning(
                "Could not read the logs of %s before rollback", stack.project, exc_info=True
            )

    async def _rollback(self, stack: StackHandle, ctx: StepContext) -> bool:
        """Remove everything created for the project; False when something was left."""
        ctx.log("Rolling back: removing everything created for this project")
        try:
            await self._engine.remove(stack, ctx.log)
            await self._workspaces.remove(stack.project)
        except Exception:
            logger.exception("Rollback of %s failed", stack.project)
            ctx.log("Rollback incomplete: see server logs")
            return False
        return True

    def _publish_failure(self, job: Job, ctx: StepContext | None, message: str) -> None:
        if ctx is not None:
            ctx.emit(EventType.STEP_FAILED, f"[{ctx.index}/{ctx.total}] {ctx.step.label} failed")
        self._bus.publish(job.id, EventType.JOB_FAILED, message)

    def _forget(self, job_id: UUID) -> None:
        self._jobs.pop(job_id, None)
        self._bus.discard_channel(job_id)

    @staticmethod
    def _generate_project_name(template: Template | None) -> str:
        # Random suffix: several instances of the same template can coexist.
        prefix = template.manifest.id[:24].rstrip("-") if template else "env"
        return f"{prefix}-{secrets.token_hex(2)}"

    def _image_title(self, image: str) -> str:
        """Allow-list title of an image ("MariaDB 11.4 LTS"), or the reference itself."""
        try:
            allowed = self._allowlist.find(parse_image_ref(image))
        except ImageRefError:
            allowed = None
        return allowed.title if allowed is not None else image[:60]

    def _endpoints(self, blueprint: StackBlueprint, project: str) -> list[WebEndpoint]:
        hosts = web_hostnames(blueprint, project, self._settings.public_domain)
        return [
            WebEndpoint(
                service=exposed.service,
                name=blueprint.service_names.get(exposed.service, exposed.service)[:60],
                url=f"http://{hosts[exposed.service]}",
            )
            for exposed in blueprint.expose
        ]

    def _meta(self, job: Job, blueprint: StackBlueprint) -> EnvironmentMeta:
        project = self._project(job)
        return EnvironmentMeta(
            project=project,
            title=blueprint.title[:80] or project,
            origin="template" if job.mode == "template" else "prompt",
            template_id=blueprint.template_id,
            created_at=datetime.now(UTC),
            services=[
                EnvironmentService(
                    service=name,
                    name=blueprint.service_names.get(name, name)[:60],
                    image=service.image,
                )
                for name, service in blueprint.compose.services.items()
            ],
            urls=self._endpoints(blueprint, project),
            volumes=sorted(blueprint.compose.volumes),
        )

    # --- Step handlers ------------------------------------------------------------

    async def _load_template(self, ctx: StepContext) -> None:
        template = ctx.job.template
        if template is None:
            raise RuntimeError("template job without a resolved template")
        self._use_template(ctx, template)

    async def _load_plan(self, ctx: StepContext) -> None:
        stored = self._plans.get(ctx.job.plan_id) if ctx.job.plan_id is not None else None
        if stored is None:
            raise StepFailedError("This plan expired. Analyse the request again.")
        ctx.job.template = stored.template
        # A private copy: the stored plan stays pristine for another deployment.
        ctx.job.candidate = replace(
            stored.candidate,
            source=copy.deepcopy(stored.candidate.source),
            service_names=dict(stored.candidate.service_names),
        )
        ctx.log(f"Plan '{stored.view.title}' loaded ({len(stored.view.services)} service(s))")

    def _use_template(self, ctx: StepContext, template: Template) -> None:
        manifest = template.manifest
        ctx.log(f"Template '{manifest.name}' ({manifest.category.value})")
        ctx.log("Components: " + ", ".join(component.name for component in manifest.components))
        ctx.job.template = template
        ctx.job.candidate = Candidate(
            title=manifest.name,
            source=template.compose_source,
            context=template.policy_context(self._allowlist),
            expose=tuple(manifest.expose),
            secrets=tuple(manifest.secrets),
            service_names={c.service: c.name for c in manifest.components},
            template_id=manifest.id,
        )

    async def _translate(self, ctx: StepContext) -> None:
        prompt, translator = ctx.job.prompt, ctx.job.translator
        if translator is None or prompt is None:
            raise RuntimeError("AI step scheduled without a translator or a prompt")

        ctx.log(f"Asking {translator.model} for a deployment plan")
        spec = await translator.translate(prompt)
        ctx.job.spec = spec
        ctx.log(f"Plan: {spec.title[:80]} - {spec.summary[:240]}")

        if spec.decision == "unsupported":
            raise StepFailedError(f"This request cannot be deployed: {spec.explanation[:300]}")
        if spec.decision == "template":
            template = self._catalog.get(spec.template_id or "")
            if template is None:
                raise StepFailedError("The AI chose a template that does not exist.")
            self._use_template(ctx, template)
            return

        try:
            source, expose = spec_to_compose_source(spec)
        except SpecConversionError as exc:
            raise StepFailedError(f"The AI plan is inconsistent: {exc}") from None
        if not all(is_valid_secret_name(name) for name in spec.secrets):
            raise StepFailedError("The AI plan declares invalid secret names.")
        for service in spec.services:
            ctx.log(f"{service.name}: {service.image} - {service.purpose[:120]}")
        ctx.job.candidate = Candidate(
            title=spec.title[:60] or "Custom stack",
            source=source,
            context=PolicyContext(
                allowlist=self._allowlist,
                allow_egress=any(service.needs_internet for service in spec.services),
                secret_names=frozenset(spec.secrets),
            ),
            expose=(expose,) if expose else (),
            secrets=tuple(spec.secrets),
            service_names={
                service.name: self._image_title(service.image) for service in spec.services
            },
        )

    async def _validate(self, ctx: StepContext) -> None:
        candidate = ctx.job.candidate
        if candidate is None:
            raise RuntimeError("validation without a candidate stack")
        try:
            compose = validate_compose(candidate.source, candidate.context)
        except ComposePolicyError as exc:
            for violation in exc.violations[:20]:
                ctx.log(f"Rejected - {violation}")
            raise StepFailedError("The stack was rejected by the security policy.") from None

        ctx.log(f"{len(compose.services)} service(s) passed the schema and policy checks")
        ctx.log("Allow-listed images: " + ", ".join(s.image for s in compose.services.values()))
        blueprint = StackBlueprint(
            title=candidate.title,
            compose=compose,
            expose=candidate.expose,
            secrets=candidate.secrets,
            service_names=dict(candidate.service_names),
            template_id=candidate.template_id,
        )
        online = [name for name, s in compose.services.items() if "egress" in s.networks]
        ctx.log(
            "Network: isolated project network"
            + (f", Internet access for {', '.join(online)}" if online else ", no Internet access")
        )
        ctx.job.blueprint = blueprint

    async def _validate_and_store_plan(self, ctx: StepContext) -> None:
        await self._validate(ctx)
        candidate, blueprint = ctx.job.candidate, ctx.job.blueprint
        if candidate is None or blueprint is None:
            raise RuntimeError("plan step without a validated stack")
        created_at, expires_at = self._plans.window()
        view = self._plan_view(uuid4(), created_at, expires_at, ctx.job, blueprint)
        self._plans.add(
            StoredPlan(
                view=view,
                candidate=replace(candidate, source=copy.deepcopy(candidate.source)),
                template=ctx.job.template,
            )
        )
        ctx.job.plan_id = view.plan_id
        ctx.log(f"Plan kept for review until {expires_at:%H:%M} UTC")

    def _plan_view(
        self,
        plan_id: UUID,
        created_at: datetime,
        expires_at: datetime,
        job: Job,
        blueprint: StackBlueprint,
    ) -> PlanView:
        spec = job.spec
        if job.template is not None:
            purposes = {c.service: c.role for c in job.template.manifest.components}
        else:
            purposes = {s.name: s.purpose for s in spec.services} if spec is not None else {}
        exposed = [item.service for item in blueprint.expose]
        domain = self._settings.public_domain
        services = []
        for name, service in blueprint.compose.services.items():
            allowed = self._allowlist.find(parse_image_ref(service.image))
            purpose = purposes.get(name)
            services.append(
                PlanServiceView(
                    service=name,
                    name=blueprint.service_names.get(name, name),
                    image=service.image,
                    purpose=purpose[:200] if purpose else None,
                    internet="egress" in service.networks,
                    vulnerable=allowed is not None and allowed.vulnerable,
                    web_access=(
                        web_hostname("<project>", domain, None if name == exposed[0] else name)
                        if name in exposed
                        else None
                    ),
                )
            )
        return PlanView(
            plan_id=plan_id,
            created_at=created_at,
            expires_at=expires_at,
            decision="template" if job.template is not None else "custom",
            # The title the review shows must match what actually gets deployed
            # (Plan ready: message, meta.json, dashboard): the candidate/blueprint
            # title (manifest name for a template, spec.title[:60] for a custom
            # stack), never the raw, untruncated spec.title.
            title=blueprint.title,
            summary=spec.summary[:400] if spec is not None else "",
            explanation=spec.explanation[:600] if spec is not None else "",
            template_id=blueprint.template_id,
            services=services,
            volumes=sorted(blueprint.compose.volumes),
            secrets=len(blueprint.secrets),
            needs_internet=blueprint.uses_egress,
        )

    async def _write_workspace(self, ctx: StepContext) -> None:
        blueprint = ctx.job.blueprint
        if blueprint is None:
            raise RuntimeError("workspace step without a validated blueprint")
        project = self._project(ctx.job)
        domain = self._settings.public_domain
        document = render_compose(blueprint, project=project, domain=domain)
        variables = {
            "EC_PROJECT": project,
            "EC_HOSTNAME": web_hostname(project, domain),
            "EC_TZ": self._timezone(ctx),
            **WorkspaceManager.generate_secrets(blueprint.secrets),
        }
        workspace = await self._workspaces.create(
            project, document, variables, self._meta(ctx.job, blueprint)
        )
        ctx.job.stack = StackHandle(
            project=project,
            compose_project=compose_project_name(project),
            compose_file=workspace.compose_file,
            edge_network=edge_network_name(project) if blueprint.expose else None,
        )
        ctx.log(
            f"Workspace '{project}' written: compose.yaml and .env "
            f"({len(blueprint.secrets)} generated secret(s), owner-only permissions)"
        )

    def _timezone(self, ctx: StepContext) -> str:
        """The browser's time zone when the server knows it, else the configured default."""
        requested = ctx.job.request.timezone if ctx.job.request is not None else None
        if requested is not None and is_known_timezone(requested):
            ctx.log(f"Time zone: {requested}")
            return requested
        if requested is not None:
            ctx.log(f"Unknown time zone '{requested}': using {self._settings.timezone}")
        return self._settings.timezone

    async def _pull_images(self, ctx: StepContext) -> None:
        await self._engine.pull(self._stack(ctx), ctx.log, ctx.progress)

    async def _create_stack(self, ctx: StepContext) -> None:
        await self._engine.create(self._stack(ctx), ctx.log)

    async def _start_stack(self, ctx: StepContext) -> None:
        blueprint = ctx.job.blueprint
        services = list(blueprint.compose.services) if blueprint is not None else []
        await self._start_watched(ctx, self._stack(ctx), services)
        if blueprint is not None and blueprint.expose:
            ctx.job.urls = self._endpoints(blueprint, self._project(ctx.job))
            for endpoint in ctx.job.urls:
                ctx.log(f"Web UI of {endpoint.service}: {endpoint.url}")
            ctx.job.url = ctx.job.urls[0].url

    async def _start_watched(
        self, ctx: StepContext, stack: StackHandle, expected: Sequence[str]
    ) -> None:
        """Start the stack; meanwhile report every few seconds how many services are ready."""
        watcher = asyncio.create_task(self._watch_health(ctx, stack, expected))
        try:
            await self._engine.start(stack, ctx.log)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _watch_health(
        self, ctx: StepContext, stack: StackHandle, expected: Sequence[str]
    ) -> None:
        while True:
            await asyncio.sleep(self._settings.health_poll_seconds)
            try:
                observed = (await self._engine.status([stack])).get(stack.project, [])
            except Exception:
                # A sign of life only: never let it fail the deployment.
                logger.debug("Health watcher could not read %s", stack.project, exc_info=True)
                continue
            ctx.progress(*health_summary(expected, observed))

    async def _existing_stack(
        self, project: str, *, tolerate_unreadable: bool = False
    ) -> tuple[StackHandle, list[str]]:
        """Stack handle and service names of an existing workspace. The edge network is
        only set when the rendered file declares one (stacks without a web UI have none).

        With `tolerate_unreadable` (removal), a compose file that cannot be read or parsed
        is logged server-side and the conventional edge network is assumed: removing an
        environment must not depend on the file it is about to delete."""
        workspace = await asyncio.to_thread(self._workspaces.get, project)
        if workspace is None:
            raise StepFailedError("This environment has no workspace.")
        services: object
        try:
            document = await asyncio.to_thread(self._workspaces.read_compose, project)
        except (OSError, ValueError, yaml.YAMLError, WorkspaceError):
            if not tolerate_unreadable:
                raise
            logger.warning(
                "Unreadable compose file of %s: removing it anyway", project, exc_info=True
            )
            # The engine tolerates detaching the reverse proxy from an absent network.
            has_edge, services = True, None
        else:
            networks = document.get("networks")
            has_edge = isinstance(networks, dict) and "edge" in networks
            services = document.get("services")
        stack = StackHandle(
            project=project,
            compose_project=compose_project_name(project),
            compose_file=workspace.compose_file,
            edge_network=edge_network_name(project) if has_edge else None,
        )
        return stack, list(services) if isinstance(services, dict) else []

    async def _teardown(self, ctx: StepContext) -> None:
        stack, _ = await self._existing_stack(self._project(ctx.job), tolerate_unreadable=True)
        await self._engine.remove(stack, ctx.log)

    async def _stop_existing(self, ctx: StepContext) -> None:
        stack, _ = await self._existing_stack(self._project(ctx.job))
        await self._engine.stop(stack, ctx.log)

    async def _start_existing(self, ctx: StepContext) -> None:
        project = self._project(ctx.job)
        stack, services = await self._existing_stack(project)
        await self._start_watched(ctx, stack, services)
        meta = await asyncio.to_thread(self._workspaces.read_meta, project)
        if meta is not None and meta.urls:
            ctx.job.urls = list(meta.urls)
            ctx.job.url = meta.urls[0].url

    async def _remove_workspace(self, ctx: StepContext) -> None:
        project = self._project(ctx.job)
        await self._workspaces.remove(project)
        ctx.log(f"Workspace '{project}' deleted, including its secrets")

    @staticmethod
    def _stack(ctx: StepContext) -> StackHandle:
        if ctx.job.stack is None:
            raise RuntimeError("engine step without a workspace")
        return ctx.job.stack


def is_ready(status: ServiceStatus) -> bool:
    """Running, and healthy or without a healthcheck."""
    return status.state == "running" and status.health in (None, "healthy")


def health_summary(
    expected: Sequence[str], observed: Sequence[ServiceStatus]
) -> tuple[int | None, str]:
    """(percent, message) for the health watcher: ready = running and not unhealthy/starting."""
    ready = {s.service for s in observed if is_ready(s)}
    waiting = [name for name in expected if name not in ready]
    total = len(expected)
    message = f"{total - len(waiting)} of {total} services ready"
    if waiting:
        message += " · waiting for " + ", ".join(waiting[:3]) + ("…" if len(waiting) > 3 else "")
    return ((total - len(waiting)) * 100 // total if total else None), message


def _accepted_message(job: Job) -> str:
    if job.mode == "planning":
        return "AI analysis of the request accepted"
    project = job.project_name
    if job.mode in DEPLOY_MODES:
        return f"Deployment of '{project}' accepted ({job.mode})"
    verbs = {"removal": "Removal", "stop": "Stop", "start": "Start", "restart": "Restart"}
    return f"{verbs[job.mode]} of '{project}' accepted"


def _cancelled_message(job: Job, rolled_back: bool | None) -> str:
    if job.mode == "planning":
        return "Analysis cancelled"
    project = job.project_name
    if rolled_back is None:
        return f"Deployment of '{project}' cancelled before anything was created"
    if rolled_back:
        return f"Deployment of '{project}' cancelled: everything created for it was removed"
    return f"Deployment of '{project}' cancelled; its rollback is incomplete: see server logs"


def _success_message(job: Job) -> str:
    if job.mode == "planning":
        return f"Plan ready: {job.candidate.title if job.candidate else 'custom stack'}"
    project = job.project_name
    at = f" at {job.url}" if job.url else ""
    if job.mode in DEPLOY_MODES:
        return f"Environment '{project}' is ready{at}"
    if job.mode == "removal":
        return f"Environment '{project}' is removed"
    if job.mode == "stop":
        return f"Environment '{project}' is stopped"
    return f"Environment '{project}' is running{at}"
