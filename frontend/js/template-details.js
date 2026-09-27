/**
 * Template details dialog: description, every tool that will be installed
 * (with its exact, pinned image), web access, storage, network exposure and notes,
 * plus an optional project name before deploying.
 * Everything comes from /api/templates and is rendered with textContent only.
 */
import { appIcon, categoryTag, footprintItems, isVulnerable } from "./catalog.js";
import { componentsTable, networkSummary, section, volumesList, vulnerableCallout } from "./dialog-parts.js";
import { el } from "./dom.js";
import { nextProjectName, projectNameProblem, webUrl } from "./environment.js";
import { icon } from "./icons.js";
import { readinessItems } from "./readiness.js";

const NAME_HINT = "3–32 lowercase letters, digits or hyphens. Leave empty to use the name shown.";

export class TemplateDetails {
  #template = null;
  #accessSection = null;
  #readiness = null;
  #readinessRequest = 0;

  /**
   * @param {{ onDeploy: (template: object, projectName: string | null) => Promise<string | null>,
   *           categoryLabel: (id: string) => string,
   *           fetchReadiness: (templateId: string) => Promise<object>,
   *           takenNames: () => Set<string> }} options
   *   onDeploy resolves with an error message
   */
  constructor(dialog, { onDeploy, categoryLabel, fetchReadiness, takenNames }) {
    this.dialog = dialog;
    this.heading = dialog.querySelector("#dialog-heading");
    this.body = dialog.querySelector("#dialog-body");
    this.nameInput = dialog.querySelector("#project-name");
    this.nameHint = dialog.querySelector("#project-name-hint");
    this.error = dialog.querySelector("#dialog-error");
    this.deployButton = dialog.querySelector("#dialog-deploy");
    this.onDeploy = onDeploy;
    this.categoryLabel = categoryLabel;
    this.fetchReadiness = fetchReadiness;
    this.takenNames = takenNames;

    const close = () => dialog.close();
    dialog.querySelector("#dialog-close").addEventListener("click", close);
    dialog.querySelector("#dialog-cancel").addEventListener("click", close);
    this.deployButton.addEventListener("click", () => this.#deploy());
    this.nameInput.addEventListener("input", () => this.#refreshName());
    this.nameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") this.#deploy();
    });
    // Click on the backdrop (outside the dialog box) closes it.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) close();
    });
  }

  open(template) {
    this.#template = template;
    this.nameInput.value = "";
    this.nameInput.placeholder = nextProjectName(template.id, this.takenNames());
    this.#hideError();
    this.deployButton.disabled = false;

    this.heading.replaceChildren(
      appIcon(template, "large"),
      el("div", {}, [
        el("h2", { text: template.name, attrs: { id: "dialog-title" } }),
        categoryTag(template.category, this.categoryLabel(template.category)),
      ]),
    );
    this.#accessSection = el("div");
    this.body.replaceChildren(
      el("p", { className: "dialog-description", text: template.description }),
      ...(isVulnerable(template) ? [vulnerableCallout()] : []),
      section("What gets installed", componentsTable(template.components)),
      section("Web access", this.#accessSection),
      section("Network", networkSummary(template.needs_internet)),
      section("Persistent storage", volumesList(template.volumes)),
      section("Before you deploy", this.#readiness = el("div", { className: "readiness", attrs: { "aria-live": "polite" } }, [
        el("p", { className: "hint", text: "Checking this machine…" }),
      ])),
      ...(template.access_notes.length
        ? [section("Good to know", el("ul", { className: "notes" }, template.access_notes.map((note) => el("li", { text: note }))))]
        : []),
    );
    this.#refreshName();
    this.dialog.showModal();
    this.body.scrollTop = 0;
    this.deployButton.focus();
    this.#loadReadiness(template);
  }

  /** The capacity check of this machine, or the manifest footprint if it cannot be read. */
  async #loadReadiness(template) {
    const request = ++this.#readinessRequest;
    let readiness = null;
    try {
      readiness = await this.fetchReadiness(template.id);
    } catch {
      readiness = null;
    }
    if (request !== this.#readinessRequest || this.#template !== template) return; // another template opened
    this.#readiness.replaceChildren(...(readiness
      ? readinessItems(readiness)
      : [
          el("ul", { className: "inline-list" }, footprintItems(template).map((text) => el("li", { text }))),
          el("p", { className: "hint access-note", text: "Approximate, measured on linux/amd64. First start assumes images are already downloaded." }),
        ]));
  }

  #projectName() {
    return this.nameInput.value.trim();
  }

  #refreshName() {
    const name = this.#projectName();
    const problem = projectNameProblem(name);
    this.nameInput.setAttribute("aria-invalid", String(problem !== null));
    this.nameHint.textContent = problem ?? NAME_HINT;
    this.nameHint.toggleAttribute("data-invalid", problem !== null);
    this.deployButton.disabled = problem !== null;
    this.#renderAccess(name && !problem ? name : null);
  }

  #renderAccess(chosenName) {
    const project = chosenName ?? this.nameInput.placeholder;
    const exposed = this.#template.components.filter((component) => component.web_access);
    const children = [
      el(
        "ul",
        { className: "access-list" },
        exposed.map((component) =>
          el("li", {}, [
            el("span", { className: "url-name", text: component.name }),
            el("code", { text: webUrl(component.web_access, project) }),
          ]),
        ),
      ),
    ];
    if (!chosenName) {
      children.push(el("p", {
        className: "hint access-note",
        text: "The next free name. Set a project name below to choose another address.",
      }));
    }
    this.#accessSection.replaceChildren(...children);
  }

  async #deploy() {
    const name = this.#projectName();
    if (!this.#template || projectNameProblem(name) !== null || this.deployButton.disabled) return;
    this.#hideError();
    this.deployButton.disabled = true;
    const error = await this.onDeploy(this.#template, name || null);
    this.deployButton.disabled = false;
    if (error) {
      this.error.replaceChildren(icon("alert"), el("span", { text: error }));
      this.error.hidden = false;
      return;
    }
    this.dialog.close();
  }

  #hideError() {
    this.error.hidden = true;
    this.error.replaceChildren();
  }
}
