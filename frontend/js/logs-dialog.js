/**
 * Live container logs of one service (WS /ws/environments/{project}/logs).
 * Log lines are untrusted text: rendered with textContent only, and the DOM is capped.
 */
import { copyText } from "./clipboard.js";
import { el } from "./dom.js";
import { showToast } from "./toasts.js";

const TAIL = 200;
const MAX_LINES = 1000;
const RETRY_MS = 2000;
const FOLLOW_THRESHOLD_PX = 24;
const SERVICE = /^[a-z][a-z0-9-]{0,39}$/;

const CLOSE_MESSAGES = {
  4404: "This service is not part of the environment any more.",
  1013: "Too many log viewers are open. Close one, then reconnect.",
  1008: "Logs are not available for this service.",
};

export class LogsDialog {
  #socket = null;
  #project = null;
  #service = null;
  #lines = [];
  #retryTimer = 0;

  constructor(dialog) {
    this.dialog = dialog;
    this.title = dialog.querySelector("#logs-title");
    this.select = dialog.querySelector("#logs-service");
    this.status = dialog.querySelector("#logs-status");
    this.retry = dialog.querySelector("#logs-retry");
    this.list = dialog.querySelector("#logs-list");
    this.jump = dialog.querySelector("#logs-jump");

    for (const button of dialog.querySelectorAll("[data-close]")) button.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => this.#disconnect());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    this.select.addEventListener("change", () => this.#connect(this.select.value));
    this.retry.addEventListener("click", () => this.#connect(this.#service));
    dialog.querySelector("#logs-copy").addEventListener("click", () => this.#copy());
    this.list.addEventListener("scroll", () => {
      this.jump.hidden = this.#atBottom();
    });
    this.jump.addEventListener("click", () => {
      this.list.scrollTop = this.list.scrollHeight;
      this.jump.hidden = true;
    });
  }

  /** @param {{ project: string, services?: {service: string, name: string}[], urls?: {service: string}[] }} environment */
  open(environment) {
    this.#project = environment.project;
    this.title.textContent = `Logs of ${environment.project}`;
    const services = (Array.isArray(environment.services) ? environment.services : [])
      .filter((service) => SERVICE.test(service?.service ?? ""));
    this.select.replaceChildren(
      ...services.map((service) => el("option", {
        text: `${service.name ?? service.service} (${service.service})`,
        attrs: { value: service.service },
      })),
    );
    const main = Array.isArray(environment.urls) ? environment.urls[0]?.service : undefined;
    this.select.value = services.some((service) => service.service === main) ? main : services[0]?.service ?? "";
    this.dialog.showModal();
    if (this.select.value) this.#connect(this.select.value);
    else this.#setStatus("This environment has no service to read.");
  }

  #connect(service) {
    this.#disconnect();
    this.#service = service;
    this.#lines = [];
    this.list.replaceChildren();
    this.retry.hidden = true;
    this.#setStatus("Connecting…");
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${scheme}://${window.location.host}/ws/environments/${encodeURIComponent(this.#project)}`
      + `/logs?service=${encodeURIComponent(service)}&tail=${TAIL}`;
    const socket = new WebSocket(url);
    this.#socket = socket;
    socket.addEventListener("open", () => {
      if (this.#socket === socket) this.#setStatus("Live");
    });
    socket.addEventListener("message", (message) => {
      if (this.#socket === socket) this.#onFrame(message.data);
    });
    socket.addEventListener("close", (event) => {
      if (this.#socket === socket) this.#onClose(event.code);
    });
  }

  #disconnect() {
    window.clearTimeout(this.#retryTimer);
    const socket = this.#socket;
    this.#socket = null; // late events of the old socket are ignored
    socket?.close(1000, "viewer closed");
  }

  #onFrame(data) {
    let frame;
    try {
      frame = JSON.parse(data);
    } catch {
      return;
    }
    if (frame?.type === "line" && typeof frame.text === "string") this.#append(frame);
    else if (frame?.type === "end") this.#setStatus(typeof frame.message === "string" ? frame.message : "Log stream ended.");
  }

  #onClose(code) {
    this.#socket = null;
    if (!this.dialog.open || code === 1000) return; // closed by us, or the stream ended
    if (CLOSE_MESSAGES[code]) {
      this.#setStatus(CLOSE_MESSAGES[code]);
      this.retry.hidden = code !== 1013;
      return;
    }
    this.#setStatus("Connection lost. Reconnecting…");
    this.#retryTimer = window.setTimeout(() => this.#connect(this.#service), RETRY_MS);
  }

  #append(frame) {
    const time = frame.timestamp ? new Date(frame.timestamp) : null;
    const label = time && !Number.isNaN(time.getTime()) ? time.toLocaleTimeString() : "--:--:--";
    const follow = this.#atBottom();
    this.list.append(el("li", { className: "log-entry" }, [
      el("time", { text: label, attrs: { datetime: String(frame.timestamp ?? "") } }),
      el("span", { text: frame.text }),
    ]));
    this.#lines.push(`${label}  ${frame.text}`);
    while (this.list.childElementCount > MAX_LINES) this.list.firstElementChild.remove();
    if (this.#lines.length > MAX_LINES) this.#lines.shift();
    if (follow) this.list.scrollTop = this.list.scrollHeight;
    else this.jump.hidden = false;
  }

  #atBottom() {
    return this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < FOLLOW_THRESHOLD_PX;
  }

  #setStatus(text) {
    this.status.textContent = text;
  }

  async #copy() {
    if (!(await copyText(this.#lines.join("\n")))) showToast("Could not copy the logs. Select the lines instead.");
  }
}
