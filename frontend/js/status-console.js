/**
 * Renders a job's event stream: status badge, progress, final outcome (web
 * addresses, sign-in notes, actions) and a step list whose rows expand to show
 * each step's logs.
 *
 * The view is a projection of the events it receives, timestamps included, so a
 * replay after reconnect rebuilds exactly the same view. The only timer refreshes
 * the "elapsed" labels while a job runs.
 */
import { copyButton, copyText } from "./clipboard.js";
import { el } from "./dom.js";
import { ENVIRONMENT_URL, isMainHost, webUrl, withProject } from "./environment.js";
import { icon } from "./icons.js";
import { showToast } from "./toasts.js";

const MAX_STEP_LOG_ENTRIES = 200; // bounded DOM: long deployments must not bloat the page
const MAX_COPY_LINES = 2000;
const FOLLOW_THRESHOLD_PX = 24;
const COPY_CONFIRMATION_MS = 2000;

export const STATUS = {
  idle: { label: "Idle", tone: "idle" },
  deploying: { label: "Deploying", tone: "running" },
  removing: { label: "Removing", tone: "running" },
  ready: { label: "Ready", tone: "succeeded" },
  removed: { label: "Removed", tone: "succeeded" },
  failed: { label: "Failed", tone: "failed" },
};

// Connection problems are the only transport states worth showing.
const CONNECTION_NOTES = {
  reconnecting: { text: "Connection lost. Reconnecting…", retry: false },
  failed: { text: "Live updates stopped.", retry: true },
  rejected: { text: "Live updates are no longer available for this job.", retry: false },
};

