"""Deployment orchestrator: turns a validated request into a background job.

The HTTP handler only *schedules* work and answers `202 Accepted` right away.
The pipeline runs as an asyncio task and reports progress exclusively through
the event bus, which makes the WebSocket the single source of truth for progress.

Pipeline (both entry points share every step after the first one):

    template -> template loading --+
                                   +-> security validation -> workspace -> images
    prompt ---> AI analysis -------+       -> networks & containers -> startup

A failure after the workspace step rolls the project back (containers,
networks, volumes and workspace), so no half-deployed stack is left behind.
"""

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from app.core.config import Settings
from app.engine.base import Engine, EngineError, StackHandle
from app.models.deployment import (
    EventType,
    JobStatus,
    PlanStep,
    PromptDeploymentRequest,
    TemplateDeploymentRequest,
)
from app.models.stack import StackBlueprint
from app.models.template import SECRET_NAME_PATTERN, ExposedPort
from app.policy.compose_policy import ComposePolicyError, PolicyContext, validate_compose
from app.policy.images import ImageAllowlist
from app.services.event_bus import JobEventBus
from app.services.template_catalog import Template, TemplateCatalog
from app.translator.client import Translator, TranslatorError
from app.translator.spec import SpecConversionError, spec_to_compose_source
from app.workspace.manager import WorkspaceManager
from app.workspace.renderer import (
    compose_project_name,
    edge_network_name,
    render_compose,
    web_hostname,
    web_hostnames,
)

logger = logging.getLogger(__name__)

AnyDeploymentRequest = TemplateDeploymentRequest | PromptDeploymentRequest
JobKind = Literal["template", "prompt", "removal"]

_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
_SECRET_NAME_RE = re.compile(SECRET_NAME_PATTERN)


class UnknownTemplateError(LookupError):
    pass


class UnknownEnvironmentError(LookupError):
    pass


class ProjectNameConflictError(ValueError):
    pass


class TranslatorUnavailableError(RuntimeError):
    pass


class StepFailedError(Exception):
    """A step failed for a reason that is safe and useful to show to the user."""


@dataclass
class Candidate:
    """A stack proposal (from a template or the LLM) that is NOT validated yet."""

    title: str
    source: dict[str, Any]
    context: PolicyContext
    expose: tuple[ExposedPort, ...]
    secrets: tuple[str, ...]


@dataclass
class Job:
    id: UUID
    mode: JobKind
    project_name: str
    request: AnyDeploymentRequest | None = None
    template: Template | None = None
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    url: str | None = None
    # Pipeline state, filled step by step.
    candidate: Candidate | None = None
    blueprint: StackBlueprint | None = None
    stack: StackHandle | None = None


