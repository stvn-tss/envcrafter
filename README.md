# EnvCrafter

EnvCrafter is a self-hosted "personal PaaS": describe the environment you want
in plain English, or pick one from the built-in catalog, and EnvCrafter
validates the stack against a strict security policy, writes an isolated
Docker Compose workspace for it, and deploys it — streaming every step (image
downloads, health checks, failures) to a web UI in real time.

## Features

- **Template catalog** — six ready-to-deploy stacks across three categories
  (ITSM, multimedia, security labs), each with a description, footprint
  estimate (download size, memory, first-start time) and default credentials.
- **Natural-language requests with plan review** — describe what you need and
  Claude turns it into a stack (structured outputs only, no tool access, no
  code execution); you review the plan — components, images, network access,
  storage, secret count — before anything is deployed.
- **Security gate** — every stack, whether it comes from a template or from
  the AI, is parsed with a hardened YAML loader and passes the same
  `validate_compose()` policy: allow-listed images only, no privileged mode,
  no host mounts, no Docker socket access, no undeclared variables.
- **Isolated networks per project** — each environment gets its own internal
  Docker network; Internet access and public routing are opt-in, per service.
- **Live progress** — deployments stream step-by-step events: image pull
  percentage, container health, and a clear failure reason if something goes
  wrong, with automatic rollback.
- **Environments dashboard** — every deployed environment, its live state
  (running, starting, degraded, stopped...), and lifecycle actions: stop,
  start, restart, remove.
- **Container logs** — live log tail per service over a WebSocket, with
  generated secrets redacted before they ever reach the browser.
- **Simulated engine for development** — the whole pipeline runs without
  Docker for local development and the test suite; real deployments run
  through the containerized control plane.

## How it works

Every deployment — from a template or from an AI-generated plan — goes
through the same pipeline:

```
template lookup           ┐
      or          → security validation → workspace → image download
AI analysis        ┘              → networks & containers → startup
```

A failure at any step after the workspace is written rolls the whole project
back: containers, networks, volumes and workspace files are removed, so a
failed deployment never leaves anything behind.

The control plane is a small set of containers, each with the least Docker
access it needs:

```
                     127.0.0.1:80 (loopback only)
                            │
                        ┌───▼───┐
                        │Traefik│  (routes http://<project>.localhost to
                        └─┬───┬─┘   the right environment; joins only the
                          │   │     opt-in "edge" network of each project)
          read-only view  │   └──────────────► environment containers
          (list/inspect)  │
                    ┌─────▼──────┐        ┌──────────────┐
                    │socket-proxy│        │  socket-proxy │  containers, networks,
                    │  -traefik  │        │     -api      │  volumes, images,
                    └─────┬──────┘        └──────┬───────┘  start/stop, logs (never
                          │                       │           exec, build or host access)
                          └──────────┬────────────┘
                                     ▼
                              Docker socket
                                     ▲
                                     │ compose commands over the proxy
                              ┌──────┴──────┐
                              │  EnvCrafter │──── HTTPS ───► Claude API
                              │     API     │      (only if a key is configured)
                              └─────────────┘
```

Only the two socket proxies ever touch `/var/run/docker.sock`; the API itself
never sees the raw socket.

## Security model

- **Least-privilege Docker access** — the Docker socket is mounted only by two
  filtering proxies. Traefik's proxy can only list and inspect containers.
  The API's proxy can manage containers, networks, volumes and images,
  start/stop them and read their logs (for the logs viewer), but can never
  execute code in a container, build an image, or reach Swarm, system or
  plugin endpoints.
- **A security gate in front of every stack** — templates and AI-generated
  stacks alike are parsed with a hardened YAML loader (no aliases, no
  duplicate keys, size-capped) and validated against an allow-list schema
  that rejects unknown fields outright, then explicitly denies privileged
  containers, added capabilities, host device mappings, host namespaces,
  published ports, unusual `security_opt` values, custom volume drivers,
  reserved labels, any reference to the Docker socket, bind mounts outside
  the workspace, images that are not on the allow-list, and variables the
  stack never declared.
- **An image allow-list** — every image a template or the AI can use is
  pinned to an exact tag or digest and reviewed ahead of time; nothing is
  ever pulled by an unpinned or arbitrary reference.
- **Network isolation per project** — each environment gets its own internal
  network. Internet egress and public routing are separate, opt-in networks
  attached only to the services that need them; Traefik only reaches a
  project through its dedicated edge network and only serves the loopback
  port, so no environment can reach another project, or the API, through it.
