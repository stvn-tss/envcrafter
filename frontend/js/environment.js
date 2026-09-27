/**
 * Environment naming and URL rules shared by the dialogs, the dashboard and the status console.
 * Both rules come from GET /api/config: the API stays the authority, the UI only gives
 * instant feedback.
 */
import { config } from "./config.js";

const DEFAULT_PROJECT_NAME = /^[a-z][a-z0-9-]{1,30}[a-z0-9]$/;

export function projectNamePattern() {
  try {
    return new RegExp(config().project_name_pattern);
  } catch {
    return DEFAULT_PROJECT_NAME;
  }
}

/**
 * Only links EnvCrafter itself generates are rendered clickable: http://<slug>(.<slug>)*.<domain>.
 * A server-provided href is still untrusted: this also rules out `javascript:` URLs.
 */
export function isEnvironmentUrl(url) {
  const domain = config().public_domain.replaceAll(".", "\\.");
  return typeof url === "string" && new RegExp(`^http://[a-z0-9-]+(\\.[a-z0-9-]+)*\\.${domain}$`).test(url);
}

// Placeholder used by the catalog in host names ("<project>.localhost") and access notes.
const PROJECT_TOKEN = "<project>";

/** User-facing reason why a typed project name is refused, or null when it is valid or empty. */
export function projectNameProblem(name) {
  if (name === "" || projectNamePattern().test(name)) return null;
  if (!/^[a-z0-9-]*$/.test(name)) return "Use only lowercase letters, digits and hyphens.";
  if (!/^[a-z]/.test(name)) return "Start with a lowercase letter.";
  if (name.length < 3) return "Use at least 3 characters.";
  if (name.length > 32) return "Use at most 32 characters.";
  return "End with a letter or a digit.";
}

/** The name the server gives a deployment without one: the first free <prefix>-<n>. */
export function nextProjectName(prefix, taken = new Set()) {
  const base = prefix.slice(0, 24).replace(/-+$/, "");
  for (let number = 1; number < 1000; number += 1) {
    const name = `${base}-${number}`;
    if (!taken.has(name)) return name;
  }
  return `${base}-1`;
}

export function withProject(text, project) {
  return text.replaceAll(PROJECT_TOKEN, project);
}

/** "<project>.localhost" or "sonarr.<project>.localhost" -> full URL for one project. */
export function webUrl(hostPattern, project) {
  return `http://${withProject(hostPattern, project)}`;
}

/** The main web UI is served at the project's own host name, the others under a subdomain. */
export function isMainHost(hostPattern) {
  return hostPattern.startsWith(PROJECT_TOKEN);
}
