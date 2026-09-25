/** Template catalog: cards, category filters and free-text search. */
import { el } from "./dom.js";
import { icon } from "./icons.js";

/** The allow-list flags intentionally vulnerable images; the API exposes it per template. */
export function isVulnerable(template) {
  return template.vulnerable === true;
}

export function formatMegabytes(mb) {
  return mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${mb} MB`;
}

function formatSeconds(seconds) {
  return seconds >= 90 ? `${Math.round(seconds / 60)} min` : `${seconds} s`;
}

/** "454 MB download · 350 MB RAM · first start ~2 min": approximate, from the manifest. */
export function footprintItems(template) {
  const footprint = template.footprint;
  if (!footprint) return [];
  return [
    `${formatMegabytes(footprint.download_mb)} download`,
    `${formatMegabytes(footprint.memory_mb)} RAM`,
    `first start ~${formatSeconds(footprint.first_start_seconds)}`,
  ];
}

const LOGO_URL = /^\/api\/templates\/[a-z0-9-]+\/logo$/;

/** The application logo when the template ships one, else the category monogram. */
export function appIcon(template, size = "") {
  const fallback = monogram(template, size);
  if (typeof template.logo_url !== "string" || !LOGO_URL.test(template.logo_url)) return fallback;
  const pixels = size === "large" ? "48" : "40";
  const image = el("img", {
    className: size ? `app-logo ${size}` : "app-logo",
    attrs: { src: template.logo_url, alt: "", width: pixels, height: pixels, decoding: "async" },
  });
  image.addEventListener("error", () => image.replaceWith(fallback), { once: true });
  return image;
}

/** Initials in the category color, standing in for the application logo. */
export function monogram(template, size = "") {
  const words = template.name.split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : template.name.slice(0, 2);
  return el("span", {
    className: size ? `monogram ${size}` : "monogram",
    text: letters.toUpperCase(),
    attrs: { "aria-hidden": "true", "data-category": template.category },
  });
}

export function categoryTag(categoryId, label) {
  return el("span", { className: "category-tag", text: label, attrs: { "data-category": categoryId } });
}

/** Security-relevant traits, shown on the card before anyone opens the details. */
export function templateFlags(template) {
  const flags = [];
  if (isVulnerable(template)) {
    flags.push(el("li", { className: "flag", attrs: { "data-flag": "vulnerable" } }, [
      icon("alert"),
      el("span", { text: "Intentionally vulnerable" }),
    ]));
  }
  if (template.needs_internet) {
    flags.push(el("li", { className: "flag", attrs: { "data-flag": "internet" } }, [
      icon("globe"),
      el("span", { text: "Internet access" }),
    ]));
  }
  return flags.length ? el("ul", { className: "flags", attrs: { "aria-label": "Traits" } }, flags) : null;
}

export class Catalog {
  #category = "all";
  #entries = []; // { template, card, haystack }

  /**
   * @param {HTMLElement} root
   * @param {{ onDetails: (template: object) => void, onDeploy: (template: object) => void,
   *           categoryLabel: (id: string) => string }} options
   */
  constructor(root, { onDetails, onDeploy, categoryLabel }) {
    this.grid = root.querySelector("#template-grid");
    this.filters = root.querySelector("#category-filters");
    this.search = root.querySelector("#template-search");
    this.empty = root.querySelector("#catalog-empty");
    this.onDetails = onDetails;
    this.onDeploy = onDeploy;
    this.categoryLabel = categoryLabel;

    this.search.addEventListener("input", () => this.#apply());
    this.filters.addEventListener("click", (event) => {
      const chip = event.target.closest("button[data-category]");
      if (chip) this.#select(chip.dataset.category);
    });
    root.querySelector("#catalog-reset").addEventListener("click", () => {
      this.search.value = "";
      this.#select("all");
      this.search.focus();
    });
  }

  render(categories, templates) {
    const chips = [{ id: "all", label: "All" }, ...categories].map(({ id, label }) =>
      el("button", {
        className: "chip",
        text: label,
        attrs: { type: "button", "data-category": id, "aria-pressed": id === this.#category },
      }),
    );
    this.filters.replaceChildren(...chips);

    this.#entries = templates.map((template) => ({
      template,
      card: this.#card(template),
      haystack: [
        template.name,
        template.summary,
        this.categoryLabel(template.category),
        ...template.tags,
        ...template.components.map((component) => component.name),
      ].join(" ").toLowerCase(),
    }));
    this.grid.replaceChildren(...this.#entries.map((entry) => entry.card));
    this.grid.setAttribute("aria-busy", "false");
    this.#apply();
  }

  showError(message) {
    this.grid.replaceChildren(el("p", { className: "hint", text: message }));
    this.grid.setAttribute("aria-busy", "false");
  }

  /** "2 running" next to the category of each template with live environments. */
  setRunningCounts(counts) {
    for (const { template, card } of this.#entries) {
      const count = counts.get(template.id) ?? 0;
      let chip = card.querySelector(".running-count");
      if (!count) {
        chip?.remove();
        continue;
      }
      if (!chip) {
        chip = el("span", { className: "running-count" });
        card.querySelector(".card-title").append(chip);
      }
      chip.textContent = `${count} running`;
    }
  }

  #select(category) {
    this.#category = category;
    for (const chip of this.filters.children) {
      chip.setAttribute("aria-pressed", String(chip.dataset.category === category));
    }
    this.#apply();
  }

  #apply() {
    const terms = this.search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    let visible = 0;
    for (const { template, card, haystack } of this.#entries) {
      const shown =
        (this.#category === "all" || template.category === this.#category) &&
        terms.every((term) => haystack.includes(term));
      card.hidden = !shown;
      if (shown) visible += 1;
    }
    this.empty.hidden = visible > 0 || this.#entries.length === 0;
  }

  #card(template) {
    const detailsButton = el("button", {
      className: "button",
      text: "Details",
      attrs: { type: "button", "aria-label": `Details of ${template.name}` },
    });
    const deployButton = el("button", {
      className: "button primary",
      text: "Deploy",
      attrs: { type: "button", "data-deploy": "", "aria-label": `Deploy ${template.name}` },
    });
    detailsButton.addEventListener("click", () => this.onDetails(template));
    deployButton.addEventListener("click", () => this.onDeploy(template));

    const flags = templateFlags(template);
    const card = el("article", { className: "template-card", attrs: { "data-category": template.category } }, [
      el("div", { className: "card-head" }, [
        appIcon(template),
        el("div", { className: "card-title" }, [
          el("h3", { text: template.name }),
          categoryTag(template.category, this.categoryLabel(template.category)),
        ]),
      ]),
      el("p", { className: "template-description", text: template.summary }),
      el(
        "ul",
        { className: "inline-list", attrs: { "aria-label": "Components" } },
        template.components.map((component) => el("li", { text: component.name })),
      ),
      el("ul", { className: "inline-list footprint", attrs: { "aria-label": "Approximate footprint" } }, footprintItems(template).map((text) => el("li", { text }))),
      ...(flags ? [flags] : []),
      el("div", { className: "card-actions" }, [detailsButton, deployButton]),
    ]);
    // The whole card opens the details (mouse); the Details button covers keyboard users.
    card.addEventListener("click", (event) => {
      if (!event.target.closest("button")) this.onDetails(template);
    });
    return card;
  }
}
