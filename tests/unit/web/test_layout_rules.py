"""Layout rules that broke on real content in the issue #76 test, each also checked in a browser at 390 px.

P9: a long unbroken word in a reply or an artifact ran past its column and was
clipped, with no scroller. P15: on a phone, a session's activity time and turn
count were drawn in one cell ("7m ago" over "1"). P16: the dashboard's activity
strip, a 4-column grid, gave one event a quarter of the row, cut short, beside
an empty grey block.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).parents[3] / "web" / "src" / "styles.css").read_text(encoding="utf-8")
KIT = (Path(__file__).parents[3] / "web" / "src" / "kit.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """The body of the first top-level rule for exactly *selector*."""
    match = re.search(rf"(?:^|}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS, re.M)
    return match.group(1) if match else ""


def test_a_long_word_in_a_reply_or_an_artifact_wraps() -> None:
    assert "overflow-wrap: anywhere" in _rule(".markdown")


def test_a_session_s_activity_and_turns_have_their_own_cells_on_a_phone() -> None:
    # Sessions is a kit table now: on a phone every cell of a row is its own line.
    phone = KIT[KIT.index("@media (max-width: 680px)"):]
    assert ".kit-table-open { grid-template-columns: 1fr;" in phone


def test_the_activity_strip_fills_each_row_however_few_events_there_are() -> None:
    assert "flex-wrap: wrap" in _rule(".activity-line")
    assert "grid-template-columns: repeat(4, 1fr)" not in _rule(".activity-line")


def test_the_devices_page_lines_up_with_its_header_like_every_other_page() -> None:
    """Its blocks were indented 26 px with margins of their own, and the Add a device panel then ran off the
    right edge at 1080 px. A page's blocks sit on its edges and are spaced by its one gap; checked in a browser
    at 390, 1080 and 1400 px, every block's left and right edges equal the header's."""
    # The bar, notice and grid are kit parts now (Toolbar, Notice, CardGrid), with no margins of their own.
    for selector in (".music-sync",):
        body = _rule(selector)
        assert body, selector
        assert not re.search(r"\b(margin|padding)\s*:\s*0 \d+px", body), selector
    assert not re.search(r"\.(lights-bar|lights-notice|music-sync|add-device)[^{]*\{[^}]*margin-(left|right)", CSS)
    assert ".add-device {" not in CSS  # the shared Panel, as in Settings


def _winning(selector: str, prop: str) -> str:
    """The value of *prop* in the last top-level rule for exactly *selector* that sets it: the one that applies."""
    values = [
        match.group(1).strip()
        for body in re.findall(rf"(?:^|}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS, re.M)
        if (match := re.search(rf"(?:^|[;\s]){re.escape(prop)}\s*:\s*([^;]+)", body))
    ]
    return values[-1] if values else ""


def test_the_dashboard_grid_has_no_grey_holes_and_a_notice_cannot_stretch_it() -> None:
    """The user's dashboard: the Attention panel held a whole briefing, hundreds of lines, and the System panel
    beside it was a tall empty box; cells no panel filled were solid grey, the lines' colour showing through."""
    assert _winning(".cockpit-grid", "background") == "var(--surface)"
    assert _winning(".cockpit-grid", "grid-auto-flow") == "dense"
    assert _winning(".cockpit-grid > .panel", "box-shadow").startswith("0 0 0 1px")
    assert ".notice-row small { white-space: pre-wrap" not in CSS  # the whole message, every line kept
    assert "-webkit-line-clamp: 2" in _rule(".notice-row .notice-preview")
