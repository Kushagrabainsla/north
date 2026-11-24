"""Page-script helpers that see into open shadow roots, as the page shows them.

`document.querySelector`, `innerText` and `document.elementFromPoint` stop at a shadow
root. LinkedIn's invitation dialog is in the open shadow root `#interop-outlet`, so north's
page view, query, click check, wait and assert could not see it: a task needed 8 calls to
find the dialog and then sent each invitation with a page script (2026-11-23).

These walk the flat tree as Playwright's locators and aria snapshot do: a host's shadow
root, and a slot's assigned elements. A closed shadow root stays closed: no page script can
open it. Use with `%` formatting: the code has no `%` sign.
"""

DEEP_JS = """
const deepAll = selector => {
  const out = [];
  const visit = root => {
    if (selector) out.push(...root.querySelectorAll(selector));
    for (const el of root.querySelectorAll("*")) {
      if (!selector) out.push(el);
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  visit(document);
  return out;
};
const deepQuery = selector => document.querySelector(selector) || deepAll(selector)[0] || null;
const flatChildren = el => {
  if (el.shadowRoot) return [...el.shadowRoot.children];
  if (el.tagName === "SLOT") {
    const given = el.assignedElements();
    return given.length ? given : [...el.children];
  }
  return [...el.children];
};
const flatParent = node => node.assignedSlot || node.parentNode || node.host || null;
const flatContains = (outer, inner) => {
  for (let node = inner; node; node = flatParent(node)) if (node === outer) return true;
  return false;
};
const deepFromPoint = (x, y) => {
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  return hit;
};
const deepActive = () => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  return el;
};
const shadowText = () => {
  const parts = [];
  for (const host of deepAll().filter(el => el.shadowRoot)) {
    for (const child of host.shadowRoot.children) {
      if (["STYLE", "SCRIPT", "TEMPLATE", "LINK", "SLOT"].includes(child.tagName)) continue;
      const text = child.getClientRects().length ? (child.innerText || "").trim() : "";
      if (text) parts.push(text);
    }
  }
  return parts.join("\\n");
};
const deepText = () => (document.body ? document.body.innerText : "") + "\\n" + shadowText();
"""
