/** Entry point: wires the catalog, the request form, the dialogs and the status console. */
import { ApiError, createJob, fetchTemplates, removeEnvironment } from "./api.js";
import { Catalog } from "./catalog.js";
import { icon } from "./icons.js";
import { openJobStream } from "./job-stream.js";
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

let activeStream = null;
let activeJob = null; // { job, context } currently displayed
let summary = null; // last StatusConsole summary
let statusInView = true;
let categoryLabels = new Map();
const categoryLabel = (id) => categoryLabels.get(id) ?? id;

// Short labels for the browser tab and the floating shortcut.
const HEADLINES = {
  deploying: ({ project, done, total }) => `Deploying ${project}${total ? ` (${done}/${total})` : ""}`,
  removing: ({ project }) => `Removing ${project}`,
  ready: ({ project }) => `✓ ${project} ready`,
  removed: ({ project }) => `✓ ${project} removed`,
  failed: ({ project }) => `✕ ${project} failed`,
};

// Icons declared in the markup: <button data-icon="close">.
for (const node of document.querySelectorAll("[data-icon]")) node.prepend(icon(node.dataset.icon));

const statusConsole = new StatusConsole(statusPanel, {
  onRemove: (target) => removeDialog.open(target),
  onReconnect: () => follow(activeJob),
  onChange: (next) => updateChrome(next),
});
const removeDialog = new RemoveDialog(document.querySelector("#remove-dialog"), {
  onConfirm: (project) => run(() => removeEnvironment(project), {}),
});
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
  onSubmit: (prompt) => run(() => createJob({ mode: "prompt", prompt }), { title: "AI request" }),
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
  setBusy(true);
  try {
    const job = await startJob();
    activeJob = { job, context };
    follow(activeJob);
    revealStatus();
    return null;
  } catch (error) {
    return error instanceof ApiError ? error.message : "Unable to reach the EnvCrafter API. Check that it is running.";
  } finally {
    setBusy(false);
  }
}

/** (Re)subscribe from the first event: the console rebuilds the whole view from the replay. */
function follow({ job, context }) {
  // One job is displayed at a time. The previous one keeps running server-side.
  activeStream?.close();
  statusConsole.start(job, context);
  activeStream = openJobStream(job.job_id, {
    onEvent: (event) => statusConsole.handle(event),
    onConnectionChange: (state) => statusConsole.setConnection(state),
  });
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
  const headline = HEADLINES[next.status];
  document.title = headline && next.project ? `${headline(next)} · ${BASE_TITLE}` : BASE_TITLE;
  refreshPill();
}

/** Floating shortcut to the status panel, shown only while the panel is out of view. */
function refreshPill() {
  const headline = summary && HEADLINES[summary.status];
  jobPill.hidden = statusInView || !headline || !summary.project;
  if (jobPill.hidden) return;
  jobPill.dataset.tone = STATUS[summary.status].tone;
  jobPillText.textContent = headline(summary);
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
    catalog.render(categories, templates);
    promptForm.showSuggestions(new Set(templates.map((template) => template.id)));
  } catch {
    catalog.showError("Templates are unavailable. Is the API running?");
  }
}

loadCatalog();
