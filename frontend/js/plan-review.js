/**
 * Review of an AI plan before anything is deployed. Everything shown comes from
 * GET /api/plans/{id}; LLM-written text (title, summary, explanation, purposes) is untrusted
 * and rendered with textContent only. Deploying sends the plan id, never the stack.
 */
import { componentsTable, networkSummary, section, volumesList, vulnerableCallout } from "./dialog-parts.js";
import { el } from "./dom.js";
import { nextProjectName, projectNameProblem, webUrl } from "./environment.js";
import { icon } from "./icons.js";

const NAME_HINT = "3–32 lowercase letters, digits or hyphens. Leave empty to use the name shown.";

export class PlanReview {
  #plan = null;
  #access = null;

  /**
   * @param {{ onDeploy: (plan: object, projectName: string | null) => Promise<string | null>,
   *           takenNames: () => Set<string> }} options
   */
  constructor(dialog, { onDeploy, takenNames }) {
    this.dialog = dialog;
    this.heading = dialog.querySelector("#plan-heading");
    this.body = dialog.querySelector("#plan-body");
    this.nameInput = dialog.querySelector("#plan-project-name");
    this.nameHint = dialog.querySelector("#plan-project-hint");
    this.error = dialog.querySelector("#plan-error");
    this.deployButton = dialog.querySelector("#plan-deploy");
    this.onDeploy = onDeploy;
    this.takenNames = takenNames;
    const close = () => dialog.close();
    dialog.querySelector("#plan-close").addEventListener("click", close);
    dialog.querySelector("#plan-cancel").addEventListener("click", close);
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) close();
    });
    this.deployButton.addEventListener("click", () => this.#deploy());
    this.nameInput.addEventListener("input", () => this.#refreshName());
    this.nameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") this.#deploy();
    });
  }

  open(plan) {
    this.#plan = plan;
    const services = Array.isArray(plan.services) ? plan.services : [];
    this.heading.replaceChildren(el("div", {}, [
      el("h2", { text: String(plan.title ?? "AI plan"), attrs: { id: "plan-title" } }),
      el("span", { className: "category-tag", text: plan.decision === "template" ? "Catalog template chosen by the AI" : "Custom stack assembled by the AI" }),
    ]));
    this.#access = el("div");
    const expires = new Date(plan.expires_at);
    this.body.replaceChildren(
      el("p", { className: "dialog-description", text: String(plan.summary ?? "") }),
      ...(services.some((service) => service.vulnerable) ? [vulnerableCallout()] : []),
      ...(plan.explanation ? [section("Why this plan", el("p", { className: "plan-explanation", text: String(plan.explanation) }))] : []),
      section("What gets installed", componentsTable(services.map((service) => ({ name: service.name, role: service.purpose ?? "—", image: service.image })))),
      section("Web access", this.#access),
      section("Network", networkSummary(plan.needs_internet === true)),
      section("Persistent storage", volumesList(Array.isArray(plan.volumes) ? plan.volumes : [])),
      ...(plan.secrets > 0 ? [section("Passwords", el("p", { text: `${plan.secrets} random password(s) will be generated and stored only in the environment's workspace.` }))] : []),
      el("p", { className: "hint", text: Number.isNaN(expires.getTime()) ? "" : `This plan stays available until ${expires.toLocaleTimeString()}.` }),
    );
    this.nameInput.value = "";
    this.nameInput.placeholder = nextProjectName(plan.template_id ?? "env", this.takenNames());
    this.#hideError();
    this.#refreshName();
    this.dialog.showModal();
    this.body.scrollTop = 0;
    this.deployButton.focus();
  }

  #refreshName() {
    const name = this.nameInput.value.trim();
    const problem = projectNameProblem(name);
    this.nameInput.setAttribute("aria-invalid", String(problem !== null));
    this.nameHint.textContent = problem ?? NAME_HINT;
    this.nameHint.toggleAttribute("data-invalid", problem !== null);
    this.deployButton.disabled = problem !== null;
    const project = name && !problem ? name : this.nameInput.placeholder;
    const exposed = (this.#plan?.services ?? []).filter((service) => typeof service.web_access === "string");
    this.#access.replaceChildren(exposed.length
      ? el("ul", { className: "access-list" }, exposed.map((service) => el("li", {}, [
        el("span", { className: "url-name", text: service.name }),
        el("code", { text: webUrl(service.web_access, project) }),
      ])))
      : el("p", { className: "hint", text: "No web interface." }));
  }

  async #deploy() {
    const name = this.nameInput.value.trim();
    if (!this.#plan || projectNameProblem(name) !== null || this.deployButton.disabled) return;
    this.#hideError();
    this.deployButton.disabled = true;
    const error = await this.onDeploy(this.#plan, name || null);
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
