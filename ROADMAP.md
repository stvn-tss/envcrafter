# EnvCrafter roadmap

The goal: the simplest way to get an isolated, working environment on your own machine,
with as little friction as possible between "I need X" and "X is open in my browser, and I
know how to sign in".

This roadmap comes from a review of the product from a user's point of view. Items are
grouped by phase: each phase removes the biggest remaining source of friction. Phases are
ordered, the items inside a phase are not.

## Done

- [x] **Claude API key from the UI** — Settings → Claude API key: checked with the API,
  stored on the server with owner-only permissions, never sent back. No `.env` to edit.
- [x] **Useful without a key** — the request box finds the closest templates (accent- and
  language-tolerant) instead of being a disabled field at the top of the page.
- [x] **Every address and the sign-in notes on the dashboard** — all web UIs of an
  environment (the Media Stack has five), plus its template's default accounts and first
  steps, long after the deployment panel moved on.
- [x] **Failures that explain themselves** — the last log lines of the services that never
  became ready are kept in the failed step (secrets redacted) before the rollback deletes
  the containers.
- [x] **Retry** — a failed job starts again in one click, with the same request.
- [x] **Browser notifications** — opt-in, when a job ends while EnvCrafter is in the
  background.
- [x] **Healthchecks on every web UI** — added to Sonarr, Radarr, Prowlarr and qBittorrent,
  and enforced by the test suite for every template.
- [x] **Time zone** — each environment gets the browser's time zone as `${EC_TZ}` (Zabbix,
  Media Stack, Audiobookshelf use it).
- [x] **Light theme** — follows the system, or pinned in Settings (the UI was dark only).
- [x] **MIT license**.

## Phase 1 — Self-sufficient after deployment

Once an environment runs, everything needed to use it should be one click away.

- [ ] **Environment page** (drawer or page): every address, sign-in details, notes,
  services with their health, volumes and their size, CPU/RAM, logs, activity history, and
  all actions in one place.
- [ ] **Credentials you can reveal** — split secrets into *internal* (databases, never
  shown) and *user-facing* (admin accounts), declared in the manifest
  (`credentials: [{label, username, password_secret}]`) and revealed on demand, with an
  audit line. Pre-set admin passwords through environment variables where images allow it
  (Grafana, Nextcloud, Keycloak, pgAdmin, Paperless, Directus...), instead of
  `admin/password` defaults.
- [ ] **Credentials from logs** — declarative extraction (`credentials_from_logs: {service,
  pattern}`) for apps that print a first password (qBittorrent).
- [ ] **Template parameters** — typed inputs in the manifest (enum, string, boolean),
  rendered as a form before deploying: DVWA security level, GLPI language, admin e-mail...
- [ ] **Cancel a running deployment** (a 1 GB download cannot be stopped today), and a
  "keep on failure" option that skips the rollback for debugging.
- [ ] **Restart one service** instead of the whole environment.
- [ ] **Readable names** — `glpi-1` or `glpi-brave-otter` rather than `glpi-3f2a`; editable
  display title and free-text notes per environment.
- [ ] **Durable history** — jobs, plans and an audit log in SQLite (today: memory only, one
  hour for jobs, 15 minutes for plans, lost on restart).
- [ ] **First-run checklist** — Docker reachable, reverse proxy up, free RAM and disk,
  optional API key.
- [ ] **Capacity check before deploying** ("750 MB needed, 1.2 GB free") and an "images
  already downloaded" badge with a realistic time estimate.
- [ ] **Lighter removal for disposable environments** — simple confirmation with a few
  seconds to undo; typing the name stays for environments holding data.
- [ ] **French and English UI**.

## Phase 2 — A richer catalog and a more useful AI

- [ ] **Generic building blocks in the image allow-list** — PostgreSQL, Redis, Mailpit,
  Adminer, Nginx... With 12 images, custom AI stacks can only recombine the templates.
- [ ] **A richer AI output contract** — several web UIs (today exactly one), access notes
  and credentials for custom stacks, and a default healthcheck per allow-listed image,
  injected by the renderer when a stack has none.
- [ ] **Conversational refinement** ("add a database", "without Internet"), an editable plan
  (remove a service, turn Internet off, pick the exposed UI), and the nearest template when
  a request is unsupported.
