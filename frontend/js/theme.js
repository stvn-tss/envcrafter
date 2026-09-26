/*
 * Theme chosen in Settings ("light" or "dark"; nothing = follow the system). A classic,
 * render-blocking script in <head>, on purpose: it runs before the first paint, so a pinned
 * theme never flashes the other one. Storage may be unavailable (private mode): the page
 * then simply follows the system.
 */
try {
  const theme = localStorage.getItem("envcrafter.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  /* storage unavailable */
}
