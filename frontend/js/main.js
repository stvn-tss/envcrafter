/** Entry point: wires the catalog, the request form, the dialogs, the settings and the status console. */
import {
  ApiError, cancelJob, createJob, createPlan, fetchActiveJobs, fetchActivity, fetchEnvironmentUsage, fetchJob,
  fetchPlan, fetchReadiness, fetchSystem, fetchTemplates, removeEnvironment, runEnvironmentAction, updateEnvironment,
} from "./api.js";
import { Catalog, rankTemplates } from "./catalog.js";
import { loadConfig } from "./config.js";
import { EnvironmentDrawer } from "./environment-drawer.js";
import { withProject } from "./environment.js";
import { EnvironmentsPanel } from "./environments.js";
import { icon } from "./icons.js";
import { openJobStream } from "./job-stream.js";
import { LogsDialog } from "./logs-dialog.js";
import { enableNotifications, notificationState, notifyIfAway } from "./notifications.js";
import { PlanReview } from "./plan-review.js";
import { PromptForm } from "./prompt-form.js";
import { checkItems, hasError } from "./readiness.js";
import { RemoveDialog } from "./remove-dialog.js";
import { SettingsDialog } from "./settings-dialog.js";
import { STATUS, StatusConsole } from "./status-console.js";
import { TemplateDetails } from "./template-details.js";
import { showToast, showUndo } from "./toasts.js";

const BASE_TITLE = document.title;
const statusPanel = document.querySelector("#status-panel");
const statusTitle = document.querySelector("#status-title");
const jobPill = document.querySelector("#job-pill");
const jobPillText = document.querySelector("#job-pill-text");
const notifyOffer = document.querySelector("#notify-offer");

const JOB_KEY = "envcrafter.job";
const LIVE_STATES = new Set(["pending", "running", "starting", "degraded"]);
const TERMINAL = new Set(["ready", "removed", "stopped", "running", "planned", "failed", "cancelled"]);
const ACTIVE = new Set(["deploying", "removing", "stopping", "starting", "restarting", "planning", "cancelling"]);

/** The browser's time zone, sent with deployments and written to the workspace as ${EC_TZ}. */
const TIMEZONE = (() => {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return typeof zone === "string" && /^[A-Za-z][A-Za-z0-9_+-]*(\/[A-Za-z0-9_+-]+){0,2}$/.test(zone) ? zone : null;
  } catch {
    return null;
  }
})();

let activeStream = null;
let activeJob = null; // { job, context } currently displayed
let summary = null; // last StatusConsole summary
let statusInView = true;
let categoryLabels = new Map();
let templatesById = new Map();
let templates = [];
let lastStatus = null;
const categoryLabel = (id) => categoryLabels.get(id) ?? id;

// Short labels for the browser tab and the floating shortcut.
const HEADLINES = {
  deploying: ({ project, done, total }) => `Deploying ${project}${total ? ` (${done}/${total})` : ""}`,
  removing: ({ project }) => `Removing ${project}`,
  stopping: ({ project }) => `Stopping ${project}`,
  starting: ({ project }) => `Starting ${project}`,
  restarting: ({ project }) => `Restarting ${project}`,
  planning: () => "Analyzing request",
  cancelling: ({ project }) => (project ? `Cancelling ${project}` : "Cancelling analysis"),
  ready: ({ project }) => `✓ ${project} ready`,
  removed: ({ project }) => `✓ ${project} removed`,
  stopped: ({ project }) => `✓ ${project} stopped`,
  running: ({ project }) => `✓ ${project} running`,
  planned: () => "✓ Plan ready",
  failed: ({ project }) => (project ? `✕ ${project} failed` : "✕ Analysis failed"),
  cancelled: ({ project }) => (project ? `${project} cancelled` : "Analysis cancelled"),
};

// Icons declared in the markup: <button data-icon="close">.
for (const node of document.querySelectorAll("[data-icon]")) node.prepend(icon(node.dataset.icon));

