/**
 * Readiness views: the capacity check shown before deploying a template, and the
 * first-run checklist (Docker, reverse proxy, memory, disk, AI key).
 * Server data is untrusted: numbers are range-checked, text goes through textContent.
 */
import { el } from "./dom.js";
import { icon } from "./icons.js";

const CHECK_LABELS = { docker: "Docker", proxy: "Reverse proxy", memory: "Memory", disk: "Disk", llm: "AI plans" };
const CHECK_ICONS = { ok: "check", warning: "alert", error: "alert", info: "info" };

const count = (value) => (Number.isInteger(value) && value >= 0 ? value : null);

/** 750 -> "750 MB", 1048 -> "1.0 GB". */
export function formatSize(mb) {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
}

function duration(seconds) {
  return seconds < 60 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

function line(text, warning = false) {
  return el("li", { attrs: warning ? { "data-warning": "" } : {} }, [
    icon(warning ? "alert" : "check"),
    el("span", { text }),
  ]);
}

/** Lines of the capacity check, for the template dialog. */
export function readinessItems(readiness) {
  const total = count(readiness?.images_total) ?? 0;
  const missing = count(readiness?.images_missing) ?? 0;
  const download = count(readiness?.download_mb) ?? 0;
  const memory = count(readiness?.memory_mb);
  const available = count(readiness?.resources?.memory_available_mb);
  const disk = count(readiness?.resources?.disk_free_mb);
  const start = count(readiness?.first_start_seconds);
  const warnings = new Set(Array.isArray(readiness?.warnings) ? readiness.warnings : []);
  const images = (n) => (n === 1 ? "image" : "images");

  const items = [
    line(missing === 0
      ? `All ${total} ${images(total)} ${total === 1 ? "is" : "are"} already downloaded`
      : `About ${formatSize(download)} to download (${missing} of ${total} ${images(total)})`),
  ];
  if (memory !== null) {
    const short = warnings.has("memory") && available !== null;
    items.push(line(
      short
        ? `Needs about ${formatSize(memory)} of memory, only ${formatSize(available)} available: stop another environment first`
        : `Needs about ${formatSize(memory)} of memory` + (available !== null ? ` · ${formatSize(available)} available` : ""),
      short,
    ));
  }
  if (disk !== null) {
    const where = readiness?.resources?.disk_is_virtual === true ? "in Docker Desktop's disk image" : "on Docker's disk";
    items.push(line(
      warnings.has("disk") ? `Only ${formatSize(disk)} free ${where}: this download may not fit` : `${formatSize(disk)} free ${where}`,
      warnings.has("disk"),
    ));
  }
  if (start !== null) {
    items.push(line(missing ? `Ready about ${duration(start)} after the download` : `Ready in about ${duration(start)}`));
  }
  return [el("ul", { className: "readiness-list" }, items)];
}

/** The checklist rows of GET /api/system. */
export function checkItems(system) {
  const checks = Array.isArray(system?.checks) ? system.checks : [];
  return checks
    .filter((check) => CHECK_LABELS[check?.name] && typeof check.detail === "string")
    .map((check) => {
      const status = CHECK_ICONS[check.status] ? check.status : "info";
      return el("li", { attrs: { "data-status": status } }, [
        icon(CHECK_ICONS[status]),
        el("strong", { text: CHECK_LABELS[check.name] }),
        el("span", { text: check.detail.slice(0, 240) }),
      ]);
    });
}

/** True when a prerequisite of real deployments is broken (Docker, reverse proxy). */
export function hasError(system) {
  return Array.isArray(system?.checks) && system.checks.some((check) => check?.status === "error");
}
