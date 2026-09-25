/**
 * Environments dashboard: every workspace with its live state (GET /api/environments),
 * refreshed every 5 s while the page is visible, with lifecycle actions.
 *
 * Rows are keyed by project and updated in place: a poll never destroys the button a
 * keyboard user is focused on.
 */
import { fetchEnvironments } from "./api.js";
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
  unknown: { label: "Unknown", tone: "idle" },
};
const JOB_LABELS = {
  template: "Deploying", prompt: "Deploying", plan: "Deploying",
  removal: "Removing", stop: "Stopping", start: "Starting", restart: "Restarting",
};
const CAN_STOP = new Set(["running", "starting", "degraded"]);
const CAN_START = new Set(["stopped", "degraded", "missing"]);

export class EnvironmentsPanel {
  #timer = 0;
  #loading = false;
  #environments = [];
  #rows = new Map(); // project -> row parts

  /**
   * @param {{ onAction: (project: string, action: string, environment: object) => void,
   *           onLogs: ((environment: object) => void) | null,
   *           onRemove: (target: { project: string, volumes: string[] | null }) => void,
   *           onFollow: (job: object, environment: object) => void,
   *           onUpdate: (environments: object[]) => void }} handlers
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
    if (this.#loading) return;
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
      if (document.visibilityState === "visible") this.#timer = window.setTimeout(() => this.refresh(), POLL_MS);
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
      link: el("a", { className: "environment-link", attrs: { target: "_blank", rel: "noopener noreferrer" } }),
      progress: button("View progress", (env) => this.handlers.onFollow(env.job, env), "button small primary"),
      logs: button("Logs", (env) => this.handlers.onLogs?.(env)),
      stop: button("Stop", (env) => this.handlers.onAction(env.project, "stop", env)),
      start: button("Start", (env) => this.handlers.onAction(env.project, "start", env)),
      restart: button("Restart", (env) => this.handlers.onAction(env.project, "restart", env)),
      remove: button("Remove", (env) => this.handlers.onRemove({ project: env.project, volumes: Array.isArray(env.volumes) ? env.volumes : null }), "button small danger"),
    };
    for (const [key, label] of [["logs", "Logs of"], ["stop", "Stop"], ["start", "Start"], ["restart", "Restart"], ["remove", "Remove"]]) {
      parts[key].setAttribute("aria-label", `${label} ${project}`);
    }
    parts.logs.hidden = !this.handlers.onLogs;
    parts.item = el("li", { className: "environment" }, [
      el("div", { className: "environment-head" }, [
        el("div", { className: "environment-title" }, [parts.title, el("code", { text: project })]),
        parts.state,
      ]),
      parts.meta,
      parts.link,
      el("div", { className: "environment-actions" }, [
        parts.progress, parts.logs, parts.stop, parts.start, parts.restart, parts.remove,
      ]),
    ]);
    this.#rows.set(project, parts);
    return parts;
  }

  #current(project) {
    return this.#environments.find((environment) => environment.project === project) ?? { project };
  }

  #updateRow(row, environment) {
    const busy = environment.job && typeof environment.job.job_id === "string";
    const state = STATES[environment.state] ?? STATES.unknown;
    row.item.dataset.state = environment.state ?? "unknown";
    row.title.textContent = typeof environment.title === "string" ? environment.title : environment.project;
    row.state.textContent = busy ? (JOB_LABELS[environment.job.mode] ?? "Working") : state.label;
    row.state.dataset.tone = busy ? "running" : state.tone;

    const services = Array.isArray(environment.services) ? environment.services : [];
    const origin = environment.origin === "template" ? "Template" : environment.origin === "prompt" ? "AI request" : "Environment";
    const created = environment.created_at ? new Date(environment.created_at) : null;
    row.meta.textContent = [
      origin,
      `${services.length} service${services.length === 1 ? "" : "s"}`,
      created && !Number.isNaN(created.getTime()) ? `created ${created.toLocaleString()}` : null,
    ].filter(Boolean).join(" · ");

    const main = Array.isArray(environment.urls) ? environment.urls[0] : null;
    const reachable = CAN_STOP.has(environment.state) && isEnvironmentUrl(main?.url);
    row.link.hidden = !reachable;
    if (reachable) {
      row.link.href = main.url;
      row.link.textContent = main.url;
    }

    row.progress.hidden = !busy;
    row.logs.disabled = Boolean(busy) || services.length === 0;
    row.stop.hidden = !CAN_STOP.has(environment.state);
    row.start.hidden = !CAN_START.has(environment.state);
    row.restart.hidden = !CAN_STOP.has(environment.state);
    for (const control of [row.stop, row.start, row.restart, row.remove]) control.disabled = Boolean(busy) || environment.state === "pending";
  }
}
