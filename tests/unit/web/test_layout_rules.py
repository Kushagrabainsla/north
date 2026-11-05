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


def _rule(selector: str) -> str:
    """The body of the first top-level rule for exactly *selector*."""
    match = re.search(rf"(?:^|}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS, re.M)
    return match.group(1) if match else ""


def test_a_long_word_in_a_reply_or_an_artifact_wraps() -> None:
    assert "overflow-wrap: anywhere" in _rule(".markdown")


def test_a_session_s_activity_and_turns_have_their_own_cells_on_a_phone() -> None:
    # :first-of-type and :last-of-type count every span in the row, so neither matched a meta cell.
    assert not re.search(r"capability-registry-meta:(first|last)-of-type", CSS)
    assert ".capability-registry-meta + .capability-registry-meta { grid-column: 2;" in CSS


def test_the_activity_strip_fills_each_row_however_few_events_there_are() -> None:
    assert "flex-wrap: wrap" in _rule(".activity-line")
    assert "grid-template-columns: repeat(4, 1fr)" not in _rule(".activity-line")


def test_the_devices_page_lines_up_with_its_header_like_every_other_page() -> None:
    """Its blocks were indented 26 px with margins of their own, and the Add a device panel then ran off the
    right edge at 1080 px. A page's blocks sit on its edges and are spaced by its one gap; checked in a browser
    at 390, 1080 and 1400 px, every block's left and right edges equal the header's."""
    for selector in (".lights-bar", ".lights-notice", ".lights-grid", ".music-sync"):
        body = _rule(selector)
        assert body, selector
        assert not re.search(r"\b(margin|padding)\s*:\s*0 \d+px", body), selector
    assert not re.search(r"\.(lights-bar|lights-notice|music-sync|add-device)[^{]*\{[^}]*margin-(left|right)", CSS)
    assert ".add-device {" not in CSS  # the shared Panel, as in Settings
