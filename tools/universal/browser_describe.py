"""What a browser action is about to do, in facts the approval layer can rule on.

Every mutating browser call was gated, but only by the generic description:
``kind=OTHER`` plus the raw params. A click is a ``uid`` like ``n10``, so neither
the policy, the memory decider nor you could tell "Next" from "Submit
application". Before asking, the tool now looks at the page: its URL and title,
and the role, text and form of the element the action lands on. From those it
says whether pressing it sends something out in your name or spends money.

Looking is read-only - a fixed script reads the page, and ``inspect`` reads the
accessibility tree - and it fails soft: a page north cannot read is described
by the call's own params, and the card says north could not see it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from approval.approvals import Request

# Seconds allowed for looking at the page before asking. Looking must never be
# the slow part of a decision.
_LOOK_TIMEOUT_SECONDS = 5
_TEXT_CHARS = 200

# Words on a control meaning that pressing it sends something out in your name,
# or spends money. A false positive costs one question; a false negative sends
# an application or places an order.
_SENDING_WORDS = frozenset(
    {"submit", "send", "post", "publish", "apply", "reply", "share", "comment", "tweet", "confirm", "invite"}
)
_SPENDING_WORDS = frozenset({"buy", "pay", "purchase", "checkout", "order", "subscribe", "donate", "book"})
_SPENDING_PATH_PARTS = ("checkout", "payment", "billing", "cart")
_SUBMITTING_KEYS = frozenset({"enter", "return"})
_WORD = re.compile(r"[a-z]+")
_SNAPSHOT_LINE = re.compile(r'^(?P<indent>\s*)uid=(?P<uid>\S+)\s+(?P<role>\S+)(?:\s+"(?P<name>.*)")?')

_VERBS = {
    "click": "Click",
    "dblclick": "Double-click",
    "press": "Press",
    "fill": "Fill",
    "type": "Type into",
    "select": "Select in",
    "check": "Check",
    "uncheck": "Uncheck",
    "eval": "Run JavaScript on",
    "goto": "Open",
    "navigate": "Open",
}

# Reads the page and the element a selector, a point or the focus names. Fixed:
# the only inputs are the selector and point, passed as JSON literals.
_PAGE_SCRIPT = """(() => {
  const describe = (el) => {
    if (!el || el === document.body || el === document.documentElement) return null;
    const tag = el.tagName.toLowerCase();
    const buttonish = tag === "button" || (tag === "input" && ["submit", "button", "image"].includes(el.type));
    return {
      role: el.getAttribute("role") || (buttonish ? "button" : tag === "a" ? "link" : tag),
      text: (el.getAttribute("aria-label") || (el.labels && el.labels[0] && el.labels[0].innerText)
        || el.innerText || el.value || el.placeholder || el.name || "").trim().slice(0, %(chars)d),
      inForm: !!el.closest("form"),
    };
  };
  let target = null;
  try {
    const selector = %(selector)s, point = %(point)s;
    if (selector) target = document.querySelector(selector);
    else if (point) target = document.elementFromPoint(point[0], point[1]);
  } catch (e) {}
  return JSON.stringify({
    url: location.href, title: document.title,
    target: describe(target), focused: describe(document.activeElement),
  });
})()"""

Runner = Callable[[list[str], int], Awaitable[Any]]


@dataclass(frozen=True)
class _Element:
    role: str = ""
    text: str = ""
    in_form: bool = False


@dataclass(frozen=True)
class _Page:
    """What north could see before acting. ``seen`` is False when it could not read the page."""

    url: str = ""
    title: str = ""
    target: _Element | None = None
    focused: _Element | None = None
    seen: bool = False


async def describe_browser_call(params: dict[str, Any], command: list[str], run: Runner) -> Request:
    """The approval request for one mutating browser call.

    *command* is chrome-agent plus the call's ``--browser`` session; *run* runs
    one chrome-agent command and returns its result (stdout on ``.stdout``).
    """
    action = str(params.get("action") or ("goto" if params.get("url") else "")).strip().lower()
    page = _Page() if action in ("goto", "navigate") else await _look(action, params, command, run)
    element = _element_for(action, params, page)
    return _request(action, params, page, element)


async def _look(action: str, params: dict[str, Any], command: list[str], run: Runner) -> _Page:
    """Read the page and the target element. Any failure returns an unseen page."""
    try:
        script = _PAGE_SCRIPT % {
            "chars": _TEXT_CHARS,
            "selector": json.dumps(str(params.get("selector") or "")),
            "point": json.dumps(_point(params.get("xy"))),
        }
        page = json.loads(json.loads((await run([*command, "eval", script], _LOOK_TIMEOUT_SECONDS)).stdout)["result"])
        target = _element(page.get("target"))
        if params.get("uid") and action != "press":
            # Plain `inspect` keeps the tree; `--limit` flattens it, and the nesting is what shows a form.
            inspected = await run([*command, "inspect"], _LOOK_TIMEOUT_SECONDS)
            target = _element_in_snapshot(json.loads(inspected.stdout).get("snapshot", ""), str(params["uid"]))
        return _Page(
            url=str(page.get("url") or ""),
            title=str(page.get("title") or ""),
            target=target,
            focused=_element(page.get("focused")),
            seen=True,
        )
    except Exception:
        return _Page()


def _element(raw: Any) -> _Element | None:
    if not isinstance(raw, dict):
        return None
    return _Element(role=str(raw.get("role") or ""), text=str(raw.get("text") or ""), in_form=bool(raw.get("inForm")))


def _element_in_snapshot(snapshot: str, uid: str) -> _Element | None:
    """The element *uid* names in an accessibility snapshot, and whether a form contains it."""
    ancestors: list[tuple[int, str]] = []
    for line in snapshot.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if not match:
            continue
        depth = len(match["indent"])
        ancestors = [(d, role) for d, role in ancestors if d < depth]
        if match["uid"] == uid:
            in_form = any(role == "form" for _, role in ancestors)
            return _Element(role=match["role"], text=(match["name"] or "")[:_TEXT_CHARS], in_form=in_form)
        ancestors.append((depth, match["role"]))
    return None


def _point(raw: Any) -> list[float] | None:
    try:
        x, y = (float(part) for part in str(raw).split(","))
    except (TypeError, ValueError):
        return None
    return [x, y]


def _element_for(action: str, params: dict[str, Any], page: _Page) -> _Element | None:
    """The element the action lands on: the focused one for a key press or typing, else the target."""
    if action == "press" or (action == "type" and not params.get("selector")):
        return page.focused
    return page.target


def _presses(action: str, params: dict[str, Any]) -> bool:
    """Whether the action activates something, as opposed to editing a field."""
    if action in ("click", "dblclick"):
        return True
    return action == "press" and str(params.get("value") or "Enter").strip().lower() in _SUBMITTING_KEYS


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _request(action: str, params: dict[str, Any], page: _Page, element: _Element | None) -> Request:
    from approval.approvals import Request
    from approval.policy import Action, ActionKind

    label = _label(params, element)
    presses = _presses(action, params)
    words = _words(element.text) if element else set()
    submits_form = bool(element and element.in_form and (element.role == "button" or action == "press"))
    sends = presses and (bool(words & _SENDING_WORDS) or submits_form)
    at_checkout = any(part in urlparse(page.url).path.lower() for part in _SPENDING_PATH_PARTS)
    spends = presses and (bool(words & _SPENDING_WORDS) or (at_checkout and (sends or submits_form)))
    attaches = bool(params.get("connect") or params.get("copy_cookies"))
    location = _location(page.url or str(params.get("url") or ""))

    headline = _headline(action, params, label)
    message = _message(action, params, page, element, headline, sends=sends, spends=spends, attaches=attaches)
    return Request(
        action=Action(
            agent="browser",
            kind=ActionKind.BROWSER,
            summary=f"browser {action} {label} on {location or 'the current page'}",
            operation=action,
            command=location,
            args=label,
            reaches_third_party=sends,
            spends_money=spends,
            details=message,
        ),
        title=f"Browser - {headline}",
        message=message,
    )


def _label(params: dict[str, Any], element: _Element | None) -> str:
    """What the action is on, in the words a person would use."""
    if element and element.text:
        return f'"{element.text}"'
    for key in ("url", "selector", "uid", "xy"):
        if params.get(key):
            return str(params[key])
    return ""


def _headline(action: str, params: dict[str, Any], label: str) -> str:
    """The action in a few words: 'Click "Submit application"', 'Press Enter in "Name"'."""
    if action == "press":
        key = params.get("value") or "Enter"
        return f"Press {key} in {label}" if label else f"Press {key}"
    return f"{_VERBS.get(action, action.title())} {label}".strip()


def _location(url: str) -> str:
    """Scheme, host and path: stable across visits, unlike a query string with a session in it."""
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _message(
    action: str,
    params: dict[str, Any],
    page: _Page,
    element: _Element | None,
    headline: str,
    *,
    sends: bool,
    spends: bool,
    attaches: bool,
) -> str:
    lines = [f"**{headline}**"]
    if element and element.role:
        lines[0] += f" ({element.role}, in a form)" if element.in_form else f" ({element.role})"
    if action in ("fill", "type", "select") and "value" in params:
        lines.append(f'with "{params["value"]}"')
    if action == "eval":
        lines.append(f"```js\n{params.get('js', '')}\n```")
    if page.url:
        lines.append(f"on **{page.title}** - {page.url}" if page.title else f"on {page.url}")
    elif not page.seen and action not in ("goto", "navigate"):
        lines.append("north could not read the page, so it cannot say what this lands on.")
    if attaches:
        lines.append("This uses your own browser: your logged-in sessions and cookies.")
    if sends:
        lines.append("This sends something to someone else in your name.")
    if spends:
        lines.append("This may spend money.")
    return "\n".join(lines)
