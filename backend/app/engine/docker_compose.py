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
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.engine.base import (
    EngineError,
    HostResources,
    LogLine,
    LogSink,
    ProgressSink,
    RuntimeCheck,
    ServiceStatus,
    ServiceUsage,
    StackHandle,
)
from app.engine.host import read_host_resources
from app.engine.pull_progress import PullProgressTracker
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
# `docker ps` of one project's running containers, then one `docker stats` sample of them.
_USAGE_FORMAT = f'{{{{.ID}}}}\t{{{{.Label "{SERVICE_LABEL}"}}}}'
_STATS_FORMAT = "{{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}"
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]+)$")
# Docker prints binary units (MiB), sometimes decimal ones (MB): both, in MiB.
_MIB_PER_UNIT = {
    "B": 1 / 2**20,
    "KiB": 1 / 2**10,
    "MiB": 1.0,
    "GiB": 2.0**10,
    "TiB": 2.0**20,
    "kB": 1e3 / 2**20,
    "KB": 1e3 / 2**20,
    "MB": 1e6 / 2**20,
    "GB": 1e9 / 2**20,
    "TB": 1e12 / 2**20,
}
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s?(.*)$"
)
# C0 controls except tab and LF, DEL, and the Unicode bidi override/isolate
# characters: logs are untrusted text. This class is written with plain escape
# sequences only, on purpose: this source file must never carry a raw control
# or bidi character (verify with a Unicode category scan after editing).
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")


