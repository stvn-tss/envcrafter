# EnvCrafter documentation

The technical side of EnvCrafter: how it is built and secured, how to configure it, its
API, how to add a template, and how to develop and test it. For an overview and the quick
start, see the [README](../README.md).

- [Architecture](#architecture)
- [Security model](#security-model)
- [Configuration](#configuration)
- [API](#api)
- [Adding a template](#adding-a-template)
- [Development](#development)
- [Project layout](#project-layout)

## Architecture

### Deployment pipeline

Every deployment, from a template or from an AI-generated plan, runs the same steps, shown
live in the UI:

1. **Template loading**, **AI analysis** (a request in plain English) or **Plan loading**
   (an AI plan reviewed beforehand).
2. **Security validation**: the stack is parsed with a hardened YAML loader and checked by
   `validate_compose()`, the policy described in [Security model](#security-model).
3. **Workspace creation**: the Compose file, generated secrets and metadata are written to
   the project's workspace.
4. **Image download**, with its progress.
5. **Network provisioning**: networks and containers are created.
6. **Container startup**, until the services are ready.

Turning a request into a plan to review (`POST /api/plans`) runs only the AI analysis and
the security validation: nothing is deployed until the plan is.

A failure at any step after the workspace is written rolls the whole project
back: containers, networks, volumes and workspace files are removed, so a
failed deployment never leaves anything behind, unless it was asked to keep
a failure for debugging (`keep_on_failure`). A cancelled deployment is always
rolled back.

### Control plane

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
  execute code in a container, copy files in or out of one (`docker cp`,
  which is also how Compose delivers `configs`: they are refused), build an
  image, or reach Swarm, system or plugin endpoints.
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
- **Template options are inert values** — a parameter chosen before
  deploying is checked against the options its manifest declares and written
  to the workspace `.env` from a tiny alphabet (no newline, `=`, `$`, quote or
  space), so it can never add a line or reference another variable.
- **Secrets are never echoed** — generated passwords live only in each
  project's workspace `.env` file; the API never returns them, and container
  logs have every generated secret value redacted before they leave the
  server.
- **The Claude API key is write-only** — a key entered in Settings is checked
  against the Claude API (a model lookup, no tokens spent), then stored in a
  settings file with owner-only permissions (`ENVCRAFTER_SETTINGS_FILE`, a
  dedicated volume in the control plane). The API only ever reports where the
  active key comes from and its last four characters. Saving or removing it is
  guarded like every other state-changing request (Origin check; JSON bodies only).
- **A history without secrets** — the SQLite history (job summaries, plans,
  audit log) holds messages written for users, never a secret value or the
  text of a request; its file is owner-only, next to the settings file.
- **Loopback by default** — the control plane's only published port is bound
  to `127.0.0.1`. Exposing EnvCrafter beyond localhost needs authentication
  in front of it, which this project does not provide (see [Current limitations](../README.md#current-limitations)).
- **Vulnerable lab images stay offline** — templates flagged as intentionally
  vulnerable (security-lab category) are never given Internet access; the
  policy enforces this regardless of what a template or plan requests.

## Configuration

Every setting is an environment variable (or a line in `.env`), prefixed
`ENVCRAFTER_`, with a sensible default — see `.env.example` for the full,
commented list. To use a `.env` file, copy the example first:

```bash
cp .env.example .env
```

The containerized control plane reads it too, but sets the engine, the Docker host, the
origins and the hosts itself. The Claude API key can be added from the UI
(**Settings → Claude API key**) or set as `ENVCRAFTER_LLM_API_KEY`: a key saved from the
UI takes precedence, and removing it falls back to the `.env` key.

The most relevant settings:

| Variable | Default | Meaning |
|---|---|---|
| `ENVCRAFTER_ENVIRONMENT` | `development` | `development` or `production` (affects API docs exposure). |
| `ENVCRAFTER_PUBLIC_DOMAIN` | `localhost` | Environments are served at `http://<project>.<public_domain>`. |
| `ENVCRAFTER_SERVE_FRONTEND` | `true` | Serve the static frontend from the API process. |
| `ENVCRAFTER_FRONTEND_DIR` | `<repository root>/frontend` | Where the static frontend is served from. |
| `ENVCRAFTER_TEMPLATES_DIR` | `<repository root>/templates` | Where the template catalog is loaded from. |
| `ENVCRAFTER_IMAGE_ALLOWLIST` | `backend/app/policy/image_allowlist.yaml` | Path to the image allow-list file. |
| `ENVCRAFTER_WORKSPACES_DIR` | `<repository root>/workspaces` | Where generated compose files, secrets and metadata are written. |
| `ENVCRAFTER_SETTINGS_FILE` | `<repository root>/data/settings.json` | Settings saved from the UI (the Claude API key), owner-only. |
| `ENVCRAFTER_HISTORY_FILE` | `<repository root>/data/history.sqlite3` | Job summaries, AI plans and the audit log (SQLite), owner-only. |
| `ENVCRAFTER_HISTORY_RETENTION_DAYS` | `90` | How long jobs and audit lines are kept in the history. |
| `ENVCRAFTER_TIMEZONE` | `Etc/UTC` | `${EC_TZ}` of an environment when the browser sends no known time zone. |
| `ENVCRAFTER_LLM_API_KEY` | *(empty)* | Claude API key, if not saved from the UI (a UI key takes precedence). Prompt mode and AI plans are disabled without any key. |
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
| `ENVCRAFTER_JOB_RETENTION_SECONDS` | `3600` | How long a finished job stays in memory (see [Current limitations](../README.md#current-limitations)). |
| `ENVCRAFTER_PLAN_TTL_SECONDS` | `900` | How long a reviewed AI plan can still be deployed. |
| `ENVCRAFTER_MAX_PLANS` | `32` | Plans kept in memory at once (oldest evicted first). |
| `ENVCRAFTER_INVENTORY_CACHE_SECONDS` | `2` | How long a `docker ps` result is reused across dashboard polls. |
| `ENVCRAFTER_READINESS_CACHE_SECONDS` | `2` | How long one Docker reading serves the setup check, the capacity check before deploying and the CPU/memory of an environment. |
| `ENVCRAFTER_HEALTH_POLL_SECONDS` | `5` | Health-watcher poll period during startup. |
| `ENVCRAFTER_PROGRESS_INTERVAL_SECONDS` | `0.5` | Minimum interval between `step.progress` events. |
| `ENVCRAFTER_LOG_TAIL_MAX` | `1000` | Largest `tail` accepted on the logs WebSocket. |
| `ENVCRAFTER_MAX_LOG_STREAMS` | `4` | Concurrent container log streams. |
| `ENVCRAFTER_SIMULATED_STEP_DELAY` | `0.4` | Delay between simulated engine steps (dev/tests only). |

The defaults above for `ENVCRAFTER_TEMPLATES_DIR`, `ENVCRAFTER_WORKSPACES_DIR`,

`ENVCRAFTER_SETTINGS_FILE`, `ENVCRAFTER_HISTORY_FILE` and `ENVCRAFTER_FRONTEND_DIR` apply to
the local dev server; the control-plane image sets its own values (`/app/templates`,
`/data/workspaces`, `/data/settings/settings.json`, `/data/settings/history.sqlite3`,
`/app/frontend`) as `ENV` in `backend/Dockerfile`, matching where it copies those
directories or mounts its volumes inside the container.

## API

### REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check. |
| GET | `/api/config` | Capabilities the UI adapts to: version, engine, LLM availability, public domain, project name pattern. |
| GET | `/api/templates` | Template catalog (categories + templates). |
| GET | `/api/templates/{template_id}/logo` | Template logo bytes, if any. |
| GET | `/api/templates/{template_id}/readiness` | What deploying the template needs here: images already downloaded, download left, memory needed and available, free disk space, warnings. |
| GET | `/api/system` | First-run checklist: Docker, the reverse proxy, free memory and disk, the AI key. |
| POST | `/api/jobs` | Start a deployment: `{"mode": "template", "template_id": ...}` (optionally with `parameters`, the template's options), `{"mode": "prompt", "prompt": ...}`, or `{"mode": "plan", "plan_id": ...}`, each with an optional `project_name` (else `<template>-<n>`), `timezone` (IANA name, e.g. `Europe/Paris`) and `keep_on_failure`. Returns `202` with `job_id` and `events_url`; `422` for an option the template does not offer. |
| GET | `/api/jobs` | List retained jobs (`?active=true` for queued/running only). |
| GET | `/api/jobs/{job_id}` | One job's current summary. |
| POST | `/api/jobs/{job_id}/cancel` | Stop a running deployment (rolled back) or AI analysis; the job ends with `job.cancelled`. Returns `202`; `409` for other jobs or jobs already over. |
| POST | `/api/plans` | Turn a natural-language prompt into a reviewable plan (`202` job; nothing is deployed). |
| GET | `/api/plans/{plan_id}` | The reviewable plan: components, images, network access, storage, secret count. |
| GET | `/api/environments` | Every environment with its live state. |
| GET | `/api/environments/{project}` | One environment's state and services. |
| PATCH | `/api/environments/{project}` | `{"title": ..., "notes": ...}` (one or both): change the display title or the notes. `409` while a job runs on it. |
| GET | `/api/environments/{project}/usage` | CPU and memory of each running service (one `docker stats` sample, shared for a few seconds). |
| GET | `/api/environments/{project}/activity` | Its jobs and edits since its latest deployment, newest first (`?limit=`, at most 200). |
| POST | `/api/environments/{project}/actions` | Lifecycle action: `{"action": "stop"\|"start"\|"restart"}`, or `{"action": "restart", "service": ...}` to restart one service. Returns `202`. |
| DELETE | `/api/environments/{project}` | Remove an environment (containers, networks, volumes, workspace). Returns `202`. |
| GET | `/api/settings` | Whether a Claude API key is configured, its source (`settings` or `environment`), its last four characters and the model. Never the key. |
| PUT | `/api/settings/llm-key` | `{"api_key": ...}`: check the key with the Claude API, then store and use it. `400` when the API refuses it, `502` when the API cannot be reached; nothing is stored then. |
| DELETE | `/api/settings/llm-key` | Forget the key saved from the UI (the `.env` key, if any, is used again). |

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
`step.failed`, `job.succeeded`, `job.failed`, `job.cancelled`. `job.failed`
carries `retryable` (whether the same request may succeed if sent again) and,
for a deployment kept for debugging, `kept`.

## Adding a template

1. Add every image the template needs to `backend/app/policy/image_allowlist.yaml`
   first: a canonical, pinned reference (tag or digest) and a description of
   its environment variables.
2. Write `compose.yaml` as a *source* stack: no `ports`, no `networks:` block,
   no container names, no Traefik labels. Services use `networks: [internal]`
   by default, or add `egress` if they need the Internet. Use named volumes,
   `${SECRET}` placeholders for generated secrets, `${EC_TZ}` for time zone
   variables (`TZ`, `PHP_TZ`...), and a healthcheck on every web service (the
   test suite enforces it: without one, a UI is reported ready before it answers).
3. Write `manifest.yaml`: every service under `components`, the web UIs under
   `expose` (the first one becomes `<project>.localhost`, the others
   `<service>.<project>.localhost`), the `secrets` to generate, `access_notes`
   (default credentials, first-run steps), and a required `footprint`
   (`download_mb`, `memory_mb`, `first_start_seconds`). An optional
   `logo: logo.png` or `logo.webp` (≤ 64 KiB) can sit next to the manifest —
   check the software's logo usage policy before publishing it. Start the file
   with `# yaml-language-server: $schema=../../manifest.schema.json` for
   completion and validation in editors. Secret names starting with `EC_` are
   reserved for built-in variables. Optional:
   - `parameters`: options chosen before deploying, written to the workspace
     `.env` and referenced as `${NAME}` in `compose.yaml` — `enum` (a list of
     `{value, label}` and a `default`) or `boolean` (written `true`/`false`);
   - `disposable: true` for a lab that holds nothing worth keeping: removing it
     asks for a simple confirmation instead of its name.
4. Check the catalog from `backend/`: `uv run python -m app.tools.template_lint`
   applies the startup rules template by template. The application itself
   refuses to start if a template breaks the security policy or its manifest
   disagrees with its compose file. Then run the end-to-end test for the new
   template.

Footprint figures are approximate, measured on `linux/amd64` with images
already cached; the end-to-end suite measures them on every run and warns
(without failing) when a template's manifest underestimates them.

## Development

Local development needs [uv](https://docs.astral.sh/uv/) and Python 3.12+.

### Development server

The development server runs EnvCrafter with the simulated engine that the test suite also
uses: no Docker needed, and no real container deployed. Real deployments run through the
containerized control plane.

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload
```

Then open http://localhost:8000. This mode also serves the frontend and runs
the whole pipeline except the actual Docker calls — handy for UI and backend
work without pulling images.

### Tests

From `backend/`:

```bash
uv run pytest                # unit and API tests (simulated engine)
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy app              # type checking (strict)
uv run python -m app.tools.template_lint  # template catalog + manifest schema
```

CI runs these checks on Linux and Windows for every push to `main` and every
pull request.

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
  `app/main.py` (app factory + lifespan). `app/tools/`: the template lint
  command.
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
    template catalog, the SQLite history, the readiness and usage readings.
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
- `data/`: settings saved from the UI (the Claude API key) and the history
  database. Git-ignored.
- `docs/`: this documentation and the screenshots of the README.
- `ROADMAP.md`: what comes next, and what is already done.