const statusConsole = new StatusConsole(statusPanel, {
  onRemove: (target) => removeDialog.open(target),
  onReconnect: () => follow(activeJob),
  onChange: (next) => updateChrome(next),
  onReviewPlan: (planId) => openPlan(planId),
  onCancel: async (job) => {
    try {
      await cancelJob(job.job_id);
      environments.refresh();
      return null;
    } catch (error) {
      return error instanceof ApiError ? error.message : "Unable to reach the EnvCrafter API.";
    }
  },
});
// A disposable lab is removed after a few seconds, unless the user undoes it meanwhile.
const UNDO_SECONDS = 5;
const removeDialog = new RemoveDialog(document.querySelector("#remove-dialog"), {
  onConfirm: async (project, { disposable = false } = {}) => {
    if (!disposable) return run(() => removeEnvironment(project), {});
    if (!(await showUndo(`Removing ${project}`, UNDO_SECONDS))) return null; // undone
    const error = await run(() => removeEnvironment(project), {});
    if (error) showToast(error);
    return null;
  },
});
const logsDialog = new LogsDialog(document.querySelector("#logs-dialog"));
const details = new TemplateDetails(document.querySelector("#template-dialog"), {
  onDeploy: (template, projectName, options) => deployTemplate(template, projectName, options),
  categoryLabel,
  fetchReadiness,
  takenNames: () => takenNames(),
});
const catalog = new Catalog(document.querySelector("#catalog"), {
  onDetails: (template) => details.open(template),
  onDeploy: async (template) => {
    const error = await deployTemplate(template);
    if (error) showToast(error);
  },
  categoryLabel,
});
const settingsDialog = new SettingsDialog(document.querySelector("#settings-dialog"), {
  onKeyChange: () => refreshAvailability(),
  onNotificationsChange: () => refreshNotifyOffer(),
});
const promptForm = new PromptForm(document.querySelector("#prompt-form"), {
  onSubmit: (prompt) => run(() => createPlan(prompt), { title: "AI analysis", autoReview: true }),
  onAddKey: () => settingsDialog.open({ focusKey: true }),
  findTemplates: (text) => rankTemplates(templates, text, categoryLabel),
  onOpenTemplate: (template) => details.open(template),
});
const planReview = new PlanReview(document.querySelector("#plan-dialog"), {
  takenNames: () => takenNames(),
  onDeploy: (plan, projectName, { keepOnFailure = false } = {}) => {
    const payload = withTimezone({ mode: "plan", plan_id: plan.plan_id });
    if (projectName) payload.project_name = projectName;
    if (keepOnFailure) payload.keep_on_failure = true;
    return run(() => createJob(payload), { title: plan.title, templateId: plan.template_id ?? undefined });
  },
});
document.querySelector("#settings-open").addEventListener("click", () => settingsDialog.open());
notifyOffer.addEventListener("click", async () => {
  await enableNotifications();
  refreshNotifyOffer();
});

/** Projects on the dashboard: an unnamed deployment gets the first free <template>-<n>. */
function takenNames() {
  return new Set(environments.environments.map((environment) => environment.project));
}

function withTimezone(payload) {
  if (TIMEZONE) payload.timezone = TIMEZONE;
  return payload;
}

/** A key was saved or removed in Settings: switch the request form between AI and search. */
async function refreshAvailability() {
  const capabilities = await loadConfig();
  promptForm.setAvailability(capabilities.llm_available);
}

async function openPlan(planId) {
  try {
    planReview.open(await fetchPlan(planId));
  } catch (error) {
    showToast(error instanceof ApiError ? error.message : "The plan could not be loaded.");
  }
}
/** Stop, start or restart an environment, or restart one of its services. */
async function runEnvironmentJob(environment, action, service = null) {
  const error = await run(
    () => runEnvironmentAction(environment.project, action, service),
    { title: environment.title, templateId: environment.template_id },
  );
  if (error) {
    showToast(error);
    environments.refresh(); // re-enables the buttons without waiting for the next poll
  }
}

