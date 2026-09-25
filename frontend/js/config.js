/**
 * Server capabilities (GET /api/config), loaded once before anything renders, so the UI
 * adapts up front (simulated banner, prompt availability) instead of failing later.
 */
import { fetchConfig } from "./api.js";

const DEFAULTS = Object.freeze({
  version: "",
  engine: "docker",
  llm_available: true, // an unreachable API must not look like a missing key
  public_domain: "localhost",
  project_name_pattern: "^[a-z][a-z0-9-]{1,30}[a-z0-9]$",
});

let current = { ...DEFAULTS };

/** Server data is untrusted: keep the known fields with the expected types only. */
function sanitize(raw) {
  const result = {};
  if (typeof raw?.version === "string") result.version = raw.version;
  if (raw?.engine === "simulated" || raw?.engine === "docker") result.engine = raw.engine;
  if (typeof raw?.llm_available === "boolean") result.llm_available = raw.llm_available;
  if (typeof raw?.public_domain === "string" && /^[a-z0-9-]+(\.[a-z0-9-]+)*$/.test(raw.public_domain)) {
    result.public_domain = raw.public_domain;
  }
  if (typeof raw?.project_name_pattern === "string") result.project_name_pattern = raw.project_name_pattern;
  return result;
}

export async function loadConfig() {
  try {
    current = { ...DEFAULTS, ...sanitize(await fetchConfig()) };
  } catch {
    current = { ...DEFAULTS };
  }
  return current;
}

export function config() {
  return current;
}
