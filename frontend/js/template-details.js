/**
 * Template details dialog: description, every tool that will be installed
 * (with its exact, pinned image), web access, storage, network exposure and notes,
 * plus an optional project name before deploying.
 * Everything comes from /api/templates and is rendered with textContent only.
 */
import { categoryTag, isVulnerable, monogram } from "./catalog.js";
import { copyButton } from "./clipboard.js";
import { el } from "./dom.js";
import { autoProjectName, projectNameProblem, webUrl } from "./environment.js";
import { icon } from "./icons.js";

const NAME_HINT = "3–32 lowercase letters, digits or hyphens. Leave empty for a random name.";

export class TemplateDetails {
  #template = null;
  #accessSection = null;

  /**
   * @param {{ onDeploy: (template: object, projectName: string | null) => Promise<string | null>,
   *           categoryLabel: (id: string) => string }} options onDeploy resolves with an error message
   */
  constructor(dialog, { onDeploy, categoryLabel }) {
    this.dialog = dialog;
    this.heading = dialog.querySelector("#dialog-heading");
    this.body = dialog.querySelector("#dialog-body");
    this.nameInput = dialog.querySelector("#project-name");
    this.nameHint = dialog.querySelector("#project-name-hint");
    this.error = dialog.querySelector("#dialog-error");
    this.deployButton = dialog.querySelector("#dialog-deploy");
    this.onDeploy = onDeploy;
    this.categoryLabel = categoryLabel;

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
    this.nameInput.placeholder = autoProjectName(template.id);
    this.#hideError();
    this.deployButton.disabled = false;

    this.heading.replaceChildren(
      monogram(template, "large"),
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
      section("Network", networkSummary(template)),
      section("Persistent storage", volumesList(template.volumes)),
      ...(template.access_notes.length
        ? [section("Good to know", el("ul", { className: "notes" }, template.access_notes.map((note) => el("li", { text: note }))))]
        : []),
    );
    this.#refreshName();
    this.dialog.showModal();
    this.body.scrollTop = 0;
    this.deployButton.focus();
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
    const project = chosenName ?? autoProjectName(this.#template.id);
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
        text: "“xxxx” is a random suffix. Set a project name below to choose the address.",
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

function section(title, content) {
  return el("section", { className: "dialog-section" }, [el("h3", { text: title }), content]);
}

function vulnerableCallout() {
  return el("p", { className: "callout" }, [
    icon("alert"),
    el("span", {
      text:
        "Intentionally vulnerable, for security training only. It never gets Internet access " +
        "and is reachable from this machine only.",
    }),
  ]);
}

function componentsTable(components) {
  const rows = components.map((component) =>
    el("tr", {}, [
      el("th", { text: component.name, attrs: { scope: "row" } }),
      el("td", { text: component.role }),
      el("td", {}, [
        el("div", { className: "image-cell" }, [
          el("code", { text: component.image }),
          copyButton(component.image, `Copy the image of ${component.name}`),
        ]),
      ]),
    ]),
  );
  return el("div", { className: "table-wrap" }, [
    el("table", { className: "components" }, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", { text: "Component", attrs: { scope: "col" } }),
          el("th", { text: "Role", attrs: { scope: "col" } }),
          el("th", { text: "Image (pinned)", attrs: { scope: "col" } }),
        ]),
      ]),
      el("tbody", {}, rows),
    ]),
  ]);
}

function networkSummary(template) {
  const text = template.needs_internet
    ? "Own isolated network. Outbound Internet access is enabled for the services that need it."
    : "Own isolated network with no Internet access. Reachable only from this machine.";
  return el("p", { className: "network-note", text, attrs: { "data-internet": String(template.needs_internet) } });
}

function volumesList(volumes) {
  if (!volumes.length) return el("p", { className: "hint", text: "No persistent data: everything resets when removed." });
  return el("ul", { className: "inline-list mono" }, volumes.map((volume) => el("li", { text: volume })));
}