class DockerComposeEngine:
    def __init__(
        self,
        *,
        docker_binary: str,
        docker_host: str,
        traefik_container: str,
        pull_timeout: float,
        start_timeout: int,
        stop_timeout: int,
        disk_path: Path,
    ) -> None:
        self._docker = docker_binary
        self._docker_host = docker_host
        self._traefik = traefik_container
        self._pull_timeout = pull_timeout
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._disk_path = disk_path  # where workspaces live: on Docker's data disk

    async def pull(self, stack: StackHandle, log: LogSink, progress: ProgressSink) -> None:
        tracker = PullProgressTracker()
        last: tuple[int, str] | None = None

        def on_line(line: str) -> None:
            nonlocal last
            try:
                event = json.loads(line)
            except ValueError:
                log(line[:_MAX_LINE])  # Compose's own warnings are not JSON
                return
            for message in tracker.feed(event):
                log(message)
            snapshot = tracker.snapshot()
            if snapshot != last:
                last = snapshot
                progress(*snapshot)

        await self._run(
            self._compose(stack, "pull", progress_mode="json"),
            log,
            timeout=self._pull_timeout,
            on_line=on_line,
        )

    async def create(self, stack: StackHandle, log: LogSink) -> None:
        await self._run(self._compose(stack, "up", "--no-start"), log, timeout=120)
        if stack.edge_network:
            # Traefik only ever joins the edge network of each project, never
            # their internal network: it can reach the exposed UI and nothing else.
            await self._run(
                ["network", "connect", stack.edge_network, self._traefik], log, timeout=30
            )
            log(f"Reverse proxy attached to {stack.edge_network}")

    async def start(self, stack: StackHandle, log: LogSink, *, service: str | None = None) -> None:
        args = ["up", "--detach", "--wait", "--wait-timeout", str(self._start_timeout)]
        if service is not None:
            # One service of an existing environment: the others keep their state.
            args += ["--no-deps", _checked_service(service)]
        # The edge network may not exist yet: `docker network inspect` then fails, but
        # that must not fail the job, since `compose up` below (re)creates the network
        # (for instance after it was removed by an external `docker compose down`). Only
        # attach Traefik up front when the network already exists; otherwise attach it
        # right after `up`, once the network is guaranteed to be there. A restart of one
        # service checks it too: the control plane may have recreated Traefik meanwhile.
        attached = False
        if stack.edge_network:
            try:
                await self._ensure_routing(stack.edge_network, log)
                attached = True
            except EngineError:
                log(f"{stack.edge_network} not found yet; `docker compose up` will create it")
        await self._run(self._compose(stack, *args), log, timeout=self._start_timeout + 60)
        if stack.edge_network and not attached:
            await self._ensure_routing(stack.edge_network, log)

    async def stop(self, stack: StackHandle, log: LogSink, *, service: str | None = None) -> None:
        only = [_checked_service(service)] if service is not None else []
        await self._run(
            self._compose(stack, "stop", "--timeout", str(self._stop_timeout), *only),
            log,
            timeout=self._stop_timeout + 120,
        )

    async def _ensure_routing(self, network: str, log: LogSink) -> None:
        """Re-attach Traefik to a project's edge network if it lost it (for instance
        after the control plane recreated the Traefik container)."""
        members = await self._capture(
            ["network", "inspect", network, "--format", "{{range .Containers}}{{.Name}} {{end}}"],
            timeout=30,
        )
        if self._traefik in members.split():
            return
        await self._run(["network", "connect", network, self._traefik], log, timeout=30)
        log(f"Reverse proxy attached to {network}")

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

    async def recent_logs(self, stack: StackHandle, service: str, *, tail: int) -> list[LogLine]:
        output = await self._capture(
            self._compose(
                stack,
                "logs",
                "--no-color",
                "--no-log-prefix",
                "--timestamps",
                "--tail",
                str(tail),
                service,
            ),
            timeout=20,
        )
        lines = (parse_log_line(raw) for raw in output.splitlines())
        return [line for line in lines if line is not None]

    async def logs(
        self, stack: StackHandle, service: str, *, tail: int
    ) -> AsyncGenerator[LogLine, None]:
        process = await self._spawn(
            self._compose(
                stack,
                "logs",
                "--no-color",
                "--no-log-prefix",
                "--timestamps",
                "--tail",
                str(tail),
                "--follow",
                service,
            ),
            stdout=asyncio.subprocess.PIPE,
            # Kept apart from stdout: the compose CLI's own warnings and errors (upstream
            # messages, socket-proxy addresses, file paths) are for the server log only,
            # never for the browser. Drained concurrently so a chatty stderr can never
            # fill its pipe buffer and stall the container output this generator yields.
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        stderr_task = asyncio.create_task(_drain_stderr(process, stack.project, service))
        try:
            if process.stdout is not None:
                async for raw in process.stdout:
                    line = parse_log_line(raw.decode("utf-8", "replace"))
                    if line is not None:
                        yield line
            code = await process.wait()
            if code != 0:
                logger.warning(
                    "`docker compose logs --follow` for %s/%s exited with code %d",
                    stack.project,
                    service,
                    code,
                )
        finally:
            if process.returncode is None:  # the viewer left: stop following
                process.kill()
                await process.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    async def missing_images(self, images: Sequence[str]) -> list[str]:
        unique = list(dict.fromkeys(images))
        present = await asyncio.gather(*(self._image_present(image) for image in unique))
        return [image for image, found in zip(unique, present, strict=True) if not found]

    async def _image_present(self, image: str) -> bool:
        """True when the image is in the local store, False when Docker says it is not.
        Anything else (Docker unreachable, timeout) raises EngineError: "unknown" must never
        read as "missing", or a stopped Docker would announce a full download."""
        code, stderr = await self._exit_status(
            ["image", "inspect", "--format", "{{.Id}}", image], timeout=10
        )
        if code == 0:
            return True
        if "no such image" in stderr.lower():
            return False
        raise EngineError("Docker does not answer.")

    async def resources(self) -> HostResources:
        return await asyncio.to_thread(read_host_resources, self._disk_path)

    async def usage(self, stack: StackHandle) -> list[ServiceUsage]:
        if not _PROJECT_RE.fullmatch(stack.project):
            raise EngineError("Invalid project name.")
        listing = await self._capture(
            [
                "ps",
                "--filter",
                f"label={MANAGED_LABEL}=true",
                "--filter",
                f"label={PROJECT_LABEL}={stack.project}",
                "--format",
                _USAGE_FORMAT,
            ],
            timeout=20,
        )
        services = parse_usage_listing(listing)
        if not services:
            return []  # nothing runs: no sample to take
        # Container ids come from Docker's own output and are checked hexadecimal.
        output = await self._capture(
            ["stats", "--no-stream", "--format", _STATS_FORMAT, *services], timeout=30
        )
        return parse_stats_output(output, services)

    async def diagnose(self) -> list[RuntimeCheck]:
        try:
            version = await self._capture(
                ["version", "--format", "{{.Server.Version}}"], timeout=10
            )
        except EngineError:
            return [
                RuntimeCheck("docker", False, "Docker does not answer through the socket proxy."),
                RuntimeCheck("proxy", False, "Unknown while Docker does not answer."),
            ]
        version = _CONTROL_RE.sub("", version).strip()[:40]
        docker = RuntimeCheck("docker", True, f"Docker {version}" if version else "Docker answers.")
        try:
            state = await self._capture(
                ["container", "inspect", "--format", "{{.State.Status}}", self._traefik],
                timeout=10,
            )
        except EngineError:
            state = "missing"
        state = _CONTROL_RE.sub("", state).strip()[:20] or "missing"
        if state == "running":
            return [docker, RuntimeCheck("proxy", True, "The reverse proxy is running.")]
        return [docker, RuntimeCheck("proxy", False, f"The reverse proxy is {state}.")]

    # --- Internals --------------------------------------------------------------

    @staticmethod
    def _compose(stack: StackHandle, *args: str, progress_mode: str = "plain") -> list[str]:
        return [
            "compose",
            "--progress",
            progress_mode,
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

    async def _spawn(self, args: Sequence[str], **kwargs: Any) -> asyncio.subprocess.Process:
        """Start the docker CLI. A missing or broken binary is an EngineError like any other
        Docker failure, so diagnose() and the readiness checks report it."""
        try:
            return await asyncio.create_subprocess_exec(
                self._docker,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                env=self._environment(),
                **kwargs,
            )
        except OSError:
            logger.exception("Cannot start the docker CLI")
            raise EngineError("The docker CLI cannot be started.") from None

    async def _exit_status(self, args: list[str], *, timeout: float) -> tuple[int, str]:  # noqa: ASYNC109
        """Run a read-only docker command: (exit code, start of its stderr). Standard output
        is discarded; a timeout raises EngineError."""
        process = await self._spawn(
            args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            async with asyncio.timeout(timeout):
                _, stderr = await process.communicate()
        except TimeoutError:
            raise EngineError(f"`{_describe(args)}` timed out after {int(timeout)}s") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return process.returncode or 0, stderr[:2000].decode("utf-8", "replace")

    async def _capture(self, args: list[str], *, timeout: float) -> str:  # noqa: ASYNC109
        """Run a read-only docker command and return its standard output, truncated to
        1 MiB after capture (`communicate()` buffers the whole output first)."""
        process = await self._spawn(
            args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
        # Truncated, not capped: this bounds what is decoded and parsed, not what was read.
        return stdout[:_MAX_CAPTURE].decode("utf-8", "replace")

    async def _run(
        self,
        args: list[str],
        log: LogSink,
        *,
        timeout: float,  # noqa: ASYNC109
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        process = await self._spawn(
            args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024,
        )
        try:
            async with asyncio.timeout(timeout):
                if process.stdout is not None:
                    async for raw in process.stdout:
                        line = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
                        if not line:
                            continue
                        if on_line is not None:
                            on_line(line)
                        elif not _NOISE_RE.search(line):
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


def _checked_service(service: str) -> str:
    """Defense in depth: callers pass validated names, argv never gets anything else."""
    if not _SERVICE_RE.fullmatch(service):
        raise EngineError("Invalid service name.")
    return service


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


def parse_usage_listing(output: str) -> dict[str, str]:
    """`docker ps --format _USAGE_FORMAT` -> {container id: service}. Labels are data:
    a line that is not EnvCrafter-shaped is dropped."""
    services: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 2:
            continue
        container, service = parts
        if _CONTAINER_ID_RE.fullmatch(container) and _SERVICE_RE.fullmatch(service):
            services[container] = service
    return services


def parse_stats_output(output: str, services: dict[str, str]) -> list[ServiceUsage]:
    """`docker stats --format _STATS_FORMAT` lines ("<id>\t1.25%\t215MiB / 7.6GiB") of
    the listed containers, as usage per service sorted by service."""
    usage: list[ServiceUsage] = []
    for line in output.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) != 3 or parts[0] not in services:
            continue
        _, cpu, memory = parts
        usage.append(
            ServiceUsage(
                service=services[parts[0]],
                cpu_percent=_percent(cpu.strip()),
                memory_mb=_mebibytes(memory.split("/")[0].strip()),
            )
        )
    return sorted(usage, key=lambda item: item.service)


def _percent(value: str) -> float | None:
    match = _PERCENT_RE.fullmatch(value)
    return float(match.group(1)) if match else None


def _mebibytes(value: str) -> int | None:
    match = _SIZE_RE.fullmatch(value)
    if match is None or match.group(2) not in _MIB_PER_UNIT:
        return None
    return int(float(match.group(1)) * _MIB_PER_UNIT[match.group(2)])


def parse_log_line(text: str) -> LogLine | None:
    """One `docker compose logs --timestamps` line, without colors or control characters.

    Not length-capped here: the reader already bounds a line to 1 MiB (`logs()`'s
    `limit=`), and capping before `LogStreamer` redacts secret values could truncate a
    secret mid-match, leaving its surviving prefix unredacted in the browser. The
    display-facing cap is applied by `LogStreamer`, after redaction.
    """
    text = _CONTROL_RE.sub("", _ANSI_RE.sub("", text.rstrip("\r\n")))
    if not text.strip():
        return None
    timestamp: datetime | None = None
    match = _TIMESTAMP_RE.match(text)
    if match:
        timestamp = _parse_timestamp(match.group(1))
        text = match.group(2)
    return LogLine(timestamp=timestamp, text=text)


def _parse_timestamp(value: str) -> datetime | None:
    # Docker prints nanoseconds; datetime keeps microseconds (and Python 3.12 needs <= 6 digits).
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _drain_stderr(process: asyncio.subprocess.Process, project: str, service: str) -> None:
    """Log `docker compose logs --follow`'s own stderr server-side only: paths and
    upstream errors go to server logs, never to the browser. Runs concurrently with
    the caller reading stdout so a chatty stderr can never fill its pipe buffer and
    stall container output; cancelled from `logs()`'s `finally` once streaming stops.
    """
    if process.stderr is None:
        return
    async for raw in process.stderr:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")[:_MAX_LINE]
        if line:
            logger.warning("docker compose logs (%s/%s) stderr: %s", project, service, line)


_VERBS = (
    "pull",
    "up",
    "down",
    "stop",
    "logs",
    "ps",
    "stats",
    "connect",
    "disconnect",
    "inspect",
)


def _describe(args: list[str]) -> str:
    """Short, path-free command label for user-facing error messages."""
    verb = next((arg for arg in args if arg in _VERBS), args[0])
    group = {"compose": "compose ", "network": "network "}.get(args[0], "")
    return f"docker {group}{verb}"