/** Default accounts and first steps of the environment's template. */
function accessNotes(environment) {
  const template = templatesById.get(environment.template_id);
  const notes = Array.isArray(template?.access_notes) ? template.access_notes : [];
  return notes.map((note) => withProject(note, environment.project));
}

const drawer = new EnvironmentDrawer(document.querySelector("#environment-drawer"), {
  onAction: (environment, action) => runEnvironmentJob(environment, action),
  onRestartService: (environment, service) => runEnvironmentJob(environment, "restart", service),
  onLogs: (environment) => logsDialog.open(environment),
  onRemove: (target) => removeDialog.open(target),
  onSave: async (project, changes) => {
    try {
      await updateEnvironment(project, changes);
      environments.refresh();
      return null;
    } catch (error) {
      return error instanceof ApiError ? error.message : "Unable to reach the EnvCrafter API.";
    }
  },
  notesFor: accessNotes,
  templateFor: (environment) => templatesById.get(environment.template_id),
  fetchUsage: fetchEnvironmentUsage,
  fetchActivity,
});

const environments = new EnvironmentsPanel(document.querySelector("#environments"), {
  onAction: (project, action, environment) => runEnvironmentJob(environment, action),
  onLogs: (environment) => logsDialog.open(environment),
  onRestartService: (environment, service) => runEnvironmentJob(environment, "restart", service),
  onRemove: (target) => removeDialog.open(target),
  onDetails: (environment) => drawer.open(environment),
  onFollow: (job, environment) => follow({ job: { ...job, project_name: environment.project }, context: { title: environment.title, templateId: environment.template_id } }),
  notesFor: accessNotes,
  onUpdate: (list) => {
    drawer.update(list);
    const counts = new Map();
    for (const environment of list) {
      if (environment.template_id && LIVE_STATES.has(environment.state)) {
        counts.set(environment.template_id, (counts.get(environment.template_id) ?? 0) + 1);
      }
    }
    catalog.setRunningCounts(counts);
  },
});

function setBusy(busy) {
  for (const button of document.querySelectorAll("[data-deploy]")) button.disabled = busy;
}

/**
 * Start a job (answered with 202 at once), then follow its progress over WebSocket.
 * Resolves with a user-facing error message, or null. Errors are shown by the caller,
 * next to what triggered them: the job currently displayed stays on screen.
 */
async function run(startJob, context) {
  // The same request again, from the failed job's Retry button.
  context.retry = () => run(startJob, context);
  setBusy(true);
  try {
    const job = await startJob();
    activeJob = { job, context };
    follow(activeJob);
    revealStatus();
    environments.refresh();
    return null;
  } catch (error) {
    return error instanceof ApiError ? error.message : "Unable to reach the EnvCrafter API. Check that it is running.";
  } finally {
    setBusy(false);
  }
}

/** (Re)subscribe from the first event: the console rebuilds the whole view from the replay. */
function follow({ job, context }) {
  context.template ??= templatesById.get(context.templateId);
  // One job is displayed at a time. The previous one keeps running server-side.
  activeStream?.close();
  activeJob = { job, context };
  statusConsole.start(job, context);
  remember(job, context);
  activeStream = openJobStream(job.job_id, {
    onEvent: (event) => statusConsole.handle(event),
    onConnectionChange: (state) => statusConsole.setConnection(state),
  });
}

/** Keeps the currently displayed job in `sessionStorage`, so a reload can re-attach to it. */
function remember(job, context) {
  try {
    sessionStorage.setItem(JOB_KEY, JSON.stringify({
      job_id: job.job_id, title: context.title ?? null, template_id: context.template?.id ?? context.templateId ?? null,
    }));
  } catch {
    /* storage unavailable (private mode): re-attach falls back to the active jobs */
  }
}

/** After a reload: the job shown before (same tab), else the newest active job. */
async function resumeJob() {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(JOB_KEY) ?? "null");
  } catch {
    saved = null;
  }
  if (typeof saved?.job_id === "string") {
    try {
      const job = await fetchJob(saved.job_id);
      follow({ job, context: { title: saved.title ?? undefined, templateId: saved.template_id } });
      return;
    } catch {
      /* forgotten or unknown: fall through */
    }
  }
  try {
    const { jobs } = await fetchActiveJobs();
    if (Array.isArray(jobs) && jobs[0]) follow({ job: jobs[0], context: {} });
  } catch {
    /* nothing to resume */
  }
}

