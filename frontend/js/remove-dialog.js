/**
 * Removal confirmation. Removing deletes data volumes, so the dialog lists what
 * goes away and asks for the project name to be typed before it can proceed.
 */
import { el } from "./dom.js";
import { icon } from "./icons.js";

export class RemoveDialog {
  #project = null;

  /** @param {{ onConfirm: (project: string) => Promise<string | null> }} options resolves with an error message */
  constructor(dialog, { onConfirm }) {
    this.dialog = dialog;
    this.title = dialog.querySelector("#remove-title");
    this.items = dialog.querySelector("#remove-items");
    this.label = dialog.querySelector("#remove-confirm-label");
    this.input = dialog.querySelector("#remove-confirm");
    this.submit = dialog.querySelector("#remove-submit");
    this.error = dialog.querySelector("#remove-error");

    for (const button of dialog.querySelectorAll("[data-close]")) {
      button.addEventListener("click", () => dialog.close());
    }
    this.input.addEventListener("input", () => {
      this.submit.disabled = !this.#confirmed();
    });
    dialog.querySelector("#remove-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!this.#confirmed() || this.submit.disabled) return;
      this.error.hidden = true;
      this.submit.disabled = true;
      const error = await onConfirm(this.#project);
      if (error) {
        this.error.replaceChildren(icon("alert"), el("span", { text: error }));
        this.error.hidden = false;
        this.submit.disabled = false;
        return;
      }
      dialog.close();
    });
  }

  /** @param {{ project: string, volumes: string[] | null }} target volumes is null when unknown */
  open({ project, volumes }) {
    this.#project = project;
    this.title.textContent = `Remove ${project}?`;
    const items = [el("li", { text: "its containers and networks" })];
    if (volumes === null) {
      items.push(el("li", { text: "its data volumes" }));
    } else if (volumes.length) {
      items.push(el("li", {}, [
        el("span", { text: "its data volumes: " }),
        ...volumes.flatMap((volume, index) => [
          ...(index ? [el("span", { text: ", " })] : []),
          el("code", { text: volume }),
        ]),
      ]));
    }
    items.push(el("li", { text: "its workspace: compose file and generated secrets" }));
    this.items.replaceChildren(...items);
    this.label.replaceChildren(
      el("span", { text: "Type " }),
      el("strong", { text: project }),
      el("span", { text: " to confirm" }),
    );
    this.input.value = "";
    this.submit.disabled = true;
    this.error.hidden = true;
    this.dialog.showModal();
    this.input.focus();
  }

  #confirmed() {
    return this.input.value.trim() === this.#project;
  }
}
