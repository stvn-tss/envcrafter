/** Clipboard helpers. The Clipboard API needs a secure context: localhost and *.localhost are. */
import { el } from "./dom.js";
import { icon } from "./icons.js";
import { showToast } from "./toasts.js";

const CONFIRMATION_MS = 1500;

/** Resolves to true when the text reached the clipboard. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Small icon button that copies `text`, then shows a check mark for a moment. */
export function copyButton(text, label) {
  const button = el("button", {
    className: "icon-button small",
    attrs: { type: "button", "aria-label": label, title: label },
  }, [icon("copy")]);
  let timer = 0;
  button.addEventListener("click", async (event) => {
    event.stopPropagation(); // e.g. inside a clickable card
    if (!(await copyText(text))) {
      showToast("Could not copy to the clipboard. Select the text instead.");
      return;
    }
    button.replaceChildren(icon("check"));
    button.dataset.copied = "";
    button.setAttribute("aria-label", "Copied");
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      button.replaceChildren(icon("copy"));
      delete button.dataset.copied;
      button.setAttribute("aria-label", label);
    }, CONFIRMATION_MS);
  });
  return button;
}
