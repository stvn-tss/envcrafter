/**
 * Environments dashboard: every workspace with its live state (GET /api/environments),
 * refreshed every 5 s while the page is visible, with lifecycle actions, every web address
 * and the sign-in notes of its template, long after the deployment panel moved on.
 *
 * Rows are keyed by project and updated in place: a poll never destroys the button a
 * keyboard user is focused on, nor closes the notes someone is reading. The rules that
 * turn a view into a badge and a set of actions are exported: the environment drawer
 * shows the same ones.
 */
import { fetchEnvironments } from "./api.js";
import { copyButton } from "./clipboard.js";
import { el } from "./dom.js";
import { isEnvironmentUrl } from "./environment.js";

const POLL_MS = 5000;

const STATES = {
  pending: { label: "Deploying", tone: "running" },
  running: { label: "Running", tone: "succeeded" },
  starting: { label: "Starting", tone: "running" },
  degraded: { label: "Degraded", tone: "warning" },
  stopped: { label: "Stopped", tone: "idle" },
  missing: { label: "Missing", tone: "failed" },
  failed: { label: "Failed", tone: "failed" }, // a failed deployment kept for debugging
  unknown: { label: "Unknown", tone: "idle" },
};
const JOB_LABELS = {
  template: "Deploying", prompt: "Deploying", plan: "Deploying",
  removal: "Removing", stop: "Stopping", start: "Starting", restart: "Restarting",
};
const CAN_STOP = new Set(["running", "starting", "degraded"]);
const SERVICE = /^[a-z][a-z0-9-]{0,39}$/;
const HEALTH = {
  healthy: { label: "Healthy", tone: "succeeded" },
  unhealthy: { label: "Unhealthy", tone: "failed" },
  starting: { label: "Starting", tone: "running" },
};

/** Label and tone of one service's container, from Docker's state and health. */
export function serviceState(service) {
  if (service.state === "running") return HEALTH[service.health] ?? { label: "Running", tone: "succeeded" };
  if (typeof service.state !== "string" || !service.state) return { label: "No container", tone: "idle" };
  const label = service.state.charAt(0).toUpperCase() + service.state.slice(1, 20);
  return { label, tone: service.state === "exited" ? "idle" : "warning" };
}
const CAN_START = new Set(["stopped", "degraded", "missing", "failed"]);
// A failed deployment kept for debugging: some services may run, and its addresses show
// what is being debugged.
const IN_PLACE = new Set([...CAN_STOP, "failed"]);
const actionControls = (row) => [row.stop, row.start, row.restart, row.remove];

/** Whether a job (deployment, stop, removal...) runs on this environment. */
export function isBusy(environment) {
  return Boolean(environment?.job && typeof environment.job.job_id === "string");
}

/** The badge of an environment: the job running on it, else its state. */
export function environmentBadge(environment) {
  const state = STATES[environment?.state] ?? STATES.unknown;
  if (!isBusy(environment)) return state;
  const label = environment.job.cancel_requested === true ? "Cancelling" : JOB_LABELS[environment.job.mode];
  return { label: label ?? "Working", tone: "running" };
}

/** Which actions fit the environment's state, a running job aside. */
export function availableActions(environment) {
  const state = environment?.state;
  return {
    stop: IN_PLACE.has(state),
    start: CAN_START.has(state),
    restart: CAN_STOP.has(state),
    restartService: IN_PLACE.has(state),
    links: IN_PLACE.has(state),
  };
}

/** "Template · 2 services · created ..." under the title. */
export function metaLine(environment) {
  const services = Array.isArray(environment?.services) ? environment.services : [];
  const origin = environment?.origin === "template" ? "Template" : environment?.origin === "prompt" ? "AI request" : "Environment";
  const created = environment?.created_at ? new Date(environment.created_at) : null;
  return [
    origin,
    `${services.length} service${services.length === 1 ? "" : "s"}`,
    created && !Number.isNaN(created.getTime()) ? `created ${created.toLocaleString()}` : null,
    environment?.failure ? "failed deployment, kept for debugging" : null,
  ].filter(Boolean).join(" · ");
}

/** Every web UI of the environment that EnvCrafter itself generated, main one first. */
export function webAddresses(environment) {
  return Array.isArray(environment?.urls)
    ? environment.urls.filter((item) => typeof item?.name === "string" && isEnvironmentUrl(item?.url))
    : [];
}

export class EnvironmentsPanel {
  #timer = 0;
  #loading = false;
  #again = false; // refresh() was called while a poll was in flight
  #environments = [];
  #rows = new Map(); // project -> row parts

