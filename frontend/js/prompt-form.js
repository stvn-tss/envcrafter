/** Natural-language request form: example requests, hints and inline errors. */
import { el } from "./dom.js";
import { icon } from "./icons.js";

const MAX_LENGTH = 2000;
const COUNTER_THRESHOLD = 1600; // the counter only shows up when the limit gets close
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform ?? "");

// Each example is something a catalog template covers, so it leads to a deployable
// stack. An example is only offered when its template is in the loaded catalog.
const SUGGESTIONS = [
  { templateId: "glpi", text: "An IT service desk with asset inventory" },
  { templateId: "zabbix", text: "Network and server monitoring with alerting" },
  { templateId: "audiobookshelf", text: "A server for my audiobooks and podcasts" },
  { templateId: "owasp-juice-shop", text: "A vulnerable web app to practise the OWASP Top 10" },
];

export class PromptForm {
  /** @param {{ onSubmit: (prompt: string) => Promise<string | null> }} options resolves with an error message */
  constructor(form, { onSubmit }) {
    this.input = form.querySelector("#prompt-input");
    this.counter = form.querySelector("#prompt-counter");
    this.error = form.querySelector("#prompt-error");
    this.suggestions = form.querySelector("#prompt-suggestions");
    form.querySelector("#shortcut-modifier").textContent = IS_MAC ? "⌘" : "Ctrl";

    this.input.addEventListener("input", () => {
      this.#refreshCounter();
      this.clearError();
    });
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) form.requestSubmit();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const prompt = this.input.value.trim();
      if (prompt.length < 3) return; // the server enforces the same limits (and more)
      this.clearError();
      const error = await onSubmit(prompt);
      if (error) this.showError(error);
    });
  }

  /** @param {Set<string>} templateIds ids present in the catalog */
  showSuggestions(templateIds) {
    const buttons = SUGGESTIONS.filter((suggestion) => templateIds.has(suggestion.templateId)).map(
      ({ text }) => {
        const button = el("button", { className: "suggestion", text, attrs: { type: "button" } });
        button.addEventListener("click", () => this.#fill(text));
        return button;
      },
    );
    this.suggestions.replaceChildren(el("span", { className: "suggestions-label", text: "Try:" }), ...buttons);
    this.suggestions.hidden = buttons.length === 0;
  }

  showError(message) {
    this.error.replaceChildren(icon("alert"), el("span", { text: message }));
    this.error.hidden = false;
  }

  clearError() {
    this.error.hidden = true;
    this.error.replaceChildren();
  }

  #fill(text) {
    this.input.value = text;
    this.#refreshCounter();
    this.clearError();
    this.input.focus();
    this.input.setSelectionRange(text.length, text.length);
  }

  #refreshCounter() {
    const length = this.input.value.length;
    this.counter.textContent = `${length} / ${MAX_LENGTH}`;
    this.counter.hidden = length < COUNTER_THRESHOLD;
  }
}
