"""Docker engine driving the `docker compose` CLI through the filtering socket proxy.

Security properties:
* `create_subprocess_exec` with a fixed argv: no shell, and every argument is a
  validated slug, a path built by the workspace manager, or a setting.
* The child process gets a minimal environment (`DOCKER_HOST` = socket proxy):
  secrets held by this process, such as the LLM API key, are not inherited.
* Output is streamed line by line to the job's event log while the command runs,
  so long pulls show real progress in the UI.
"""

import asyncio
import logging
import os
import re
from collections.abc import Sequence

from app.engine.base import EngineError, LogSink, ServiceStatus, StackHandle
from app.models.common import PROJECT_NAME_PATTERN
from app.models.template import SERVICE_NAME_PATTERN
from app.workspace.renderer import MANAGED_LABEL, PROJECT_LABEL, SERVICE_LABEL

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Per-layer download chatter would flood the UI: keep only meaningful lines.
_NOISE_RE = re.compile(r"(Downloading|Extracting|Waiting|Verifying Checksum|Download complete)")
_MAX_LINE = 300
_MAX_CAPTURE = 1024 * 1024
_PROJECT_RE = re.compile(PROJECT_NAME_PATTERN)
_SERVICE_RE = re.compile(SERVICE_NAME_PATTERN)
_HEALTH_VALUES = frozenset({"healthy", "unhealthy", "starting"})
_STATUS_FORMAT = (
    f'{{{{.Label "{PROJECT_LABEL}"}}}}\t{{{{.Label "{SERVICE_LABEL}"}}}}'
    "\t{{.State}}\t{{.HealthStatus}}"
)


class DockerComposeEngine:
    def __init__(
        self,
        *,
        docker_binary: str,
        docker_host: str,
        traefik_container: str,
        pull_timeout: float,
        start_timeout: int,
    ) -> None:
        self._docker = docker_binary
        self._docker_host = docker_host
        self._traefik = traefik_container
        self._pull_timeout = pull_timeout
        self._start_timeout = start_timeout

    async def pull(self, stack: StackHandle, log: LogSink) -> None:
        await self._run(self._compose(stack, "pull"), log, timeout=self._pull_timeout)

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        await self._run(self._compose(stack, "up", "--no-start"), log, timeout=120)
        if stack.edge_network:
            # Traefik only ever joins the edge network of each project, never
            # their internal network: it can reach the exposed UI and nothing else.
            await self._run(
                ["network", "connect", stack.edge_network, self._traefik], log, timeout=30
            )
            log(f"Reverse proxy attached to {stack.edge_network}")

    async def start(self, stack: StackHandle, log: LogSink) -> None:
        await self._run(
            self._compose(
                stack, "up", "--detach", "--wait", "--wait-timeout", str(self._start_timeout)
            ),
            log,
            timeout=self._start_timeout + 60,
        )

    async def remove(self, stack: StackHandle, log: LogSink) -> None:
        if stack.edge_network:
            try:
                await self._run(
                    ["network", "disconnect", "--force", stack.edge_network, self._traefik],
                    log,
                    timeout=30,
                )
            except EngineError:
                log("Reverse proxy was not attached (nothing to detach)")
        await self._run(
            self._compose(stack, "down", "--volumes", "--remove-orphans", "--timeout", "20"),
            log,
            timeout=300,
        )

    async def status(self, stacks: Sequence[StackHandle]) -> dict[str, list[ServiceStatus]]:
        wanted = {stack.project for stack in stacks}
        if not wanted:
            return {}
        # One call for every project; only objects EnvCrafter labelled are listed.
        output = await self._capture(
            [
                "ps",
                "--all",
                "--filter",
                f"label={MANAGED_LABEL}=true",
                "--filter",
                f"label={PROJECT_LABEL}",
                "--format",
                _STATUS_FORMAT,
            ],
            timeout=20,
        )
        observed = parse_ps_output(output)
        return {project: observed.get(project, []) for project in wanted}

    # --- Internals --------------------------------------------------------------

    @staticmethod
    def _compose(stack: StackHandle, *args: str) -> list[str]:
        return [
            "compose",
            "--progress",
            "plain",
            "--project-name",
            stack.compose_project,
            "--project-directory",
            str(stack.compose_file.parent),
            "--file",
            str(stack.compose_file),
            *args,
        ]

    def _environment(self) -> dict[str, str]:
        env = {"DOCKER_HOST": self._docker_host}
        for key in ("PATH", "HOME", "DOCKER_CONFIG", "SYSTEMROOT", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    async def _capture(self, args: list[str], *, timeout: float) -> str:  # noqa: ASYNC109
        """Run a read-only docker command and return its (size-capped) standard output."""
        process = await asyncio.create_subprocess_exec(
            self._docker,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(),
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            raise EngineError(f"`{_describe(args)}` timed out after {int(timeout)}s") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode != 0:
            logger.warning(
                "%s failed: %s", _describe(args), stderr.decode("utf-8", "replace")[:500]
            )
            raise EngineError(f"`{_describe(args)}` failed (exit code {process.returncode})")
        return stdout[:_MAX_CAPTURE].decode("utf-8", "replace")

    async def _run(self, args: list[str], log: LogSink, *, timeout: float) -> None:  # noqa: ASYNC109
        process = await asyncio.create_subprocess_exec(
            self._docker,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._environment(),
            limit=1024 * 1024,
        )
        try:
            async with asyncio.timeout(timeout):
                if process.stdout is not None:
                    async for raw in process.stdout:
                        line = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
                        if line and not _NOISE_RE.search(line):
                            log(line[:_MAX_LINE])
                code = await process.wait()
        except TimeoutError:
            raise EngineError(f"`{_describe(args)}` timed out after {int(timeout)}s") from None
        finally:
            if process.returncode is None:  # timeout or task cancellation
                process.kill()
                await process.wait()
        if code != 0:
            raise EngineError(f"`{_describe(args)}` failed (exit code {code})")


def parse_ps_output(output: str) -> dict[str, list[ServiceStatus]]:
    """Parse `docker ps --format _STATUS_FORMAT`. Labels are data: anything that is not
    EnvCrafter-shaped is dropped instead of trusted."""
    result: dict[str, list[ServiceStatus]] = {}
    for line in output.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 4:
            continue
        project, service, state, health = parts
        if not (_PROJECT_RE.match(project) and _SERVICE_RE.match(service) and state.isalpha()):
            continue
        result.setdefault(project, []).append(
            ServiceStatus(
                service=service,
                state=state.lower(),
                health=health if health in _HEALTH_VALUES else None,
            )
        )
    return result


_VERBS = ("pull", "up", "down", "stop", "logs", "ps", "connect", "disconnect", "inspect")


def _describe(args: list[str]) -> str:
    """Short, path-free command label for user-facing error messages."""
    verb = next((arg for arg in args if arg in _VERBS), args[0])
    group = {"compose": "compose ", "network": "network "}.get(args[0], "")
    return f"docker {group}{verb}"
