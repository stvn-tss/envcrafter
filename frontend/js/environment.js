/**
 * Environment naming and URL rules shared by the dialogs and the status console.
 *
 * PROJECT_NAME mirrors PROJECT_NAME_PATTERN (backend/app/models/common.py). It only
 * gives instant feedback while typing: the API stays the authority.
 */
export const PROJECT_NAME = /^[a-z][a-z0-9-]{1,30}[a-z0-9]$/;

// Only links EnvCrafter itself generates are rendered clickable: http://<slug>.localhost
// (a server-provided href is still untrusted: this also rules out `javascript:` URLs).
export const ENVIRONMENT_URL = /^http:\/\/[a-z0-9-]+(\.[a-z0-9-]+)*\.localhost$/;

// Placeholder used by the catalog in host names ("<project>.localhost") and access notes.
const PROJECT_TOKEN = "<project>";

/** User-facing reason why a typed project name is refused, or null when it is valid or empty. */
export function projectNameProblem(name) {
  if (name === "" || PROJECT_NAME.test(name)) return null;
  if (!/^[a-z0-9-]*$/.test(name)) return "Use only lowercase letters, digits and hyphens.";
  if (!/^[a-z]/.test(name)) return "Start with a lowercase letter.";
  if (name.length < 3) return "Use at least 3 characters.";
  if (name.length > 32) return "Use at most 32 characters.";
  return "End with a letter or a digit.";
}

/** Shape of the name the server picks when none is given: <template id>-<4 hex digits>. */
export function autoProjectName(templateId) {
  return `${templateId.slice(0, 24).replace(/-+$/, "")}-xxxx`;
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
