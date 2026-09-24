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
import os
import re

from app.engine.base import EngineError, LogSink, StackHandle

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Per-layer download chatter would flood the UI: keep only meaningful lines.
_NOISE_RE = re.compile(r"(Downloading|Extracting|Waiting|Verifying Checksum|Download complete)")
_MAX_LINE = 300


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


_VERBS = ("pull", "up", "down", "connect", "disconnect")


def _describe(args: list[str]) -> str:
    """Short, path-free command label for user-facing error messages."""
    verb = next((arg for arg in args if arg in _VERBS), args[0])
    return f"docker {'compose ' if args[0] == 'compose' else 'network '}{verb}"
