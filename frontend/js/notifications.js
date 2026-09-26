/**
 * Browser notifications when a job ends while EnvCrafter is in the background: first starts
 * take minutes, nobody should have to keep the tab in view. Opt-in: the browser asks for
 * permission only after a click (Settings, or "Notify me" next to a running job).
 */
const PREFERENCE_KEY = "envcrafter.notify";

function readPreference() {
  try {
    return localStorage.getItem(PREFERENCE_KEY);
  } catch {
    return null;
  }
}

function writePreference(value) {
  try {
    localStorage.setItem(PREFERENCE_KEY, value);
  } catch {
    /* storage unavailable: the choice lasts until the page is closed */
  }
}

export function notificationsSupported() {
  return typeof window.Notification === "function";
}

/** "on", "off" (turned off here), "blocked" (denied in the browser) or "unset". */
export function notificationState() {
  if (!notificationsSupported()) return "blocked";
  if (Notification.permission === "denied") return "blocked";
  if (readPreference() === "off") return "off";
  return Notification.permission === "granted" ? "on" : "unset";
}

/** Must run from a click: browsers only show the permission prompt after a user gesture. */
export async function enableNotifications() {
  if (!notificationsSupported()) return "blocked";
  const permission = await Notification.requestPermission();
  if (permission === "granted") writePreference("on");
  return notificationState();
}

export function disableNotifications() {
  writePreference("off");
}

/** Notify only when nobody is looking at the page. */
export function notifyIfAway(text, tag) {
  if (notificationState() !== "on") return;
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  try {
    const notification = new Notification("EnvCrafter", { body: text, tag });
    notification.addEventListener("click", () => {
      window.focus();
      notification.close();
    });
  } catch {
    /* some browsers only allow notifications from a service worker */
  }
}
