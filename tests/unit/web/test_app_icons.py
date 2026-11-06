"""North's logo and app icons: the "n" of its wordmark and the green dot.

North added as an app on a Mac got a plain icon the system made up, because the dashboard named
no icon at all. Now its page names a tab icon and a manifest, and the manifest gives the Mac an
opaque, full-bleed icon to cut its rounded square from. `scripts/make_icons.py` draws them all.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

WEB = Path(__file__).parents[3] / "web"
PUBLIC = WEB / "public"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "link" and found.get("rel") in {"icon", "apple-touch-icon", "manifest"}:
            self.links.setdefault(str(found["rel"]), []).append(str(found["href"]))


def _links(page: Path) -> dict[str, list[str]]:
    parser = _Links()
    parser.feed(page.read_text(encoding="utf-8"))
    return parser.links


def _manifest() -> dict:
    return json.loads((PUBLIC / "manifest.webmanifest").read_text(encoding="utf-8"))


def test_the_page_names_its_icons_and_its_manifest() -> None:
    assert _links(WEB / "index.html") == {
        "icon": ["/favicon.ico", "/favicon.svg"],
        "apple-touch-icon": ["/apple-touch-icon.png"],
        "manifest": ["/manifest.webmanifest"],
    }


def test_each_icon_named_is_there_and_nothing_else_is() -> None:
    named = {href.lstrip("/") for hrefs in _links(WEB / "index.html").values() for href in hrefs}
    named |= {icon["src"] for icon in _manifest()["icons"]}

    assert named == {path.name for path in PUBLIC.iterdir()}


def test_the_build_serves_them_under_the_dashboards_address() -> None:
    built = [href for hrefs in _links(WEB / "dist" / "index.html").values() for href in hrefs]

    assert len(built) == 4 and all(href.startswith("/app/") for href in built)
    for path in PUBLIC.iterdir():
        assert (WEB / "dist" / path.name).read_bytes() == path.read_bytes(), f"rebuild the dashboard: {path.name}"


def test_the_manifest_gives_a_mac_an_opaque_full_bleed_icon_to_cut_its_own_shape_from() -> None:
    manifest = _manifest()
    assert manifest["name"] == "north" and manifest["display"] == "standalone" and manifest["start_url"] == "./"
    for icon in manifest["icons"]:
        with Image.open(PUBLIC / icon["src"]) as image:
            assert f"{image.width}x{image.height}" == icon["sizes"]

    [maskable] = [icon for icon in manifest["icons"] if icon["purpose"] == "maskable"]
    with Image.open(PUBLIC / maskable["src"]) as image:
        assert image.width >= 1024 and image.mode == "RGB"  # Safari's Add to Dock asks for 1024 px, opaque
    assert {"192x192", "512x512"} <= {icon["sizes"] for icon in manifest["icons"] if icon["purpose"] == "any"}


def test_the_icon_a_phone_or_tablet_uses_is_opaque_at_its_size() -> None:
    """iOS fills a transparent corner with black, and rounds the square itself."""
    with Image.open(PUBLIC / "apple-touch-icon.png") as image:
        assert image.size == (180, 180) and image.mode == "RGB"
