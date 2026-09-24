/**
 * Error notifications for actions whose origin is no longer on screen (a card's
 * Deploy button, a copy). Errors tied to a form are shown inline instead.
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
  while (region.childElementCount > MAX_TOASTS) region.firstElementChild.remove();
  arm();
}