function deployTemplate(template, projectName = null, { parameters = {}, keepOnFailure = false } = {}) {
  const payload = withTimezone({ mode: "template", template_id: template.id });
  if (projectName) payload.project_name = projectName;
  if (Object.keys(parameters).length) payload.parameters = parameters;
  if (keepOnFailure) payload.keep_on_failure = true;
  return run(() => createJob(payload), { title: template.name, template });
}

function scrollBehavior() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

/** In the single-column layout the panel sits below the catalog: bring it into view. */
function revealStatus() {
  const { top } = statusPanel.getBoundingClientRect();
  if (top >= 0 && top < window.innerHeight / 2) return;
  statusPanel.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
}

function updateChrome(next) {
  summary = next;
  const text = HEADLINES[next.status]?.(next);
  document.title = text ? `${text} · ${BASE_TITLE}` : BASE_TITLE;
  refreshPill();
  if (next.status !== lastStatus && TERMINAL.has(next.status)) {
    environments.refresh();
    // Only a job seen running here: a replayed, long-finished job notifies nobody.
    if (ACTIVE.has(lastStatus) && text) notifyIfAway(text, activeJob?.job?.job_id);
  }
  lastStatus = next.status;
  refreshNotifyOffer();
}

/** "Notify me when it's done" next to a running job, until notifications are decided. */
function refreshNotifyOffer() {
  notifyOffer.hidden = !(summary && ACTIVE.has(summary.status) && notificationState() === "unset");
}

/** Floating shortcut to the status panel, shown only while the panel is out of view. */
function refreshPill() {
  const text = summary && HEADLINES[summary.status]?.(summary);
  jobPill.hidden = statusInView || !text;
  if (jobPill.hidden) return;
  jobPill.dataset.tone = STATUS[summary.status].tone;
  jobPillText.textContent = text;
}

new IntersectionObserver(([entry]) => {
  statusInView = entry.isIntersecting;
  refreshPill();
}).observe(statusPanel);

jobPill.addEventListener("click", () => {
  statusPanel.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  statusTitle.focus({ preventScroll: true });
});

async function loadCatalog() {
  try {
    const { categories, templates: list } = await fetchTemplates();
    categoryLabels = new Map(categories.map((category) => [category.id, category.label]));
    templates = Array.isArray(list) ? list : [];
    templatesById = new Map(templates.map((template) => [template.id, template]));
    catalog.render(categories, templates);
    promptForm.showSuggestions(new Set(templates.map((template) => template.id)));
  } catch {
    catalog.showError("Templates are unavailable. Is the API running?");
  }
}

const SETUP_KEY = "envcrafter.setup-dismissed";
const setupPanel = document.querySelector("#setup-check");

function setupDismissed() {
  try {
    return localStorage.getItem(SETUP_KEY) === "1";
  } catch {
    return false;
  }
}

/** First visit: the whole checklist; afterwards, only when Docker or the proxy is down. */
async function loadSetupCheck() {
  let system;
  try {
    system = await fetchSystem();
  } catch {
    return; // the API itself is down: the rest of the page already says so
  }
  document.querySelector("#setup-list").replaceChildren(...checkItems(system));
  setupPanel.hidden = setupDismissed() && !hasError(system);
}

document.querySelector("#setup-dismiss").addEventListener("click", () => {
  try {
    localStorage.setItem(SETUP_KEY, "1");
  } catch {
    /* storage unavailable: hidden until the page is reloaded */
  }
  setupPanel.hidden = true;
});

async function init() {
  const capabilities = await loadConfig();
  loadSetupCheck();
  document.querySelector("#engine-banner").hidden = capabilities.engine !== "simulated";
  promptForm.setAvailability(capabilities.llm_available);
  await loadCatalog();
  environments.start();
  await resumeJob();
}

init();
