"""A read of the page that cannot change it: the elements a CSS selector finds, with their text and attributes.

On 2026-11-18 a LinkedIn outreach task sent 102 page scripts (`eval`); 72 of them only read
the page: elements by selector, their text, `aria-label` or `href`, whether they show. North
cannot tell a script that reads from one that clicks, so each went to the approval model, 2
to 7 s each, and some much longer. `query` is that read with north's own fixed script: the
model chooses only what to look for, never the code, so it changes nothing and needs no
approval once the site may be read.
"""

from __future__ import annotations

import json
import re
from typing import Any

_DEFAULT_ITEMS = 20
_MAX_ITEMS = 100
_DEFAULT_CHARS = 300
_MAX_CHARS = 4000
_MAX_ATTRIBUTES = 10
# An attribute name, nothing that could be read as code.
_ATTRIBUTE = re.compile(r"^[A-Za-z_:][-A-Za-z0-9_:.]{0,63}$")

# Values come in as one JSON object, so nothing the model sends is ever part of the code.
_SCRIPT = """(() => {
  const q = %s;
  let found;
  try { found = [...document.querySelectorAll(q.selector)]; }
  catch (error) { return JSON.stringify({error: "not a valid CSS selector: " + q.selector}); }
  const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const needle = q.contains.toLowerCase();
  const matching = found.filter(el => (!q.visible_only || shown(el))
    && (!needle || ((el.innerText || el.textContent || "") + " " + (el.getAttribute("aria-label") || ""))
      .toLowerCase().includes(needle)));
  const items = matching.slice(0, q.limit).map(el => {
    const item = {tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, q.chars),
      visible: shown(el)};
    for (const name of q.attributes) {
      const value = el.getAttribute(name);
      if (value !== null) item[name] = value.slice(0, q.chars);
    }
    return item;
  });
  return JSON.stringify({url: location.href, title: document.title, count: matching.length, items});
})()"""


def _bounded(value: Any, default: int, ceiling: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be a whole number, got {value!r}") from None
    return min(max(number, 1), ceiling)


def query_script(params: dict[str, Any]) -> str:
    """North's fixed page read for a `query` call, with the call's values as data.

    Raises ValueError naming a value the model must send differently.
    """
    selector = str(params.get("selector") or "").strip()
    if not selector:
        raise ValueError("Parameter 'selector' is required for action='query' (a CSS selector, e.g. 'main h1').")
    attributes = params.get("attributes") or []
    if isinstance(attributes, str):
        attributes = [attributes]
    if not isinstance(attributes, list) or len(attributes) > _MAX_ATTRIBUTES:
        raise ValueError(f"'attributes' must be a list of at most {_MAX_ATTRIBUTES} attribute names.")
    names = [str(name).strip() for name in attributes]
    if bad := [name for name in names if not _ATTRIBUTE.match(name)]:
        raise ValueError(f"Not attribute names: {bad}. Pass names like 'href' or 'aria-label'.")
    values = {
        "selector": selector,
        "attributes": names,
        "contains": str(params.get("contains") or ""),
        "visible_only": bool(params.get("visible_only")),
        "limit": _bounded(params.get("limit"), _DEFAULT_ITEMS, _MAX_ITEMS, "limit"),
        "chars": _bounded(params.get("chars"), _DEFAULT_CHARS, _MAX_CHARS, "chars"),
    }
    return _SCRIPT % json.dumps(values)