@dataclass
class StepContext:
    """Everything a step handler may touch: its job and a way to report progress."""

    job: Job
    step: PlanStep
    index: int
    total: int
    bus: JobEventBus

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
    ) -> None:
        self._bus = bus
        self._catalog = catalog
        self._allowlist = allowlist
        self._workspaces = workspaces
        self._engine = engine
        self._translator = translator
        self._settings = settings
        self._jobs: dict[UUID, Job] = {}
        # The event loop keeps only *weak* references to tasks: without this set,
        # a running job could be garbage-collected mid-flight. It also lets
        # shutdown() cancel in-flight jobs cleanly.
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, job_id: UUID) -> Job | None:
        return self._jobs.get(job_id)

    async def submit(self, request: AnyDeploymentRequest) -> Job:
        """Check business rules, register the job and schedule its pipeline.

        Cheap checks run before scheduling so the client gets a 4xx immediately,
        instead of a job that fails a second later.
        """
        template: Template | None = None
        if isinstance(request, TemplateDeploymentRequest):
            # Allow-list lookup: the id never touches the filesystem directly.
            template = self._catalog.get(request.template_id)
            if template is None:
                raise UnknownTemplateError(request.template_id)
        elif self._translator is None:
            raise TranslatorUnavailableError

        project = request.project_name or self._generate_project_name(template)
        job = Job(
            id=uuid4(), mode=request.mode, project_name=project, request=request, template=template
        )
        await self._reserve(job, must_exist=False)

        first_step: tuple[PlanStep, StepHandler] = (
            (PlanStep(key="template_resolution", label="Template loading"), self._load_template)
            if template is not None
            else (PlanStep(key="ai_translation", label="AI analysis"), self._translate)
        )
        # Template and prompt paths only differ by their first step. Both MUST
        # go through security validation: templates are trusted, not exempt.
        return self._launch(
            job,
            [
                first_step,
                (PlanStep(key="security_validation", label="Security validation"), self._validate),
                (
                    PlanStep(key="workspace_setup", label="Workspace creation"),
                    self._write_workspace,
                ),
                (PlanStep(key="image_pull", label="Image download"), self._pull_images),
                (PlanStep(key="network_setup", label="Network provisioning"), self._create_stack),
                (PlanStep(key="container_deploy", label="Container startup"), self._start_stack),
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

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- Scheduling -------------------------------------------------------------

    async def _reserve(self, job: Job, *, must_exist: bool) -> None:
        if any(
            other.project_name == job.project_name and other.status in _ACTIVE_STATUSES
            for other in self._jobs.values()
        ):
            raise ProjectNameConflictError(job.project_name)
        # Register before awaiting, so a concurrent request for the same name
        # sees this job and gets a conflict.
        self._jobs[job.id] = job
        exists = await asyncio.to_thread(self._workspaces.exists, job.project_name)
        if exists != must_exist:
            del self._jobs[job.id]
            if must_exist:
                raise UnknownEnvironmentError(job.project_name)
            raise ProjectNameConflictError(job.project_name)

    def _launch(self, job: Job, plan: Plan) -> Job:
        # Open the channel before the task starts, so a client that connects
        # right after the 202 always finds it (and gets the full history).
        self._bus.open_channel(job.id)
        task = asyncio.create_task(self._run(job, plan), name=f"job-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, plan: Plan) -> None:
        job.status = JobStatus.RUNNING
        # The full plan is announced first: the UI can show remaining steps from t=0.
        self._bus.publish(
            job.id,
            EventType.JOB_ACCEPTED,
            f"{'Removal' if job.mode == 'removal' else 'Deployment'} of "
            f"'{job.project_name}' accepted ({job.mode})",
            plan=[step for step, _ in plan],
        )

        ctx: StepContext | None = None
        try:
            for index, (step, handler) in enumerate(plan, start=1):
                ctx = StepContext(job=job, step=step, index=index, total=len(plan), bus=self._bus)
                ctx.emit(EventType.STEP_STARTED, f"[{index}/{len(plan)}] {step.label}")
                await handler(ctx)
                ctx.emit(EventType.STEP_COMPLETED, f"[{index}/{len(plan)}] {step.label} done")
        except asyncio.CancelledError:
            job.status = JobStatus.FAILED
            self._publish_failure(job, ctx, "Interrupted: the server is shutting down.")
            raise
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
            done = "removed" if job.mode == "removal" else "ready"
            self._bus.publish(
                job.id,
                EventType.JOB_SUCCEEDED,
                f"Environment '{job.project_name}' is {done}"
                + (f" at {job.url}" if job.url else ""),
                url=job.url,
            )
        finally:
            # Keep the history for late viewers and reconnects, then free memory.
            asyncio.get_running_loop().call_later(
                self._settings.job_retention_seconds, self._forget, job.id
            )

    async def _fail(self, job: Job, ctx: StepContext | None, message: str) -> None:
        job.status = JobStatus.FAILED
        if ctx is not None and job.stack is not None and job.mode != "removal":
            await self._rollback(job.stack, ctx)
        self._publish_failure(job, ctx, message)

    async def _rollback(self, stack: StackHandle, ctx: StepContext) -> None:
        ctx.log("Rolling back: removing everything created for this project")
        try:
            await self._engine.remove(stack, ctx.log)
            await self._workspaces.remove(stack.project)
        except Exception:
            logger.exception("Rollback of %s failed", stack.project)
            ctx.log("Rollback incomplete: see server logs")

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

    # --- Step handlers ------------------------------------------------------------

    async def _load_template(self, ctx: StepContext) -> None:
        template = ctx.job.template
        if template is None:
            raise RuntimeError("template job without a resolved template")
        self._use_template(ctx, template)

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
        )

    async def _translate(self, ctx: StepContext) -> None:
        request = ctx.job.request
        if self._translator is None or not isinstance(request, PromptDeploymentRequest):
            raise RuntimeError("AI step scheduled without a translator or a prompt")

        ctx.log(f"Asking {self._translator.model} for a deployment plan")
        spec = await self._translator.translate(request.prompt)
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
        if not all(_SECRET_NAME_RE.match(name) for name in spec.secrets):
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
        )
        online = [name for name, s in compose.services.items() if "egress" in s.networks]
        ctx.log(
            "Network: isolated project network"
            + (f", Internet access for {', '.join(online)}" if online else ", no Internet access")
        )
        ctx.job.blueprint = blueprint

    async def _write_workspace(self, ctx: StepContext) -> None:
        blueprint = ctx.job.blueprint
        if blueprint is None:
            raise RuntimeError("workspace step without a validated blueprint")
        project = ctx.job.project_name
        domain = self._settings.public_domain
        document = render_compose(blueprint, project=project, domain=domain)
        variables = {
            "EC_PROJECT": project,
            "EC_HOSTNAME": web_hostname(project, domain),
            **WorkspaceManager.generate_secrets(blueprint.secrets),
        }
        workspace = await self._workspaces.create(project, document, variables)
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

    async def _pull_images(self, ctx: StepContext) -> None:
        await self._engine.pull(self._stack(ctx), ctx.log)

    async def _create_stack(self, ctx: StepContext) -> None:
        await self._engine.create(self._stack(ctx), ctx.log)

    async def _start_stack(self, ctx: StepContext) -> None:
        await self._engine.start(self._stack(ctx), ctx.log)
        blueprint = ctx.job.blueprint
        if blueprint is not None and blueprint.expose:
            hosts = web_hostnames(blueprint, ctx.job.project_name, self._settings.public_domain)
            for service, host in hosts.items():
                ctx.log(f"Web UI of {service}: http://{host}")
            ctx.job.url = f"http://{hosts[blueprint.expose[0].service]}"

    async def _teardown(self, ctx: StepContext) -> None:
        project = ctx.job.project_name
        workspace = await asyncio.to_thread(self._workspaces.get, project)
        if workspace is None:
            raise StepFailedError("This environment has no workspace to remove.")
        stack = StackHandle(
            project=project,
            compose_project=compose_project_name(project),
            compose_file=workspace.compose_file,
            edge_network=edge_network_name(project),
        )
        await self._engine.remove(stack, ctx.log)

    async def _remove_workspace(self, ctx: StepContext) -> None:
        await self._workspaces.remove(ctx.job.project_name)
        ctx.log(f"Workspace '{ctx.job.project_name}' deleted, including its secrets")

    @staticmethod
    def _stack(ctx: StepContext) -> StackHandle:
        if ctx.job.stack is None:
            raise RuntimeError("engine step without a workspace")
        return ctx.job.stack
