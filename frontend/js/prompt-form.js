/**
 * Natural-language request form: example requests, hints and inline errors.
 *
 * Without a Claude API key the form stays usable: what the user types finds the closest
 * catalog templates (live), and a button opens Settings to add a key. It never turns
 * into a disabled box at the top of the page.
 */
import { el } from "./dom.js";
import { icon } from "./icons.js";

const MAX_LENGTH = 2000;
const COUNTER_THRESHOLD = 1600; // the counter only shows up when the limit gets close
const MATCH_DELAY_MS = 150;
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform ?? "");
const AI_PLACEHOLDER = "e.g. Deploy a test ITSM environment with a service desk and asset inventory";
const SEARCH_PLACEHOLDER = "e.g. A service desk with asset inventory: the closest templates show up below";

// Each example is something a catalog template covers, so it leads to a deployable
// stack. An example is only offered when its template is in the loaded catalog.
const SUGGESTIONS = [
  { templateId: "glpi", text: "An IT service desk with asset inventory" },
  { templateId: "zabbix", text: "Network and server monitoring with alerting" },
  { templateId: "audiobookshelf", text: "A server for my audiobooks and podcasts" },
  { templateId: "owasp-juice-shop", text: "A vulnerable web app to practise the OWASP Top 10" },
];

export class PromptForm {
  #available = true;
  #matchTimer = 0;

  /**
   * @param {{ onSubmit: (prompt: string) => Promise<string | null>,
   *           onAddKey: () => void,
   *           findTemplates: (text: string) => object[],
   *           onOpenTemplate: (template: object) => void }} options onSubmit resolves with an error message
   */
  constructor(form, { onSubmit, onAddKey, findTemplates, onOpenTemplate }) {
    this.input = form.querySelector("#prompt-input");
    this.counter = form.querySelector("#prompt-counter");
    this.error = form.querySelector("#prompt-error");
    this.suggestions = form.querySelector("#prompt-suggestions");
    this.matches = form.querySelector("#prompt-matches");
    this.noMatch = form.querySelector("#prompt-no-match");
    this.submitButton = form.querySelector('button[type="submit"]');
    this.unavailable = form.querySelector("#prompt-unavailable");
    this.findTemplates = findTemplates;
    this.onOpenTemplate = onOpenTemplate;
    form.querySelector("#shortcut-modifier").textContent = IS_MAC ? "⌘" : "Ctrl";
    form.querySelector("#prompt-add-key").addEventListener("click", () => onAddKey());

    this.input.addEventListener("input", () => {
      this.#refreshCounter();
      this.clearError();
      this.noMatch.hidden = true;
      if (!this.#available) this.#scheduleMatches();
    });
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) form.requestSubmit();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const prompt = this.input.value.trim();
      if (prompt.length < 3) return; // the server enforces the same limits (and more)
      this.clearError();
      if (!this.#available) {
        this.#showMatches(true);
        return;
      }
      const error = await onSubmit(prompt);
      if (error) this.showError(error);
    });
  }

  /** AI plans on (a key is configured) or off (the text searches the catalog). */
  setAvailability(available) {
    this.#available = available;
    this.input.placeholder = available ? AI_PLACEHOLDER : SEARCH_PLACEHOLDER;
    this.submitButton.textContent = available ? "Generate plan" : "Find templates";
    this.unavailable.hidden = available;
    this.noMatch.hidden = true;
    if (available) {
      window.clearTimeout(this.#matchTimer);
      this.matches.hidden = true;
      this.matches.replaceChildren();
    } else {
      this.#showMatches(false);
    }
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

  #scheduleMatches() {
    window.clearTimeout(this.#matchTimer);
    this.#matchTimer = window.setTimeout(() => this.#showMatches(false), MATCH_DELAY_MS);
  }

  /** Closest templates for the typed text. `submitted`: also say so when nothing matches. */
  #showMatches(submitted) {
    window.clearTimeout(this.#matchTimer);
    const text = this.input.value.trim();
    const found = text.length >= 3 ? this.findTemplates(text) : [];
    const buttons = found.map((template) => {
      const button = el("button", {
        className: "suggestion match",
        text: template.name,
        attrs: { type: "button", "aria-label": `Open ${template.name}` },
      });
      button.addEventListener("click", () => this.onOpenTemplate(template));
      return button;
    });
    this.matches.replaceChildren(el("span", { className: "suggestions-label", text: "Closest templates:" }), ...buttons);
    this.matches.hidden = buttons.length === 0;
    if (submitted && buttons.length) buttons[0].focus();
    this.noMatch.textContent = "No template matches these words. Try other words, browse the templates below, or add an API key to get a tailored plan.";
    this.noMatch.hidden = !(submitted && text.length >= 3 && buttons.length === 0);
  }

  #fill(text) {
    this.input.value = text;
    this.#refreshCounter();
    this.clearError();
    this.input.focus();
    this.input.setSelectionRange(text.length, text.length);
    if (!this.#available) this.#showMatches(false);
  }

  #refreshCounter() {
    const length = this.input.value.length;
    this.counter.textContent = `${length} / ${MAX_LENGTH}`;
    this.counter.hidden = length < COUNTER_THRESHOLD;
  }
}
