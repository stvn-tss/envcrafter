/** REST client for the EnvCrafter API (same origin: no CORS, no tokens in JS). */

export class ApiError extends Error {}

async function readError(response) {
  try {
    const body = await response.json();
    // FastAPI returns a string for HTTPException and a list for validation errors (422).
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && body.detail.length > 0) return body.detail[0].msg;
  } catch {
    /* non-JSON error body */
  }
  return `Request failed (HTTP ${response.status})`;
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

export function fetchTemplates() {
  return getJson("/api/templates");
}

/** Server capabilities: engine, LLM availability, public domain, project name rule. */
export function fetchConfig() {
  return getJson("/api/config");
}

/** Every environment with its live state (GET /api/environments). */
export function fetchEnvironments() {
  return getJson("/api/environments");
}

/** One job summary; rejects with ApiError (404) once the job is forgotten. */
export function fetchJob(jobId) {
  return getJson(`/api/jobs/${encodeURIComponent(jobId)}`);
}

/** Queued and running jobs, newest first. */
export function fetchActiveJobs() {
  return getJson("/api/jobs?active=true");
}

/** Starts a deployment. Resolves with the job summary (HTTP 202). */
export async function createJob(payload) {
  const response = await fetch("/api/jobs", {
    method: "POST",
    // Required by the server: it rejects anything that is not application/json.
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** Starts the removal of an environment. Resolves with the job summary (HTTP 202). */
export async function removeEnvironment(project) {
  const response = await fetch(`/api/environments/${encodeURIComponent(project)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** Stop, start or restart an environment. Resolves with the job summary (HTTP 202). */
export async function runEnvironmentAction(project, action, service = null) {
  const response = await fetch(`/api/environments/${encodeURIComponent(project)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(service ? { action, service } : { action }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** Starts the AI analysis of a request. Resolves with the planning job (HTTP 202). */
export async function createPlan(prompt) {
  const response = await fetch("/api/plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** The reviewable plan; rejects with ApiError (404) once it expired. */
export function fetchPlan(planId) {
  return getJson(`/api/plans/${encodeURIComponent(planId)}`);
}

/** First-run checklist: Docker, reverse proxy, memory, disk, AI key (GET /api/system). */
export function fetchSystem() {
  return getJson("/api/system");
}

/** What deploying a template needs on this machine (GET /api/templates/{id}/readiness). */
export function fetchReadiness(templateId) {
  return getJson(`/api/templates/${encodeURIComponent(templateId)}/readiness`);
}

/** Whether a Claude API key is configured, and where it comes from (never the key). */
export function fetchSettings() {
  return getJson("/api/settings");
}

/** Checks a Claude API key with the API, then stores it on the server. */
export async function saveLlmKey(apiKey) {
  const response = await fetch("/api/settings/llm-key", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
  });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** Stops a running deployment (rolled back) or AI analysis; the job ends with job.cancelled. */
export async function cancelJob(jobId) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** Forgets the key saved from the UI; the .env key, if any, is used again. */
export async function removeLlmKey() {
  const response = await fetch("/api/settings/llm-key", { method: "DELETE" });
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}