- **No remote code execution through the AI** — the language model only ever
  fills in a typed, schema-validated stack description; its output is data,
  never code, a shell command, or a template to render. It never receives
  tools and cannot invoke `docker exec`.
- **Secrets are never echoed** — generated passwords live only in each
  project's workspace `.env` file; the API never returns them, and container
  logs have every generated secret value redacted before they leave the
  server.
- **Loopback by default** — the control plane's only published port is bound
  to `127.0.0.1`. Exposing EnvCrafter beyond localhost needs authentication
  in front of it, which this project does not provide (see Limitations).
- **Vulnerable lab images stay offline** — templates flagged as intentionally
  vulnerable (security-lab category) are never given Internet access; the
  policy enforces this regardless of what a template or plan requests.

## Quick start

Prerequisites: Docker Desktop or Docker Engine with Compose v2+, and, for
local development, [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
cp .env.example .env
# optionally set ENVCRAFTER_LLM_API_KEY for natural-language requests
```

**Control plane (real deployments):**

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Then open http://envcrafter.localhost.

**Development server (simulated engine, no Docker deployments):**

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

Then open http://localhost:8000. This mode also serves the frontend and runs
the whole pipeline except the actual Docker calls — handy for UI and backend
work without pulling images.

## Configuration

Every setting is an environment variable (or a line in `.env`), prefixed
`ENVCRAFTER_`, with a sensible default — see `.env.example` for the full,
commented list. The most relevant ones:

| Variable | Default | Meaning |
|---|---|---|
| `ENVCRAFTER_ENVIRONMENT` | `development` | `development` or `production` (affects API docs exposure). |
| `ENVCRAFTER_PUBLIC_DOMAIN` | `localhost` | Environments are served at `http://<project>.<public_domain>`. |
| `ENVCRAFTER_SERVE_FRONTEND` | `true` | Serve the static frontend from the API process. |
| `ENVCRAFTER_FRONTEND_DIR` | `<repository root>/frontend` | Where the static frontend is served from. |
| `ENVCRAFTER_TEMPLATES_DIR` | `<repository root>/templates` | Where the template catalog is loaded from. |
| `ENVCRAFTER_IMAGE_ALLOWLIST` | `backend/app/policy/image_allowlist.yaml` | Path to the image allow-list file. |
| `ENVCRAFTER_WORKSPACES_DIR` | `<repository root>/workspaces` | Where generated compose files, secrets and metadata are written. |
| `ENVCRAFTER_LLM_API_KEY` | *(empty)* | Claude API key. Prompt mode and AI plans are disabled without it. |
| `ENVCRAFTER_LLM_MODEL` | `claude-opus-5-5` | Model used to turn a request into a stack. |
| `ENVCRAFTER_LLM_EFFORT` | `high` | Reasoning effort: `low`, `medium`, `high`, `xhigh` or `max`. |
| `ENVCRAFTER_LLM_TIMEOUT_SECONDS` | `180` | Timeout for a translation request. |
| `ENVCRAFTER_ENGINE` | `simulated` | `simulated` (dev/tests) or `docker` (control plane). |
| `ENVCRAFTER_DOCKER_HOST` | `tcp://socket-proxy-api:2375` | Docker endpoint the API talks to (docker engine only). |
| `ENVCRAFTER_DOCKER_BINARY` | `docker` | Docker CLI binary name/path. |
| `ENVCRAFTER_TRAEFIK_CONTAINER` | `envcrafter-traefik` | Container name reattached to a project's edge network on start. |
| `ENVCRAFTER_PULL_TIMEOUT_SECONDS` | `1800` | Timeout for image downloads. |
| `ENVCRAFTER_START_TIMEOUT_SECONDS` | `600` | Timeout for `compose up --wait`. |
| `ENVCRAFTER_STOP_TIMEOUT_SECONDS` | `20` | Grace period for `compose stop`. |
| `ENVCRAFTER_ALLOWED_ORIGINS` | `["http://localhost:8000","http://127.0.0.1:8000"]` | Origins allowed to call the API or open a WebSocket (JSON array). |
| `ENVCRAFTER_ALLOWED_HOSTS` | `["localhost","127.0.0.1","*.localhost"]` | Accepted `Host` headers (JSON array). |
| `ENVCRAFTER_EVENT_HISTORY_SIZE` | `2000` | Events kept per job for replay. |
| `ENVCRAFTER_SUBSCRIBER_QUEUE_SIZE` | `500` | Per-subscriber event queue before it is considered lagged. |
| `ENVCRAFTER_JOB_RETENTION_SECONDS` | `3600` | How long a finished job stays in memory (see Limitations). |
| `ENVCRAFTER_PLAN_TTL_SECONDS` | `900` | How long a reviewed AI plan can still be deployed. |
| `ENVCRAFTER_MAX_PLANS` | `32` | Plans kept in memory at once (oldest evicted first). |
| `ENVCRAFTER_INVENTORY_CACHE_SECONDS` | `2` | How long a `docker ps` result is reused across dashboard polls. |
| `ENVCRAFTER_HEALTH_POLL_SECONDS` | `5` | Health-watcher poll period during startup. |
| `ENVCRAFTER_PROGRESS_INTERVAL_SECONDS` | `0.5` | Minimum interval between `step.progress` events. |
| `ENVCRAFTER_LOG_TAIL_MAX` | `1000` | Largest `tail` accepted on the logs WebSocket. |
| `ENVCRAFTER_MAX_LOG_STREAMS` | `4` | Concurrent container log streams. |
| `ENVCRAFTER_SIMULATED_STEP_DELAY` | `0.4` | Delay between simulated engine steps (dev/tests only). |

The defaults above for `ENVCRAFTER_TEMPLATES_DIR`, `ENVCRAFTER_WORKSPACES_DIR` and
`ENVCRAFTER_FRONTEND_DIR` apply to the local dev server; the control-plane image sets its own
values (`/app/templates`, `/data/workspaces`, `/app/frontend`) as `ENV` in `backend/Dockerfile`,
matching where it copies those directories inside the container.

## API overview

### REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check. |
| GET | `/api/config` | Capabilities the UI adapts to: version, engine, LLM availability, public domain, project name pattern. |
| GET | `/api/templates` | Template catalog (categories + templates). |
| GET | `/api/templates/{template_id}/logo` | Template logo bytes, if any. |
| POST | `/api/jobs` | Start a deployment: `{"mode": "template", "template_id": ...}`, `{"mode": "prompt", "prompt": ...}`, or `{"mode": "plan", "plan_id": ...}`. Returns `202` with `job_id` and `events_url`. |
| GET | `/api/jobs` | List retained jobs (`?active=true` for queued/running only). |
| GET | `/api/jobs/{job_id}` | One job's current summary. |
| POST | `/api/plans` | Turn a natural-language prompt into a reviewable plan (`202` job; nothing is deployed). |
| GET | `/api/plans/{plan_id}` | The reviewable plan: components, images, network access, storage, secret count. |
| GET | `/api/environments` | Every environment with its live state. |
| GET | `/api/environments/{project}` | One environment's state and services. |
| POST | `/api/environments/{project}/actions` | Lifecycle action: `{"action": "stop"\|"start"\|"restart"}`. Returns `202`. |
| DELETE | `/api/environments/{project}` | Remove an environment (containers, networks, volumes, workspace). Returns `202`. |

### WebSockets

| Endpoint | Purpose |
|---|---|
| `WS /ws/jobs/{job_id}?after_seq=N` | Job progress: replays events after sequence `N`, then streams live ones. |
| `WS /ws/environments/{project}/logs?service=<name>&tail=<n>` | Live container log lines for one service (secrets redacted). |

Close codes on both channels: `1000` normal end (do not reconnect), `1008`
policy violation — bad Origin, bad params, unexpected client data (do not
reconnect), `1013` try again — subscriber lagged or too many concurrent log
streams (reconnect), `4404` unknown or expired job/environment/service (do
not reconnect); any other close (e.g. `1006`) should be retried with backoff.

### Event types

`job.accepted`, `step.started`, `step.log`, `step.progress`, `step.completed`,
`step.failed`, `job.succeeded`, `job.failed`.

## Templates

| Template | Category | Components | Download | Memory | First start |
|---|---|---|---|---|---|
| GLPI | ITSM & Administration | GLPI, MariaDB | 454 MB | 400 MB | ~120 s |
| Zabbix | ITSM & Administration | Zabbix web, Zabbix server, MariaDB | 199 MB | 350 MB | ~90 s |
| Audiobookshelf | Multimedia | Audiobookshelf | 115 MB | 150 MB | ~20 s |
| Media Stack | Multimedia | Jellyfin, Sonarr, Radarr, Prowlarr, qBittorrent | 1048 MB | 750 MB | ~60 s |
| DVWA | Security & Lab | DVWA, MariaDB | 318 MB | 250 MB | ~45 s |
| OWASP Juice Shop | Security & Lab | OWASP Juice Shop | 114 MB | 250 MB | ~30 s |

Footprint figures are approximate, measured on `linux/amd64` with images
already cached; the end-to-end suite measures them on every run and warns
(without failing) when a template's manifest underestimates them.

### Adding a template

1. Add every image the template needs to `backend/app/policy/image_allowlist.yaml`
   first: a canonical, pinned reference (tag or digest) and a description of
   its environment variables.
2. Write `compose.yaml` as a *source* stack: no `ports`, no `networks:` block,
   no container names, no Traefik labels. Services use `networks: [internal]`
   by default, or add `egress` if they need the Internet. Use named volumes,
   `${SECRET}` placeholders for generated secrets, and a healthcheck on every
   web service.
3. Write `manifest.yaml`: every service under `components`, the web UIs under
   `expose` (the first one becomes `<project>.localhost`, the others
   `<service>.<project>.localhost`), the `secrets` to generate, `access_notes`
   (default credentials, first-run steps), and a required `footprint`
   (`download_mb`, `memory_mb`, `first_start_seconds`). An optional
   `logo: logo.png` or `logo.webp` (≤ 64 KiB) can sit next to the manifest —
   check the software's logo usage policy before publishing it.
4. The application refuses to start if a template breaks the security policy
   or its manifest disagrees with its compose file. Then run the end-to-end
   test for the new template.

## Testing

From `backend/`:

```bash
uv run pytest                # unit and API tests (simulated engine)
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy app              # type checking (strict)
```

End-to-end, against the real control plane (slow, pulls images):

```bash
ENVCRAFTER_E2E=1 uv run pytest tests/e2e -v
# also exercise the real AI flow (costs one request):
ENVCRAFTER_E2E_LLM=1 ENVCRAFTER_E2E=1 uv run pytest tests/e2e -v
```

Browser UI tests (Playwright + axe, opt-in), against the simulated engine:

```bash
ENVCRAFTER_UI=1 uv run pytest tests/ui --browser-channel msedge
# or, without a local Edge install:
uv run playwright install chromium
ENVCRAFTER_UI=1 uv run pytest tests/ui
```

## Project layout

- `backend/`: Python 3.12+, FastAPI, asyncio, Pydantic v2. Entry point:
  `app/main.py` (app factory + lifespan).
  - `app/api/`: REST routes, WebSocket streams, dependency providers.
  - `app/core/`: settings, request guards, security headers.
  - `app/models/`: typed contracts for requests, events, templates, plans,
    environments, capabilities.
  - `app/policy/`: the compose security gate, the image allow-list, the
    hardened YAML loader.
  - `app/translator/`: the Claude-backed translator and its output contract.
  - `app/workspace/`: the compose renderer (networks, labels, Traefik routes)
    and workspace files.
  - `app/engine/`: the real engine (via the socket proxy) and the simulated
    one used in development and tests.
  - `app/services/`: the deployment pipeline, the real-time event bus, the
    environment inventory, container log streaming, the plan store, the
    template catalog.
  - `tests/`: unit and API tests, `tests/e2e/` (opt-in, real control plane),
    `tests/ui/` (opt-in, Playwright).
- `frontend/`: static HTML/CSS and vanilla ES modules — no framework, no
  bundler, no npm.
- `templates/<category>/<id>/`: `manifest.yaml` (what users see) and
  `compose.yaml` (the source stack) for each template.
- `deploy/`: the control plane (`docker-compose.yml`: Traefik, two socket
  proxies, the API) and Traefik configuration.
- `workspaces/`: runtime output — generated compose files, secrets and
  metadata for each deployed environment. Git-ignored.

## Limitations

- The real-time event bus is in-process: run a single Uvicorn worker.
- Job history is kept in memory only, for about one hour; durable truth is
  always the workspace files and the Docker state, not the job log.
- Docker Desktop on Windows is supported through the containerized control
  plane; the local development server (`uv run uvicorn ...`) always runs the
  simulated engine and never deploys real containers.
- Designed for localhost use. There is no built-in authentication: do not
  expose EnvCrafter beyond `127.0.0.1` without putting an authenticating
  proxy in front of it.
- Linux containers only.
