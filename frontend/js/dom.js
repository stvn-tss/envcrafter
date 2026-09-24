/**
 * Tiny DOM builder.
 *
 * SECURITY: text is only ever assigned through `textContent`. Template metadata,
 * LLM-derived messages and container logs are all untrusted: going through
 * `innerHTML` would turn any of them into an XSS vector.
 */
export function el(tag, { className, text, attrs = {} } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  for (const child of children) node.append(child);
  return node;
}