- [ ] **AI diagnosis of a failure** from the redacted logs of the failed step.
- [ ] **New templates and categories** — see [Template ideas](#template-ideas).
- [ ] **Template tooling** — catalog hot reload, a `template lint` command, a JSON Schema for
  manifests (editor completion), Renovate for image bumps, Trivy scans and cosign
  verification in CI, a `platforms` field (arm64: Apple Silicon, Raspberry Pi) to hide what
  cannot run on the host.
- [ ] **Seed files and demo data** — bind mounts (`./...`) are resolved by the Docker daemon
  on the host, while the control plane keeps workspaces in a volume: templates cannot ship
  configuration files yet (Prometheus, Mosquitto 2.x need one).

## Phase 3 — Lifecycle

- [ ] **Reset** (labs back to their initial state), **clone**, **backup/restore** of volumes
  (downloadable archive), **export** (compose + `.env` to run it elsewhere).
- [ ] **Upgrades** — detect that the allow-list moved to a newer version, offer an upgrade
  job, warn about data migrations.
- [ ] **Expiry and auto-stop** — `expires_at` per environment, stop after inactivity.
- [ ] **Disk hygiene** — image usage and pruning of unused images.
- [ ] **Resource guardrails** — a default `mem_limit` derived from the footprint, `cpus` in
  the policy schema, global quotas, live CPU/RAM per environment.
- [ ] **Push instead of polling** — Docker events streamed to the dashboard instead of a
  `docker ps` every 5 seconds.
- [ ] **Linked environments** — an explicit link network between two projects (Zabbix
  monitoring GLPI, ZAP attacking DVWA); a vulnerable lab only links to other labs.

## Phase 4 — Beyond localhost

- [ ] **Authentication and roles** (forward-auth or OIDC): an admin approves images, users
  deploy, each environment has an owner and quotas.
- [ ] **LAN mode with HTTPS** — Jellyfin on the TV, Audiobookshelf on a phone: impossible
  while everything is bound to 127.0.0.1. Requires authentication first.
- [ ] **CLI and API tokens** (`envcrafter deploy glpi --name demo`), OpenAPI docs available in
  production.
- [ ] **Import an existing `docker-compose.yml`** — a readable policy report, automatic fixes
  (published ports → Traefik routes) and an image approval flow (resolve the digest, add it
  to the local allow-list).
- [ ] **One-command install** — images published on GHCR instead of `--build`, possibly a
  Docker Desktop extension.

## Template ideas

Existing categories:

| Category | Ideas |
|---|---|
| ITSM & Administration | Snipe-IT, NetBox, Uptime Kuma (very light), LibreNMS or Checkmk, BookStack, Keycloak, lldap |
| Multimedia | Navidrome, Kavita or Komga, Calibre-Web, Jellyseerr, Bazarr, Lidarr |
| Security & Lab | WebGoat, DVGA (GraphQL), crAPI (APIs), WrongSecrets, Mutillidae II, Vulhub scenarios (Log4Shell...), CTFd, CyberChef, ZAP (Webswing), an in-browser Kali workstation |

New categories:

| Category | Ideas |
|---|---|
| Development & databases | Gitea or Forgejo, code-server, PostgreSQL + pgAdmin, MariaDB + phpMyAdmin, MongoDB + mongo-express, Redis + RedisInsight, Mailpit, Redpanda + Console, RabbitMQ, Verdaccio |
| Observability | Prometheus + Grafana, Loki, Graylog |
| Productivity | Nextcloud, Paperless-ngx, Stirling-PDF, Excalidraw, HedgeDoc, Vikunja, Mattermost |
| Business & web | Odoo, Dolibarr, WordPress, Directus, Matomo |
| AI & data | Open WebUI + Ollama (CPU), Jupyter, Metabase, n8n, Qdrant |
| IoT | Node-RED + Mosquitto |

Scenario templates (several apps, wired together):

- **Pentest lab** — in-browser Kali + ZAP + DVWA + Juice Shop on one isolated network.
- **Observability** — Prometheus, Grafana and Loki with an instrumented demo app.
- **SSO playground** — Keycloak with Grafana and Nextcloud already configured for OIDC.
- **Complete ITSM** — GLPI + Zabbix.
- **Developer workspace** — Gitea + code-server + PostgreSQL + Mailpit.

Constraints to keep in mind:

- The policy refuses, on purpose: CI runners (they need the Docker socket), GPU and hardware
  transcoding (`devices`), DNS/SMTP/SNMP reachable from outside (no `ports`), VPNs
  (`NET_ADMIN`).
- Elasticsearch/OpenSearch, SonarQube and Wazuh need `vm.max_map_count` raised on the host:
  a sysctl the containers cannot set.
- Avoid Bitnami images: their free catalog was restricted in 2025.
