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

export async function fetchTemplates() {
  const response = await fetch("/api/templates");
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
}

/** Server capabilities: engine, LLM availability, public domain, project name rule. */
export async function fetchConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new ApiError(await readError(response));
  return response.json();
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
