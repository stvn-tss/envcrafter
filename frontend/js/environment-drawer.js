/**
 * Environment drawer: everything about one environment in one place. Its web addresses,
 * the template's sign-in notes, the user's own title and notes, the options chosen at
 * deployment, each service with its health, CPU and memory, its volumes, its activity and
 * every action. Opened from the dashboard and kept current by the dashboard's polls; CPU
 * and memory are read every few seconds while it is open.
 *
 * Server data is untrusted: text goes through el()/textContent, and only the links
 * EnvCrafter generates are rendered. The drawer is built once and updated in place, so a
 * poll never clears the notes being typed.
 */
import { copyButton } from "./clipboard.js";
import { section } from "./dialog-parts.js";
import { el } from "./dom.js";
import {
  availableActions,
  environmentBadge,
  isBusy,
  metaLine,
  serviceState,
  webAddresses,
} from "./environments.js";
import { icon } from "./icons.js";
import { formatSize } from "./readiness.js";

const USAGE_POLL_MS = 5000;
const SERVICE = /^[a-z][a-z0-9-]{0,39}$/;
const RUNNING_JOB = new Set(["queued", "running"]);

export class EnvironmentDrawer {
  #environment = null;
  #removed = false;
  #usage = new Map(); // service -> { cpu_percent, memory_mb }
  #usageAvailable = true;
  #usageTimer = 0;
  #usageRequest = 0;
  #activityRequest = 0;
  #activityJob = null; // the environment's job when the activity was last loaded
  #activityRunning = false; // the last activity still shows a running job
  #serviceRows = new Map();
  // What each part was last built from: an unchanged part is left alone, so a poll never
  // drops the text the reader is selecting (to copy the sign-in notes, say).
  #renderedFrom = new Map();

