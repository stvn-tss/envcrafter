/** Entry point: wires the catalog, the request form, the dialogs and the status console. */
import {
  ApiError, createJob, createPlan, fetchActiveJobs, fetchJob, fetchPlan, fetchTemplates, removeEnvironment,
  runEnvironmentAction,
} from "./api.js";
import { Catalog } from "./catalog.js";
import { loadConfig } from "./config.js";
import { EnvironmentsPanel } from "./environments.js";
import { icon } from "./icons.js";
import { openJobStream } from "./job-stream.js";
import { LogsDialog } from "./logs-dialog.js";
import { PlanReview } from "./plan-review.js";
import { PromptForm } from "./prompt-form.js";
import { RemoveDialog } from "./remove-dialog.js";
import { STATUS, StatusConsole } from "./status-console.js";
import { TemplateDetails } from "./template-details.js";
import { showToast } from "./toasts.js";

const BASE_TITLE = document.title;
const statusPanel = document.querySelector("#status-panel");
const statusTitle = document.querySelector("#status-title");
const jobPill = document.querySelector("#job-pill");
const jobPillText = document.querySelector("#job-pill-text");

const JOB_KEY = "envcrafter.job";
const LIVE_STATES = new Set(["pending", "running", "starting", "degraded"]);
const TERMINAL = new Set(["ready", "removed", "stopped", "running", "planned", "failed"]);

let activeStream = null;
let activeJob = null; // { job, context } currently displayed
let summary = null; // last StatusConsole summary
let statusInView = true;
let categoryLabels = new Map();
let templatesById = new Map();
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
  ready: ({ project }) => `✓ ${project} ready`,
  removed: ({ project }) => `✓ ${project} removed`,
  stopped: ({ project }) => `✓ ${project} stopped`,
  running: ({ project }) => `✓ ${project} running`,
  planned: () => "✓ Plan ready",
  failed: ({ project }) => (project ? `✕ ${project} failed` : "✕ Analysis failed"),
};

// Icons declared in the markup: <button data-icon="close">.
for (const node of document.querySelectorAll("[data-icon]")) node.prepend(icon(node.dataset.icon));

const statusConsole = new StatusConsole(statusPanel, {
  onRemove: (target) => removeDialog.open(target),
  onReconnect: () => follow(activeJob),
  onChange: (next) => updateChrome(next),
  onReviewPlan: (planId) => openPlan(planId),
});
const removeDialog = new RemoveDialog(document.querySelector("#remove-dialog"), {
  onConfirm: (project) => run(() => removeEnvironment(project), {}),
});
const logsDialog = new LogsDialog(document.querySelector("#logs-dialog"));
const details = new TemplateDetails(document.querySelector("#template-dialog"), {
  onDeploy: (template, projectName) => deployTemplate(template, projectName),
  categoryLabel,
});
const catalog = new Catalog(document.querySelector("#catalog"), {
  onDetails: (template) => details.open(template),
  onDeploy: async (template) => {
    const error = await deployTemplate(template);
    if (error) showToast(error);
  },
  categoryLabel,
});
const promptForm = new PromptForm(document.querySelector("#prompt-form"), {
  onSubmit: (prompt) => run(() => createPlan(prompt), { title: "AI analysis", autoReview: true }),
});
const planReview = new PlanReview(document.querySelector("#plan-dialog"), {
  onDeploy: (plan, projectName) => {
    const payload = { mode: "plan", plan_id: plan.plan_id };
    if (projectName) payload.project_name = projectName;
    return run(() => createJob(payload), { title: plan.title, templateId: plan.template_id ?? undefined });
  },
});

async function openPlan(planId) {
  try {
    planReview.open(await fetchPlan(planId));
  } catch (error) {
    showToast(error instanceof ApiError ? error.message : "The plan could not be loaded.");
  }
}
const environments = new EnvironmentsPanel(document.querySelector("#environments"), {
  onAction: async (project, action, environment) => {
    const error = await run(() => runEnvironmentAction(project, action), { title: environment.title, templateId: environment.template_id });
    if (error) showToast(error);
  },
  onLogs: (environment) => logsDialog.open(environment),
  onRemove: (target) => removeDialog.open(target),
  onFollow: (job, environment) => follow({ job: { ...job, project_name: environment.project }, context: { title: environment.title, templateId: environment.template_id } }),
  onUpdate: (list) => {
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
  for (const button of document.querySelectorAll("[data-deploy]")) {
    button.disabled = busy || button.hasAttribute("data-unavailable");
  }
}

/**
 * Start a job (answered with 202 at once), then follow its progress over WebSocket.
 * Resolves with a user-facing error message, or null. Errors are shown by the caller,
 * next to what triggered them: the job currently displayed stays on screen.
 */
async function run(startJob, context) {
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

function deployTemplate(template, projectName = null) {
  const payload = { mode: "template", template_id: template.id };
  if (projectName) payload.project_name = projectName;
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
  if (next.status !== lastStatus && TERMINAL.has(next.status)) environments.refresh();
  lastStatus = next.status;
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
    const { categories, templates } = await fetchTemplates();
    categoryLabels = new Map(categories.map((category) => [category.id, category.label]));
    templatesById = new Map(templates.map((template) => [template.id, template]));
    catalog.render(categories, templates);
    promptForm.showSuggestions(new Set(templates.map((template) => template.id)));
  } catch {
    catalog.showError("Templates are unavailable. Is the API running?");
  }
}

const LLM_UNAVAILABLE =
  "Natural-language requests are turned off on this server: set ENVCRAFTER_LLM_API_KEY to enable them.";

async function init() {
  const capabilities = await loadConfig();
  document.querySelector("#engine-banner").hidden = capabilities.engine !== "simulated";
  promptForm.setAvailability(capabilities.llm_available, LLM_UNAVAILABLE);
  await loadCatalog();
  environments.start();
  await resumeJob();
}

init();