  /**
   * @param {{ onAction: (project: string, action: string, environment: object) => void,
   *           onLogs: ((environment: object) => void) | null,
   *           onRemove: (target: { project: string, volumes: string[] | null }) => void,
   *           onFollow: (job: object, environment: object) => void,
   *           onRestartService: (environment: object, service: string) => void,
   *           onDetails: ((environment: object) => void) | null,
   *           onUpdate: (environments: object[]) => void,
   *           notesFor: (environment: object) => string[] }} handlers
   */
  constructor(root, handlers) {
    this.handlers = handlers;
    this.list = root.querySelector("#environment-list");
    this.empty = root.querySelector("#environments-empty");
    this.error = root.querySelector("#environments-error");
    this.count = root.querySelector("#environments-count");
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") this.refresh();
      else window.clearTimeout(this.#timer);
    });
  }

  get environments() {
    return this.#environments;
  }

  start() {
    this.refresh();
  }

  async refresh() {
    window.clearTimeout(this.#timer);
    if (this.#loading) {
      // That poll may have read the server before an action: run once more after it.
      this.#again = true;
      return;
    }
    this.#loading = true;
    try {
      const body = await fetchEnvironments();
      this.#environments = Array.isArray(body?.environments) ? body.environments : [];
      this.error.hidden = true;
      this.#render();
      this.handlers.onUpdate?.(this.#environments);
    } catch {
      this.error.hidden = false;
    } finally {
      this.#loading = false;
      if (this.#again) {
        this.#again = false;
        this.refresh();
      } else if (document.visibilityState === "visible") {
        this.#timer = window.setTimeout(() => this.refresh(), POLL_MS);
      }
    }
  }

  #render() {
    const seen = new Set();
    const ordered = [];
    for (const environment of this.#environments) {
      if (typeof environment?.project !== "string") continue;
      seen.add(environment.project);
      const row = this.#rows.get(environment.project) ?? this.#createRow(environment.project);
      this.#updateRow(row, environment);
      ordered.push(row.item);
    }
    for (const [project, row] of this.#rows) {
      if (!seen.has(project)) {
        row.item.remove();
        this.#rows.delete(project);
      }
    }
    // Re-insert only when the order changed: moving a node would drop keyboard focus.
    const current = [...this.list.children];
    if (current.length !== ordered.length || current.some((node, index) => node !== ordered[index])) {
      this.list.replaceChildren(...ordered);
    }
    this.empty.hidden = ordered.length > 0;
    this.count.hidden = ordered.length === 0;
    this.count.textContent = String(ordered.length);
  }

  #createRow(project) {
    const button = (text, onClick, className = "button small") => {
      const node = el("button", { className, text, attrs: { type: "button" } });
      node.addEventListener("click", () => onClick(this.#current(project)));
      return node;
    };
    const parts = {
      title: el("strong"),
      state: el("span", { className: "status-badge" }),
      meta: el("p", { className: "hint environment-meta" }),
      links: el("ul", { className: "environment-links", attrs: { "aria-label": `Web addresses of ${project}` } }),
      linksKey: "",
      notesList: el("ul", { className: "notes" }),
      notesKey: "",
      details: button("Details", (env) => this.handlers.onDetails?.(env)),
      progress: button("View progress", (env) => this.handlers.onFollow(env.job, env), "button small primary"),
      logs: button("Logs", (env) => this.handlers.onLogs?.(env)),
      stop: button("Stop", (env) => this.#act(project, "stop", env)),
      start: button("Start", (env) => this.#act(project, "start", env)),
      restart: button("Restart", (env) => this.#act(project, "restart", env)),
      remove: button("Remove", (env) => this.handlers.onRemove({
        project: env.project,
        volumes: Array.isArray(env.volumes) ? env.volumes : null,
        disposable: env.disposable === true,
      }), "button small danger"),
    };
    for (const [key, label] of [["details", "Details of"], ["progress", "View progress of"], ["logs", "Logs of"], ["stop", "Stop"], ["start", "Start"], ["restart", "Restart"], ["remove", "Remove"]]) {
      parts[key].setAttribute("aria-label", `${label} ${project}`);
    }
    parts.logs.hidden = !this.handlers.onLogs;
    parts.details.hidden = !this.handlers.onDetails;
    parts.servicesSummary = el("summary", { text: "Services" });
    parts.servicesList = el("ul", { className: "service-states" });
    parts.serviceRows = new Map(); // service -> { item, name, state, restart }
    parts.services = el("details", { className: "environment-services" }, [parts.servicesSummary, parts.servicesList]);
    parts.notes = el("details", { className: "environment-notes" }, [
      el("summary", { text: "Sign-in and notes" }),
      parts.notesList,
    ]);
    parts.item = el("li", { className: "environment" }, [
      el("div", { className: "environment-head" }, [
        el("div", { className: "environment-title" }, [parts.title, el("code", { text: project })]),
        parts.state,
      ]),
      parts.meta,
      parts.links,
      parts.services,
      parts.notes,
      el("div", { className: "environment-actions" }, [
        parts.details, parts.progress, parts.logs, parts.stop, parts.start, parts.restart, parts.remove,
      ]),
    ]);
    this.#rows.set(project, parts);
    return parts;
  }

  /** Disable the row's actions at once, so a second click cannot race the first one;
   *  the next render re-enables the ones that fit the new state. */
  #act(project, action, environment) {
    const row = this.#rows.get(project);
    if (row) for (const control of actionControls(row)) control.disabled = true;
    this.handlers.onAction(environment.project, action, environment);
  }

  #current(project) {
    return this.#environments.find((environment) => environment.project === project) ?? { project };
  }

  #updateRow(row, environment) {
    const busy = isBusy(environment);
    const badge = environmentBadge(environment);
    const actions = availableActions(environment);
    row.item.dataset.state = environment.state ?? "unknown";
    row.title.textContent = typeof environment.title === "string" ? environment.title : environment.project;
    row.state.textContent = badge.label;
    row.state.dataset.tone = badge.tone;

    const services = Array.isArray(environment.services) ? environment.services : [];
    row.meta.textContent = metaLine(environment);

    this.#renderLinks(row, environment);
    this.#renderServices(row, environment, busy);
    this.#renderNotes(row, environment);

    row.progress.hidden = !busy;
    row.logs.disabled = Boolean(busy) || services.length === 0;
    row.stop.hidden = !actions.stop;
    row.start.hidden = !actions.start;
    row.restart.hidden = !actions.restart;
    for (const control of actionControls(row)) control.disabled = Boolean(busy) || environment.state === "pending";
  }

  /** Every web UI while the environment runs, main one first. Rebuilt only on change. */
  #renderLinks(row, environment) {
    const urls = availableActions(environment).links ? webAddresses(environment) : [];
    const key = JSON.stringify(urls.map((item) => [item.name, item.url]));
    row.links.hidden = urls.length === 0;
    if (key === row.linksKey) return;
    row.linksKey = key;
    row.links.replaceChildren(...urls.map((item) => el("li", {}, [
      ...(urls.length > 1 ? [el("span", { className: "url-name", text: item.name })] : []),
      el("a", { text: item.url, attrs: { href: item.url, target: "_blank", rel: "noopener noreferrer" } }),
      copyButton(item.url, `Copy the address of ${item.name}`),
    ])));
  }

  /** Each service with its live state and a restart of that service alone. Updated in
   *  place, like the rows: a poll never destroys the button a keyboard user is on. */
  #renderServices(row, environment, busy) {
    const services = (Array.isArray(environment.services) ? environment.services : [])
      .filter((service) => SERVICE.test(service?.service ?? ""));
    row.services.hidden = services.length === 0;
    row.servicesSummary.textContent = `Services (${services.length})`;
    const canRestart = !busy && availableActions(environment).restartService;
    const seen = new Set();
    for (const service of services) {
      seen.add(service.service);
      let parts = row.serviceRows.get(service.service);
      if (!parts) {
        parts = this.#createServiceRow(environment.project, service.service);
        row.serviceRows.set(service.service, parts);
      }
      const name = typeof service.name === "string" && service.name ? service.name : service.service;
      const state = serviceState(service);
      parts.name.textContent = name;
      parts.state.textContent = state.label;
      parts.state.dataset.tone = state.tone;
      parts.restart.setAttribute("aria-label", `Restart ${name} in ${environment.project}`);
      parts.restart.disabled = !canRestart;
    }
    for (const [key, parts] of row.serviceRows) {
      if (seen.has(key)) continue;
      parts.item.remove();
      row.serviceRows.delete(key);
    }
    const ordered = services.map((service) => row.serviceRows.get(service.service).item);
    const current = [...row.servicesList.children];
    if (current.length !== ordered.length || current.some((node, index) => node !== ordered[index])) {
      row.servicesList.replaceChildren(...ordered);
    }
  }

  #createServiceRow(project, service) {
    const restart = el("button", { className: "text-button", text: "Restart", attrs: { type: "button" } });
    restart.addEventListener("click", () => {
      restart.disabled = true; // the next render re-enables it if the restart was refused
      this.handlers.onRestartService?.(this.#current(project), service);
    });
    const parts = {
      name: el("span", { className: "service-name" }),
      state: el("span", { className: "service-state" }),
      restart,
    };
    parts.item = el("li", {}, [parts.name, el("code", { text: service }), parts.state, restart]);
    return parts;
  }

  /** Default accounts and first steps from the template: still there hours later. */
  #renderNotes(row, environment) {
    const notes = (this.handlers.notesFor?.(environment) ?? []).filter((note) => typeof note === "string");
    const key = JSON.stringify(notes);
    row.notes.hidden = notes.length === 0;
    if (key === row.notesKey) return;
    row.notesKey = key;
    row.notesList.replaceChildren(...notes.map((note) => el("li", { text: note })));
  }
}
