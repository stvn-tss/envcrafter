/**
 * Line icons drawn on a 24x24 grid, built with the DOM API (no markup strings).
 * They are decorative: the text or aria-label next to them carries the meaning.
 */
const SVG_NS = "http://www.w3.org/2000/svg";

const SHAPES = {
  alert: [["path", { d: "M12 3.5 2.5 20h19L12 3.5Z" }], ["path", { d: "M12 10v4.5M12 17.5v.01" }]],
  arrowDown: [["path", { d: "M12 5v14M6 13l6 6 6-6" }]],
  bell: [["path", { d: "M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15L6 16Z" }], ["path", { d: "M10 20.5a2 2 0 0 0 4 0" }]],
  check: [["path", { d: "m5 12.5 4.5 4.5L19 7.5" }]],
  chevron: [["path", { d: "m6 9 6 6 6-6" }]],
  close: [["path", { d: "M6 6l12 12M18 6 6 18" }]],
  copy: [
    ["rect", { x: 8, y: 8, width: 12, height: 12, rx: 2 }],
    ["path", { d: "M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" }],
  ],
  external: [
    ["path", { d: "M14 4h6v6M20 4l-9 9" }],
    ["path", { d: "M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" }],
  ],
  globe: [
    ["circle", { cx: 12, cy: 12, r: 9 }],
    ["path", { d: "M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3Z" }],
  ],
  info: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 11v5M12 8v.01" }]],
  key: [["circle", { cx: 8, cy: 15, r: 4 }], ["path", { d: "m11 12 9-9M16 7l3 3M14 9l2 2" }]],
  retry: [["path", { d: "M20 12a8 8 0 1 1-2.34-5.66" }], ["path", { d: "M20 4v4h-4" }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "m20 20-3.5-3.5" }]],
  settings: [
    ["path", { d: "M4 7h9M17 7h3M4 17h3M11 17h9" }],
    ["circle", { cx: 15, cy: 7, r: 2 }],
    ["circle", { cx: 9, cy: 17, r: 2 }],
  ],
};

export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  const attrs = {
    class: className,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 2,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
  };
  for (const [key, value] of Object.entries(attrs)) svg.setAttribute(key, String(value));
  for (const [tag, shapeAttrs] of SHAPES[name] ?? []) {
    const shape = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(shapeAttrs)) shape.setAttribute(key, String(value));
    svg.append(shape);
  }
  return svg;
}
