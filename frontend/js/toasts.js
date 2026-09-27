/**
 * Error notifications for actions whose origin is no longer on screen (a card's
 * Deploy button, a copy), and the undo notice of a delayed removal. Errors tied to a
 * form are shown inline instead.
 */
import { el } from "./dom.js";
import { icon } from "./icons.js";

const TIMEOUT_MS = 10_000;
const MAX_TOASTS = 3;

export function showToast(message) {
  const region = document.querySelector("#toasts");
  if (!region) return;
  const dismissButton = el("button", {
    className: "icon-button small",
    attrs: { type: "button", "aria-label": "Dismiss" },
  }, [icon("close")]);
  const toast = el("div", { className: "toast", attrs: { role: "alert" } }, [
    icon("alert"),
    el("p", { text: message }),
    dismissButton,
  ]);

  let timer = 0;
  const dismiss = () => {
    window.clearTimeout(timer);
    toast.remove();
  };
  const arm = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(dismiss, TIMEOUT_MS);
  };
  dismissButton.addEventListener("click", dismiss);
  // Never time out while someone is reading or focusing it.
  toast.addEventListener("pointerenter", () => window.clearTimeout(timer));
  toast.addEventListener("pointerleave", arm);
  toast.addEventListener("focusin", () => window.clearTimeout(timer));
  toast.addEventListener("focusout", arm);

  region.append(toast);
  // Notices (an undo countdown) are never evicted: dropping one would silently cancel it.
  const errors = [...region.querySelectorAll(".toast:not(.notice)")];
  for (const old of errors.slice(0, Math.max(0, errors.length - MAX_TOASTS))) old.remove();
  arm();
}

/**
 * A notice with an Undo button and a countdown. Resolves true once the time ran out (go
 * ahead), false when the user undid it: nothing is sent to the server before that.
 */
export function showUndo(message, seconds) {
  return new Promise((resolve) => {
    const region = document.querySelector("#toasts");
    if (!region) {
      resolve(true);
      return;
    }
    let left = seconds;
    const text = el("p", { text: `${message} in ${left} s` });
    const undo = el("button", { className: "button small", text: "Undo", attrs: { type: "button" } });
    const toast = el("div", { className: "toast notice", attrs: { role: "status" } }, [icon("info"), text, undo]);
    const timer = window.setInterval(() => {
      left -= 1;
      if (left > 0) text.textContent = `${message} in ${left} s`;
      else finish(true);
    }, 1000);
    function finish(proceed) {
      window.clearInterval(timer);
      toast.remove();
      resolve(proceed);
    }
    undo.addEventListener("click", () => finish(false));
    region.append(toast);
    undo.focus();
  });
}