  /**
   * @param {HTMLDialogElement} dialog
   * @param {{ onAction: (environment: object, action: string) => void,
   *           onRestartService: (environment: object, service: string) => void,
   *           onLogs: (environment: object) => void,
   *           onRemove: (target: { project: string, volumes: string[] | null, disposable: boolean }) => void,
   *           onSave: (project: string, changes: { title?: string, notes?: string }) => Promise<string | null>,
   *           notesFor: (environment: object) => string[],
   *           templateFor: (environment: object) => object | undefined,
   *           fetchUsage: (project: string) => Promise<object>,
   *           fetchActivity: (project: string) => Promise<object> }} handlers
   */
  constructor(dialog, handlers) {
    this.dialog = dialog;
    this.handlers = handlers;
    this.heading = dialog.querySelector("#drawer-title");
    this.projectCode = dialog.querySelector("#drawer-project");
    this.badge = dialog.querySelector("#drawer-state");
    this.meta = dialog.querySelector("#drawer-meta");
    for (const button of dialog.querySelectorAll("[data-close]")) {
      button.addEventListener("click", () => dialog.close());
    }
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => this.#stop());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && dialog.open) this.#pollUsage();
    });
    this.#build(dialog.querySelector("#drawer-body"));
  }

  open(environment) {
    this.#environment = environment;
    this.#removed = false;
    this.#usage = new Map();
    this.#usageAvailable = true;
    this.#serviceRows = new Map();
    this.servicesList.replaceChildren();
    this.#renderedFrom.clear();
    this.titleInput.value = typeof environment.title === "string" ? environment.title : environment.project;
    this.notesInput.value = typeof environment.notes === "string" ? environment.notes : "";
    this.#setSaveStatus("");
    this.saveButton.disabled = false;
    this.activityList.replaceChildren(el("li", { className: "hint", text: "Loading…" }));
    this.#activityJob = environment.job?.job_id ?? null;
    this.#render();
    this.dialog.showModal();
    this.#loadActivity();
    this.#pollUsage();
  }

  /** The dashboard polled again: refresh everything but the fields being edited. */
  update(environments) {
    if (!this.dialog.open || !this.#environment || this.#removed) return;
    const current = environments.find((item) => item?.project === this.#environment.project);
    if (!current) {
      this.#markRemoved();
      return;
    }
    this.#environment = current;
    this.#render();
    const job = current.job?.job_id ?? null;
    // A job started or ended, or the activity still shows one running: read it again.
    if (job !== this.#activityJob || this.#activityRunning) {
      this.#activityJob = job;
      this.#loadActivity();
    }
  }

  // ---- Structure (built once) ----------------------------------------------------

  #build(body) {
    const action = (text, onClick, className = "button small") => {
      const node = el("button", { className, text, attrs: { type: "button" } });
      node.addEventListener("click", () => {
        if (this.#environment) onClick(this.#environment);
      });
      return node;
    };
    this.failure = el("p", { className: "callout drawer-failure", attrs: { hidden: "" } });
    this.openLink = el("a", {
      className: "button small primary",
      attrs: { target: "_blank", rel: "noopener noreferrer" },
    }, [el("span", { text: "Open" }), icon("external")]);
    this.actions = {
      logs: action("Logs", (environment) => this.handlers.onLogs(environment)),
      stop: action("Stop", (environment) => this.handlers.onAction(environment, "stop")),
      start: action("Start", (environment) => this.handlers.onAction(environment, "start")),
      restart: action("Restart", (environment) => this.handlers.onAction(environment, "restart")),
      remove: action("Remove", (environment) => {
        this.dialog.close();
        this.handlers.onRemove({
          project: environment.project,
          volumes: Array.isArray(environment.volumes) ? environment.volumes : null,
          disposable: environment.disposable === true,
        });
      }, "button small danger"),
    };

    this.linksList = el("ul", { className: "url-list" });
    this.linksSection = section("Web access", this.linksList);
    this.signInList = el("ul", { className: "notes" });
    this.signInSection = section("Sign-in", this.signInList);

    this.titleInput = el("input", {
      className: "input",
      attrs: { id: "drawer-title-input", maxlength: "80", autocomplete: "off", spellcheck: "false" },
    });
    this.notesInput = el("textarea", {
      className: "input",
      attrs: { id: "drawer-notes", maxlength: "2000", rows: "4", placeholder: "What this environment is for, what to remember…" },
    });
    this.saveButton = el("button", { className: "button", text: "Save", attrs: { type: "submit" } });
    this.saveStatus = el("p", { className: "hint save-status", attrs: { role: "status" } });
    const form = el("form", { className: "drawer-form" }, [
      el("div", { className: "field wide" }, [
        el("label", { text: "Title", attrs: { for: "drawer-title-input" } }),
        this.titleInput,
      ]),
      el("div", { className: "field wide" }, [
        el("label", { text: "Notes", attrs: { for: "drawer-notes" } }),
        this.notesInput,
      ]),
      el("div", { className: "drawer-form-actions" }, [this.saveStatus, this.saveButton]),
    ]);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.#save();
    });

    this.optionsList = el("ul", { className: "option-list" });
    this.optionsSection = section("Options", this.optionsList);
    this.servicesList = el("ul", { className: "service-states drawer-services" });
    this.usageNote = el("p", { className: "hint usage-note", attrs: { hidden: "" } });
    this.storage = el("div");
    this.activityList = el("ol", { className: "activity-list", attrs: { "aria-label": "Activity" } });

    body.replaceChildren(
      this.failure,
      el("div", { className: "result-actions drawer-actions" }, [
        this.openLink,
        this.actions.logs,
        this.actions.stop,
        this.actions.start,
        this.actions.restart,
        this.actions.remove,
      ]),
      this.linksSection,
      this.signInSection,
      section("Title and notes", form),
      this.optionsSection,
      section("Services", el("div", {}, [this.servicesList, this.usageNote])),
      section("Storage", this.storage),
      section("Activity", this.activityList),
    );
  }

  // ---- Rendering -------------------------------------------------------------------

  #render() {
    const environment = this.#environment;
    const badge = this.#removed ? { label: "Removed", tone: "idle" } : environmentBadge(environment);
    this.heading.textContent = typeof environment.title === "string" ? environment.title : environment.project;
    this.projectCode.textContent = environment.project;
    this.badge.textContent = badge.label;
    this.badge.dataset.tone = badge.tone;
    this.meta.textContent = metaLine(environment);

    const failure = environment.failure;
    this.failure.hidden = typeof failure?.message !== "string";
    if (!this.failure.hidden && this.#changed("failure", failure.message)) {
      this.failure.replaceChildren(icon("alert"), el("span", {
        text: `The deployment failed: ${failure.message} It was kept for debugging: read its logs, then remove it.`,
      }));
    }

    this.#renderActions(environment);
    this.#renderLinks(environment);
    const notes = (this.handlers.notesFor(environment) ?? []).filter((note) => typeof note === "string");
    this.signInSection.hidden = notes.length === 0;
    if (this.#changed("signIn", notes)) {
      this.signInList.replaceChildren(...notes.map((note) => el("li", { text: note })));
    }
    this.#renderOptions(environment);
    this.#renderServices();
    this.#renderStorage(environment);
  }

  /** True when `part` must be built again: what it shows differs from the last build. */
  #changed(part, source) {
    const key = JSON.stringify(source);
    if (this.#renderedFrom.get(part) === key) return false;
    this.#renderedFrom.set(part, key);
    return true;
  }

  #renderActions(environment) {
    const busy = isBusy(environment);
    const actions = availableActions(environment);
    const main = webAddresses(environment)[0];
    this.openLink.hidden = this.#removed || !actions.links || !main;
    if (!this.openLink.hidden) this.openLink.setAttribute("href", main.url);
    this.actions.stop.hidden = !actions.stop;
    this.actions.start.hidden = !actions.start;
    this.actions.restart.hidden = !actions.restart;
    const blocked = busy || this.#removed || environment.state === "pending";
    for (const control of Object.values(this.actions)) control.disabled = blocked;
    const services = Array.isArray(environment.services) ? environment.services : [];
    this.actions.logs.disabled = blocked || services.length === 0;
  }

  #renderLinks(environment) {
    const urls = availableActions(environment).links ? webAddresses(environment) : [];
    this.linksSection.hidden = urls.length === 0 || this.#removed;
    if (!this.#changed("links", urls.map((item) => [item.name, item.url]))) return;
    this.linksList.replaceChildren(...urls.map((item) => el("li", {}, [
      el("span", { className: "url-name", text: item.name }),
      el("a", { text: item.url, attrs: { href: item.url, target: "_blank", rel: "noopener noreferrer" } }),
      copyButton(item.url, `Copy the address of ${item.name}`),
    ])));
  }

  /** The options chosen at deployment, named as the template names them. */
  #renderOptions(environment) {
    const chosen = environment.parameters && typeof environment.parameters === "object" ? environment.parameters : {};
    const definitions = new Map(
      (this.handlers.templateFor(environment)?.parameters ?? []).map((parameter) => [parameter.name, parameter]),
    );
    const rows = Object.entries(chosen)
      .filter(([, value]) => typeof value === "string")
      .map(([name, value]) => {
        const definition = definitions.get(name);
        let shown = value;
        if (definition?.type === "boolean") shown = value === "true" ? "Yes" : "No";
        else if (Array.isArray(definition?.options)) {
          shown = definition.options.find((option) => option.value === value)?.label ?? value;
        }
        return [definition?.label ?? name, shown];
      });
    this.optionsSection.hidden = rows.length === 0;
    if (!this.#changed("options", rows)) return;
    this.optionsList.replaceChildren(...rows.map(([label, shown]) => el("li", {}, [
      el("span", { className: "option-name", text: label }),
      el("span", { text: shown }),
    ])));
  }

  /** Each service with its state, CPU and memory, and a restart of that service alone. */
  #renderServices() {
    const environment = this.#environment;
    const services = (Array.isArray(environment.services) ? environment.services : [])
      .filter((service) => SERVICE.test(service?.service ?? ""));
    const canRestart = !this.#removed && !isBusy(environment) && availableActions(environment).restartService;
    const seen = new Set();
    for (const service of services) {
      seen.add(service.service);
      let parts = this.#serviceRows.get(service.service);
      if (!parts) {
        parts = this.#createServiceRow(service.service);
        this.#serviceRows.set(service.service, parts);
      }
      const name = typeof service.name === "string" && service.name ? service.name : service.service;
      const state = serviceState(service);
      parts.name.textContent = name;
      parts.state.textContent = state.label;
      parts.state.dataset.tone = state.tone;
      parts.usage.textContent = this.#usageText(service.service);
      parts.restart.setAttribute("aria-label", `Restart ${name} in ${environment.project}`);
      parts.restart.disabled = !canRestart;
    }
    for (const [key, parts] of this.#serviceRows) {
      if (seen.has(key)) continue;
      parts.item.remove();
      this.#serviceRows.delete(key);
    }
    const ordered = services.map((service) => this.#serviceRows.get(service.service).item);
    const current = [...this.servicesList.children];
    if (current.length !== ordered.length || current.some((node, index) => node !== ordered[index])) {
      this.servicesList.replaceChildren(...ordered);
    }
    this.usageNote.hidden = this.#usageAvailable || this.#removed;
    this.usageNote.textContent = "CPU and memory are unavailable: Docker does not answer.";
  }

  #createServiceRow(service) {
    const restart = el("button", { className: "text-button", text: "Restart", attrs: { type: "button" } });
    restart.addEventListener("click", () => {
      restart.disabled = true; // the next update re-enables it if the restart was refused
      if (this.#environment) this.handlers.onRestartService(this.#environment, service);
    });
    const parts = {
      name: el("span", { className: "service-name" }),
      state: el("span", { className: "service-state" }),
      usage: el("span", { className: "usage" }),
      restart,
    };
    parts.item = el("li", {}, [parts.name, el("code", { text: service }), parts.state, parts.usage, restart]);
    return parts;
  }

  #usageText(service) {
    const usage = this.#usage.get(service);
    if (!usage) return "";
    const cpu = typeof usage.cpu_percent === "number" && Number.isFinite(usage.cpu_percent) ? `${usage.cpu_percent.toFixed(1)}% CPU` : null;
    const memory = Number.isInteger(usage.memory_mb) && usage.memory_mb >= 0 ? formatSize(usage.memory_mb) : null;
    return [cpu, memory].filter(Boolean).join(" · ");
  }

  #renderStorage(environment) {
    const volumes = (Array.isArray(environment.volumes) ? environment.volumes : [])
      .filter((volume) => typeof volume === "string");
    if (!this.#changed("storage", volumes)) return;
    this.storage.replaceChildren(...(volumes.length
      ? [
          el("ul", { className: "inline-list mono" }, volumes.map((volume) => el("li", { text: volume }))),
          el("p", { className: "hint access-note", text: "Deleted with the environment." }),
        ]
      : [el("p", { className: "hint", text: "No persistent data: everything resets when it is removed." })]));
  }

  #markRemoved() {
    this.#removed = true;
    this.#stop();
    this.#render();
    this.saveButton.disabled = true;
    this.#setSaveStatus("This environment was removed.");
  }

  // ---- Data --------------------------------------------------------------------------

  async #save() {
    const environment = this.#environment;
    if (!environment || this.#removed) return;
    const title = this.titleInput.value.trim();
    const notes = this.notesInput.value;
    if (!title) {
      this.#setSaveStatus("The title cannot be empty.", true);
      return;
    }
    // Only what changed: the activity then says exactly what was edited.
    const changes = {};
    if (title !== environment.title) changes.title = title;
    if (notes !== (environment.notes ?? "")) changes.notes = notes;
    if (!Object.keys(changes).length) {
      this.#setSaveStatus("Nothing to save.");
      return;
    }
    this.saveButton.disabled = true;
    this.#setSaveStatus("Saving…");
    const error = await this.handlers.onSave(environment.project, changes);
    this.saveButton.disabled = this.#removed;
    if (error) {
      this.#setSaveStatus(error, true);
      return;
    }
    this.#environment = { ...this.#environment, ...changes };
    this.heading.textContent = this.#environment.title;
    this.#setSaveStatus("Saved");
    this.#loadActivity();
  }

  #setSaveStatus(text, invalid = false) {
    this.saveStatus.textContent = text;
    this.saveStatus.toggleAttribute("data-invalid", invalid);
  }

  async #loadActivity() {
    const project = this.#environment?.project;
    if (!project) return;
    const request = ++this.#activityRequest;
    let body = null;
    try {
      body = await this.handlers.fetchActivity(project);
    } catch {
      body = null;
    }
    if (request !== this.#activityRequest || this.#environment?.project !== project) return;
    this.#renderActivity(body);
  }

  #renderActivity(body) {
    const entries = Array.isArray(body?.entries) ? body.entries.filter((entry) => typeof entry?.message === "string") : [];
    this.#activityRunning = entries.some((entry) => entry.kind === "job" && RUNNING_JOB.has(entry.status));
    if (!body) {
      this.activityList.replaceChildren(el("li", { className: "hint", text: "The activity could not be loaded." }));
    } else if (body.available === false) {
      this.activityList.replaceChildren(el("li", { className: "hint", text: "The history is off: see the server logs." }));
    } else if (!entries.length) {
      this.activityList.replaceChildren(el("li", { className: "hint", text: "No activity recorded yet." }));
    } else {
      this.activityList.replaceChildren(...entries.map((entry) => {
        const at = new Date(entry.at);
        return el("li", { attrs: { "data-status": typeof entry.status === "string" ? entry.status : "audit" } }, [
          el("time", {
            text: Number.isNaN(at.getTime()) ? "" : at.toLocaleString(),
            attrs: { datetime: String(entry.at) },
          }),
          el("span", { text: entry.message }),
        ]);
      }));
    }
  }

  #pollUsage() {
    window.clearTimeout(this.#usageTimer);
    this.#loadUsage().finally(() => {
      if (this.dialog.open && !this.#removed && document.visibilityState === "visible") {
        this.#usageTimer = window.setTimeout(() => this.#pollUsage(), USAGE_POLL_MS);
      }
    });
  }

  async #loadUsage() {
    const project = this.#environment?.project;
    if (!project || this.#removed) return;
    const request = ++this.#usageRequest;
    let body = null;
    try {
      body = await this.handlers.fetchUsage(project);
    } catch {
      body = null;
    }
    if (request !== this.#usageRequest || this.#environment?.project !== project || !this.dialog.open) return;
    this.#usageAvailable = body?.available === true;
    const services = Array.isArray(body?.services) ? body.services : [];
    this.#usage = new Map(
      services.filter((item) => SERVICE.test(item?.service ?? "")).map((item) => [item.service, item]),
    );
    this.#renderServices();
  }

  /** Stop reading once the drawer closes; late answers of pending reads are dropped. */
  #stop() {
    window.clearTimeout(this.#usageTimer);
    this.#usageRequest += 1;
    this.#activityRequest += 1;
  }
}
