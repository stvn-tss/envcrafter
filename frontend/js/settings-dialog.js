/**
 * Settings: the Claude API key (no more editing .env by hand), notifications and the theme.
 *
 * The key only travels from this form to the server (PUT /api/settings/llm-key), which
 * checks it with the Claude API before storing it. The server never sends it back: the
 * dialog shows where the active key comes from and its last characters only.
 */
import { ApiError, fetchSettings, fetchSystem, removeLlmKey, saveLlmKey } from "./api.js";
import { el } from "./dom.js";
import { icon } from "./icons.js";
import { disableNotifications, enableNotifications, notificationState } from "./notifications.js";
import { checkItems } from "./readiness.js";

const THEME_KEY = "envcrafter.theme";
const KEY_FORMAT = /^[A-Za-z0-9_-]{20,256}$/;
const NOTIFY_NOTES = {
  blocked: "Notifications are blocked for this site in the browser settings.",
  off: "",
  on: "",
  unset: "",
};

function readTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    return theme === "light" || theme === "dark" ? theme : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  try {
    if (theme === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* storage unavailable: the theme lasts until the page is closed */
  }
}

/** Server data is untrusted: keep the expected fields and types only. */
function sanitizeStatus(raw) {
  const llm = raw?.llm ?? {};
  return {
    configured: llm.configured === true,
    source: llm.source === "settings" || llm.source === "environment" ? llm.source : null,
    keyHint: typeof llm.key_hint === "string" ? llm.key_hint.slice(0, 8) : null,
    model: typeof llm.model === "string" ? llm.model.slice(0, 80) : "",
  };
}

export class SettingsDialog {
  /** @param {{ onKeyChange: () => void, onNotificationsChange?: () => void }} handlers */
  constructor(dialog, { onKeyChange, onNotificationsChange }) {
    this.dialog = dialog;
    this.onKeyChange = onKeyChange;
    this.onNotificationsChange = onNotificationsChange;
    this.statusLine = dialog.querySelector("#llm-status");
    this.form = dialog.querySelector("#llm-form");
    this.keyInput = dialog.querySelector("#llm-key");
    this.keyLabel = dialog.querySelector("#llm-key-label");
    this.error = dialog.querySelector("#llm-error");
    this.saveButton = dialog.querySelector("#llm-save");
    this.removeButton = dialog.querySelector("#llm-remove");
    this.notifyToggle = dialog.querySelector("#notify-toggle");
    this.notifyNote = dialog.querySelector("#notify-note");
    this.themeSelect = dialog.querySelector("#theme-select");
    this.systemList = dialog.querySelector("#settings-system");

    for (const button of dialog.querySelectorAll("[data-close]")) button.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", () => {
      this.keyInput.value = ""; // never keep a typed key around
    });
    this.keyInput.addEventListener("input", () => this.#hideError());
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.#save();
    });
    this.removeButton.addEventListener("click", () => this.#remove());
    this.notifyToggle.addEventListener("change", () => this.#toggleNotifications());
    this.themeSelect.addEventListener("change", () => applyTheme(this.themeSelect.value));
  }

  /** @param {{ focusKey?: boolean }} options focusKey: opened from "Add an API key" */
  async open({ focusKey = false } = {}) {
    this.#hideError();
    this.keyInput.value = "";
    this.themeSelect.value = readTheme();
    this.#renderNotifications();
    this.statusLine.replaceChildren(el("span", { text: "Checking…" }));
    this.dialog.showModal();
    if (focusKey) this.keyInput.focus();
    try {
      this.#render(sanitizeStatus(await fetchSettings()));
    } catch {
      this.statusLine.replaceChildren(el("span", { text: "The settings could not be loaded." }));
    }
    try {
      this.systemList.replaceChildren(...checkItems(await fetchSystem()));
    } catch {
      this.systemList.replaceChildren(el("li", {}, [el("span", { className: "hint", text: "The system checks could not be loaded." })]));
    }
  }

  #render(status) {
    let text = "No key: AI plans are off.";
    if (status.source === "settings") text = `Saved from these settings (key ending ${status.keyHint ?? "…"}).`;
    if (status.source === "environment") text = `Set in the server's .env file (key ending ${status.keyHint ?? "…"}).`;
    this.statusLine.dataset.configured = String(status.configured);
    this.statusLine.replaceChildren(
      icon(status.configured ? "check" : "key"),
      el("span", { text: status.model && status.configured ? `${text} Model: ${status.model}.` : text }),
    );
    this.keyLabel.textContent = status.configured ? "Replace the key" : "API key";
    this.saveButton.textContent = status.configured ? "Replace key" : "Save key";
    this.removeButton.hidden = status.source !== "settings";
  }

  async #save() {
    const key = this.keyInput.value.trim();
    if (!KEY_FORMAT.test(key)) {
      this.#showError("Paste the whole key: letters, digits, - and _ only (it usually starts with sk-ant-).");
      this.keyInput.focus();
      return;
    }
    this.#hideError();
    this.saveButton.disabled = true;
    const label = this.saveButton.textContent;
    this.saveButton.textContent = "Checking the key…";
    try {
      this.#render(sanitizeStatus(await saveLlmKey(key)));
      this.keyInput.value = "";
      this.onKeyChange();
    } catch (error) {
      this.saveButton.textContent = label;
      this.#showError(error instanceof ApiError ? error.message : "Unable to reach the EnvCrafter API.");
    } finally {
      this.saveButton.disabled = false;
    }
  }

  async #remove() {
    this.#hideError();
    this.removeButton.disabled = true;
    try {
      this.#render(sanitizeStatus(await removeLlmKey()));
      this.onKeyChange();
    } catch (error) {
      this.#showError(error instanceof ApiError ? error.message : "Unable to reach the EnvCrafter API.");
    } finally {
      this.removeButton.disabled = false;
    }
  }

  async #toggleNotifications() {
    if (this.notifyToggle.checked) await enableNotifications();
    else disableNotifications();
    this.#renderNotifications();
    this.onNotificationsChange?.();
  }

  #renderNotifications() {
    const state = notificationState();
    this.notifyToggle.checked = state === "on";
    this.notifyToggle.disabled = state === "blocked";
    this.notifyNote.textContent = NOTIFY_NOTES[state];
    this.notifyNote.hidden = !NOTIFY_NOTES[state];
  }

  #showError(message) {
    this.error.replaceChildren(icon("alert"), el("span", { text: message }));
    this.error.hidden = false;
  }

  #hideError() {
    this.error.hidden = true;
    this.error.replaceChildren();
  }
}
