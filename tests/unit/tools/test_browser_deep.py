"""North's page scripts see into open shadow roots, as the page shows them.

LinkedIn's invitation dialog is in the open shadow root `#interop-outlet`. 7 of north's 8
page reads could not see it, so a task needed 8 calls to find it and then sent each
invitation with a page script (2026-11-23). These run the helpers on a small fake page in
Node: a light-DOM button, and a host whose shadow root holds a dialog with a note and a
Send button, and a slot that shows one of the host's own children.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.universal.browser_deep import DEEP_JS

_FAKE_PAGE = """
const matches = (el, selector) => selector === "*" || el.tagName === selector.toUpperCase()
  || selector === "#" + el.id || selector === '[data-north-uid="' + el.uid + '"]';
const node = (tagName, props = {}, children = []) => {
  const el = {tagName, id: props.id || "", uid: props.uid || "", innerText: props.text || "", children,
    shadowRoot: null, assignedSlot: null, parentNode: null, host: null,
    getClientRects: () => [1], assignedElements: () => props.assigned || []};
  for (const child of children) child.parentNode = el;
  el.querySelectorAll = selector => {
    const out = [];
    const visit = parent => { for (const child of parent.children) { if (matches(child, selector)) out.push(child);
      visit(child); } };
    visit(el);
    return out;
  };
  el.querySelector = selector => el.querySelectorAll(selector)[0] || null;
  return el;
};
const shown = node("P", {id: "kept", text: "Shown through the slot"});
const hidden = node("P", {id: "dropped", text: "Not assigned to any slot"});
const send = node("BUTTON", {id: "send", uid: "n9", text: "Send invitation"});
const note = node("TEXTAREA", {id: "note"});
const slot = node("SLOT", {assigned: [shown]});
const dialog = node("DIV", {id: "dialog", text: "Add a note to your invitation"}, [note, send, slot]);
const host = node("DIV", {id: "interop-outlet"}, [shown, hidden]);
const root = node("#shadow-root", {}, [dialog]);
root.host = host;
dialog.parentNode = root;
host.shadowRoot = root;
shown.assignedSlot = slot;
const light = node("BUTTON", {id: "light", text: "Connect"});
const body = node("BODY", {text: "Home Connect"}, [light, host]);
root.elementFromPoint = () => send;
globalThis.document = node("#document", {}, [body]);
document.body = body;
document.elementFromPoint = () => host;
document.activeElement = host;
root.activeElement = note;
"""


def _run(tmp_path: Path, expression: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node to run the page script")
    script = tmp_path / "deep.js"
    script.write_text(_FAKE_PAGE + DEEP_JS + f"\nconsole.log(JSON.stringify({expression}));\n", encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


def test_a_selector_finds_an_element_in_an_open_shadow_root(tmp_path) -> None:
    assert _run(
        tmp_path, "[deepQuery('#send').id, deepQuery('[data-north-uid=\"n9\"]').id, deepQuery('#light').id]"
    ) == [
        "send",
        "send",
        "light",
    ]
    assert _run(tmp_path, "deepAll('button').map(el => el.id)") == ["light", "send"]


def test_the_page_is_walked_as_it_is_shown(tmp_path) -> None:
    """A host shows its shadow root, not its own children; a slot shows the children assigned to it."""
    assert _run(tmp_path, "flatChildren(host).map(el => el.id)") == ["dialog"]
    assert _run(tmp_path, "flatChildren(slot).map(el => el.id)") == ["kept"]


def test_a_click_check_reaches_the_button_inside_the_shadow_root(tmp_path) -> None:
    """The document's hit test names only the host; the shadow root's own names the button."""
    assert _run(tmp_path, "deepFromPoint(1, 1).id") == "send"
    assert _run(tmp_path, "[flatContains(host, send), flatContains(dialog, shown), flatContains(send, host)]") == [
        True,
        True,
        False,
    ]


def test_focus_and_text_inside_the_shadow_root_are_seen(tmp_path) -> None:
    assert _run(tmp_path, "deepActive().id") == "note"
    assert _run(tmp_path, "shadowText()") == "Add a note to your invitation"
    assert "Add a note to your invitation" in _run(tmp_path, "deepText()")