/** 850 -> "0.9s" (precise), 42_000 -> "42s", 125_000 -> "2m 05s". */
export function formatDuration(ms, precise = false) {
  const seconds = Math.max(0, ms) / 1000;
  if (precise && seconds < 10) return `${seconds.toFixed(1)}s`;
  const total = Math.floor(seconds);
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

function parseTime(value) {
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? null : time;
}

function isAtBottom(list) {
  return list.scrollHeight - list.scrollTop - list.clientHeight < FOLLOW_THRESHOLD_PX;
}

export class StatusConsole {
  #job = null; // JobSummary returned by the API
  #context = {}; // { title, template } from the caller
  #steps = new Map(); // step_key -> step view
  #status = "idle";
  #startedAt = null;
  #endedAt = null;
  #copyLines = [];
  #timer = 0;

  /**
   * @param {{ onRemove: (target: { project: string, volumes: string[] | null }) => void,
   *           onReconnect: () => void, onChange: (summary: object) => void }} actions
   */
  constructor(root, { onRemove, onReconnect, onChange }) {
    this.onRemove = onRemove;
    this.onChange = onChange;
    this.badge = root.querySelector("#status-badge");
    this.connectionNote = root.querySelector("#connection-note");
    this.connectionText = root.querySelector("#connection-text");
    this.retryButton = root.querySelector("#connection-retry");
    this.empty = root.querySelector("#job-empty");
    this.view = root.querySelector("#job-view");
    this.title = root.querySelector("#job-title");
    this.elapsed = root.querySelector("#job-elapsed");
    this.progress = root.querySelector("#job-progress");
    this.progressBar = root.querySelector("#job-progress-bar");
    this.progressLabel = root.querySelector("#job-progress-label");
    this.result = root.querySelector("#job-result");
    this.stepList = root.querySelector("#step-list");
    this.copyLogsButton = root.querySelector("#copy-logs");
    this.announcer = root.querySelector("#status-announcer");

    this.retryButton.addEventListener("click", () => onReconnect());
    this.copyLogsButton.addEventListener("click", () => this.#copyLogs());
  }

  get summary() {
    const counts = this.#counts();
    return {
      status: this.#status,
      project: this.#job?.project_name ?? null,
      done: counts.done,
      total: this.#steps.size,
    };
  }

  /** @param {{ title?: string, template?: object }} context */
  start(job, context = {}) {
    this.#job = job;
    this.#context = context;
    this.#steps.clear();
    this.#copyLines = [];
    this.#startedAt = null;
    this.#endedAt = null;
    this.#stopTimer();
    this.stepList.replaceChildren();
    this.result.replaceChildren();
    this.result.hidden = true;
    delete this.result.dataset.outcome;
    delete this.progress.dataset.outcome;
    this.title.textContent =
      job.mode === "removal" ? `Removal of ${job.project_name}` : `${context.title ?? "Deployment"} → ${job.project_name}`;
    this.elapsed.textContent = "";
    this.progressLabel.textContent = "Waiting for the plan…";
    this.#setPercent(0, "Waiting for the plan");
    this.setConnection("connecting");
    this.empty.hidden = true;
    this.view.hidden = false;
    this.#setStatus(job.mode === "removal" ? "removing" : "deploying");
    this.#announce(
      job.mode === "removal" ? `Removal of ${job.project_name} started.` : `Deployment of ${job.project_name} started.`,
    );
  }

  setConnection(state) {
    const note = CONNECTION_NOTES[state];
    this.connectionNote.hidden = !note;
    if (!note) return;
    this.connectionNote.dataset.state = state;
    this.connectionText.textContent = note.text;
    this.retryButton.hidden = !note.retry;
  }

  handle(event) {
    const at = parseTime(event.timestamp);
    this.#startedAt ??= at;
    this.#record(event, at);
    switch (event.type) {
      case "job.accepted":
        this.#renderPlan(Array.isArray(event.plan) ? event.plan : []);
        break;
      case "step.started":
        this.#stepStarted(event, at);
        break;
      case "step.log":
        this.#stepLog(event, at, "detail");
        break;
      case "step.completed":
        this.#stepEnded(event, at, "done");
        break;
      case "step.failed":
        this.#stepLog(event, at, "error");
        this.#stepEnded(event, at, "failed");
        break;
      case "job.succeeded":
        this.#finish("success", event, at);
        break;
      case "job.failed":
        this.#finish("error", event, at);
        break;
      default:
        break; // future event types only go to the copied logs
    }
  }

  // ---- Steps -----------------------------------------------------------------

  #renderPlan(plan) {
    this.#steps.clear();
    this.stepList.replaceChildren(...plan.map((step, index) => this.#createStep(step, index)));
    this.#refreshProgress();
  }

  #createStep(step, index) {
    const logsId = `step-logs-${index}`;
    const meta = el("span", { className: "step-meta", text: "pending" });
    const toggle = el("button", {
      className: "step-toggle",
      attrs: { type: "button", "aria-expanded": "false", "aria-controls": logsId, disabled: "" },
    }, [
      el("span", { className: "step-icon", attrs: { "aria-hidden": "true" } }),
      el("span", { className: "step-label", text: `${index + 1}. ${step.label}` }),
      meta,
      icon("chevron", "icon step-chevron"),
    ]);
    const preview = el("p", { className: "step-preview", attrs: { hidden: "" } });
    const list = el("ol", {
      className: "log-list",
      attrs: { "aria-label": `${step.label} logs`, tabindex: "0" },
    });
    const jump = el("button", { className: "jump-latest", attrs: { type: "button", hidden: "" } }, [
      icon("arrowDown"),
      el("span", { text: "Jump to latest" }),
    ]);
    const logs = el("div", { className: "log-view", attrs: { id: logsId, hidden: "" } }, [list, jump]);
    const item = el("li", { className: "step", attrs: { "data-state": "pending" } }, [toggle, preview, logs]);

    const view = {
      label: step.label, index, item, toggle, meta, preview, logs, list, jump,
      state: "pending", startedAt: null, endedAt: null, lastLine: "", expanded: false,
    };
    toggle.addEventListener("click", () => this.#setExpanded(view, !view.expanded));
    list.addEventListener("scroll", () => {
      view.jump.hidden = isAtBottom(list);
    });
    jump.addEventListener("click", () => {
      list.scrollTop = list.scrollHeight;
      jump.hidden = true;
    });
    this.#steps.set(step.key, view);
    return item;
  }

  #stepStarted(event, at) {
    const view = this.#steps.get(event.step_key);
    if (!view) return;
    view.startedAt = at;
    this.#setStepState(view, "running");
    this.#announce(`Step ${view.index + 1} of ${this.#steps.size}: ${view.label}.`);
    this.#startTimer();
  }

  #stepEnded(event, at, state) {
    const view = this.#steps.get(event.step_key);
    if (!view) return;
    view.endedAt = at;
    this.#setStepState(view, state);
    if (state === "failed") {
      this.#setExpanded(view, true);
      this.#announce(`Step failed: ${view.label}.`);
    }
  }

  #stepLog(event, at, level) {
    const view = this.#steps.get(event.step_key);
    if (!view) return; // job-level messages only go to the copied logs
    const list = view.list;
    const follow = isAtBottom(list);
    list.append(
      el("li", { className: "log-entry", attrs: { "data-level": level } }, [
        el("time", {
          text: at ? at.toLocaleTimeString() : "--:--:--",
          attrs: { datetime: String(event.timestamp) },
        }),
        el("span", { text: event.message }),
      ]),
    );
    while (list.childElementCount > MAX_STEP_LOG_ENTRIES) list.firstElementChild.remove();
    // Follow the tail only if the reader has not scrolled up to older lines.
    if (follow) list.scrollTop = list.scrollHeight;
    else if (view.expanded) view.jump.hidden = false;
    view.lastLine = event.message;
    this.#refreshPreview(view);
  }

  #setStepState(view, state) {
    view.state = state;
    view.item.dataset.state = state;
    view.toggle.disabled = state === "pending";
    if (state === "running") view.item.setAttribute("aria-current", "step");
    else view.item.removeAttribute("aria-current");
    this.#refreshStepMeta(view);
    this.#refreshPreview(view);
    this.#refreshProgress();
  }

  #setExpanded(view, expanded) {
    view.expanded = expanded;
    view.toggle.setAttribute("aria-expanded", String(expanded));
    view.logs.hidden = !expanded;
    if (expanded) {
      view.list.scrollTop = view.list.scrollHeight;
      view.jump.hidden = true;
    }
    this.#refreshPreview(view);
  }

  /** The last log line of the running step, while its logs are collapsed. */
  #refreshPreview(view) {
    view.preview.textContent = view.lastLine;
    view.preview.hidden = view.state !== "running" || view.expanded || !view.lastLine;
  }

  #refreshStepMeta(view) {
    const duration = (end) =>
      view.startedAt && end ? formatDuration(end - view.startedAt, view.state !== "running") : "";
    const text = {
      pending: "pending",
      running: `in progress · ${duration(new Date())}`,
      done: `done · ${duration(view.endedAt)}`,
      failed: `failed · ${duration(view.endedAt)}`,
    }[view.state];
    view.meta.textContent = text.replace(/ · $/, "");
  }

  // ---- Progress & outcome --------------------------------------------------------

  #counts() {
    const counts = { pending: 0, running: 0, done: 0, failed: 0 };
    for (const { state } of this.#steps.values()) counts[state] += 1;
    return counts;
  }

  #refreshProgress() {
    const counts = this.#counts();
    const total = this.#steps.size;
    const label =
      `${counts.done} of ${total} steps done` + (counts.failed ? ` · ${counts.failed} failed` : "");
    this.progressLabel.textContent = label;
    this.#setPercent(((counts.done + counts.running * 0.5) / (total || 1)) * 100, label);
    this.#notify();
  }

  #setPercent(percent, text) {
    const value = Math.round(Math.min(Math.max(percent, 0), 100));
    this.progressBar.style.width = `${value}%`; // CSSOM: allowed by the CSP, unlike style=""
    this.progress.setAttribute("aria-valuenow", String(value));
    this.progress.setAttribute("aria-valuetext", text);
  }

  #finish(outcome, event, at) {
    this.#endedAt = at;
    this.#stopTimer();
    const removal = this.#job?.mode === "removal";
    if (outcome === "success") this.#setPercent(100, "Completed");
    this.progress.dataset.outcome = outcome;
    this.#setStatus(outcome === "success" ? (removal ? "removed" : "ready") : "failed");
    this.#refreshElapsed();

    this.result.dataset.outcome = outcome;
    this.result.replaceChildren(...this.#resultContent(outcome, event));
    this.result.hidden = false;
    this.#announce(outcome === "success" ? event.message : `Failed: ${event.message}`);
  }

  #resultContent(outcome, event) {
    const children = [
      el("p", { className: "result-title" }, [
        icon(outcome === "success" ? "check" : "alert"),
        el("span", { text: event.message }),
      ]),
    ];
    if (outcome === "error") {
      if (this.#counts().failed) {
        children.push(el("p", { className: "hint", text: "The failed step is open below with its logs." }));
      }
      return children;
    }
    const project = this.#job?.project_name;
    if (this.#job?.mode === "removal" || !project) return children;

    const template = this.#context.template ?? null;
    const urls = this.#environmentUrls(event, project, template);
    if (urls.length) {
      children.push(resultSection("Web access", el("ul", { className: "url-list" }, urls.map(urlItem))));
    }
    const notes = template?.access_notes ?? [];
    if (notes.length) {
      children.push(resultSection(
        "Good to know",
        el("ul", { className: "notes" }, notes.map((note) => el("li", { text: withProject(note, project) }))),
      ));
    }

    const actions = [];
    if (urls.length) {
      actions.push(el("a", {
        className: "button primary",
        attrs: { href: urls[0].url, target: "_blank", rel: "noopener noreferrer" },
      }, [el("span", { text: "Open environment" }), icon("external")]));
    }
    const remove = el("button", { className: "button danger", text: "Remove environment", attrs: { type: "button" } });
    remove.addEventListener("click", () => this.onRemove({ project, volumes: template ? template.volumes : null }));
    actions.push(remove);
    children.push(el("div", { className: "result-actions" }, actions));
    return children;
  }

  /** Every web UI of the environment, main one first. Only EnvCrafter-shaped URLs pass. */
  #environmentUrls(event, project, template) {
    const urls = template
      ? template.components
          .filter((component) => component.web_access)
          .map((component) => ({
            name: component.name,
            url: webUrl(component.web_access, project),
            main: isMainHost(component.web_access),
          }))
          .sort((a, b) => Number(b.main) - Number(a.main))
      : typeof event.url === "string"
        ? [{ name: "Web interface", url: event.url, main: true }]
        : [];
    return urls.filter(({ url }) => ENVIRONMENT_URL.test(url));
  }

  // ---- Status, time, logs ----------------------------------------------------------

  #setStatus(status) {
    this.#status = status;
    this.badge.dataset.tone = STATUS[status].tone;
    this.badge.textContent = STATUS[status].label;
    this.#notify();
  }

  #notify() {
    this.onChange?.(this.summary);
  }

  #announce(text) {
    this.announcer.textContent = text;
  }

  #startTimer() {
    if (this.#timer) return;
    this.#timer = window.setInterval(() => this.#tick(), 1000);
  }

  #stopTimer() {
    window.clearInterval(this.#timer);
    this.#timer = 0;
  }

  #tick() {
    this.#refreshElapsed();
    for (const view of this.#steps.values()) {
      if (view.state === "running") this.#refreshStepMeta(view);
    }
  }

  #refreshElapsed() {
    if (!this.#startedAt) return;
    if (!this.#endedAt) {
      this.elapsed.textContent = `Elapsed ${formatDuration(Date.now() - this.#startedAt)}`;
      return;
    }
    const duration = formatDuration(this.#endedAt - this.#startedAt, true);
    this.elapsed.textContent =
      this.#status === "failed" ? `Failed after ${duration}` : `Completed in ${duration}`;
  }

  #record(event, at) {
    const time = at ? at.toLocaleTimeString() : "--:--:--";
    this.#copyLines.push(`${time}  ${event.message}`);
    if (this.#copyLines.length > MAX_COPY_LINES) this.#copyLines.shift();
  }

  async #copyLogs() {
    if (!(await copyText(this.#copyLines.join("\n")))) {
      showToast("Could not copy the logs. Open a step to select its lines instead.");
      return;
    }
    const label = this.copyLogsButton.lastChild;
    label.textContent = "Copied";
    window.setTimeout(() => {
      label.textContent = "Copy logs";
    }, COPY_CONFIRMATION_MS);
  }
}

function resultSection(title, content) {
  return el("div", { className: "result-section" }, [el("h4", { text: title }), content]);
}

function urlItem({ name, url }) {
  return el("li", {}, [
    el("span", { className: "url-name", text: name }),
    el("a", { text: url, attrs: { href: url, target: "_blank", rel: "noopener noreferrer" } }),
    copyButton(url, `Copy the address of ${name}`),
  ]);
}
