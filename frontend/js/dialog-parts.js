/**
 * Building blocks shared by the template details dialog and the plan review dialog:
 * a titled section, the vulnerable-image callout, the components table, the network
 * summary and the volumes list. Everything is rendered with textContent only.
 */
import { copyButton } from "./clipboard.js";
import { el } from "./dom.js";
import { icon } from "./icons.js";

export function section(title, content) {
  return el("section", { className: "dialog-section" }, [el("h3", { text: title }), content]);
}

export function vulnerableCallout() {
  return el("p", { className: "callout" }, [
    icon("alert"),
    el("span", {
      text:
        "Intentionally vulnerable, for security training only. It never gets Internet access " +
        "and is reachable from this machine only.",
    }),
  ]);
}

/** @param {{ name: string, role: string, image: string }[]} rows */
export function componentsTable(rows) {
  const trs = rows.map((row) =>
    el("tr", {}, [
      el("th", { text: row.name, attrs: { scope: "row" } }),
      el("td", { text: row.role }),
      el("td", {}, [
        el("div", { className: "image-cell" }, [
          el("code", { text: row.image }),
          copyButton(row.image, `Copy the image of ${row.name}`),
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
      el("tbody", {}, trs),
    ]),
  ]);
}

export function networkSummary(needsInternet) {
  const text = needsInternet
    ? "Own isolated network. Outbound Internet access is enabled for the services that need it."
    : "Own isolated network with no Internet access. Reachable only from this machine.";
  return el("p", { className: "network-note", text, attrs: { "data-internet": String(needsInternet) } });
}

export function volumesList(volumes) {
  if (!volumes.length) return el("p", { className: "hint", text: "No persistent data: everything resets when removed." });
  return el("ul", { className: "inline-list mono" }, volumes.map((volume) => el("li", { text: volume })));
}
