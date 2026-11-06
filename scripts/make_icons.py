"""Draw north's logo and app icons into web/public: the "n" of north's wordmark and its green dot.

The "n" is JetBrains Mono ExtraBold's (SIL OFL 1.1), the typeface and weight of the wordmark in the
dashboard's sidebar, taken from web/src/fonts with its overlaps removed; the full stop is drawn as
the circle it is. The colours are the dashboard's own (--bg, --text and --accent in
web/src/styles.css). Run it after changing any of them; the dashboard's build copies web/public:

    python scripts/make_icons.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageColor, ImageDraw

PUBLIC = Path(__file__).resolve().parents[1] / "web" / "public"

# In font units: 1000 to the em, the baseline at y=0, y pointing up.
N = (
    "M66 0V550H211V445H214Q222 491 251 520Q291 560 359 560Q439 560 487.5 504Q536 448 536 355"
    "V0H386V340Q386 383 363.5 406.5Q341 430 300 430Q260 430 238 406.5Q216 383 216 340V0Z"
)
N_LEFT, N_RIGHT, N_TOP = 66, 536, 560
DOT_R = 101
DOT_GAP = 70  # the font sets the dot 208 from the n, which leaves it adrift at icon size
DOT_X = N_RIGHT + DOT_GAP + DOT_R
DOT_Y = DOT_R - 10  # the full stop dips 10 below the baseline, as the font's does
WIDTH = DOT_X + DOT_R - N_LEFT
MIDDLE = (N_TOP + DOT_Y - DOT_R) / 2  # halfway between the top of the n and the bottom of the dot

BG, INK, ACCENT = "#0a0c0b", "#f0f2ef", "#b8ef72"
CORNER = 0.225  # a rounded tile's corner radius, as a share of its side


class Icon(NamedTuple):
    share: float  # how much of the tile's width the "n." spans
    rounded: bool  # False: a full square, for systems that cut their own shape (Apple's, a maskable icon)


TAB = Icon(0.70, rounded=True)  # large enough to read at 16 px in a browser tab
APP = Icon(0.58, rounded=True)
FULL = Icon(0.58, rounded=False)  # the "n." stays inside a maskable icon's safe circle, 80% across

# The manifest's icons: Chrome installs from the 192 and 512 px ones, and Safari's Add to Dock, like
# Chrome on a recent Mac, takes the full-bleed maskable one and cuts the Mac's rounded square from it.
APP_ICONS = (
    ("icon-192.png", 192, APP, "any"),
    ("icon-512.png", 512, APP, "any"),
    ("icon-maskable.png", 1024, FULL, "maskable"),
)


def place(size: float, icon: Icon) -> tuple[float, float, float]:
    """How much to scale the font units, and where the n's origin goes, to centre the "n." on a tile."""
    scale = size * icon.share / WIDTH
    return scale, (size - WIDTH * scale) / 2 - N_LEFT * scale, size / 2 + MIDDLE * scale


def svg(icon: Icon, size: int = 512) -> str:
    scale, x, baseline = place(size, icon)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" rx="{size * CORNER if icon.rounded else 0:g}" fill="{BG}"/>'
        f'<g transform="translate({x:.2f} {baseline:.2f}) scale({scale:.5f} {-scale:.5f})">'
        f'<path fill="{INK}" d="{N}"/><circle cx="{DOT_X}" cy="{DOT_Y}" r="{DOT_R}" fill="{ACCENT}"/></g></svg>\n'
    )


def png(size: int, icon: Icon) -> Image.Image:
    """The icon at this size, drawn many times larger and averaged down, so its edges are smooth."""
    big = size * max(4, 4096 // size)
    scale, x, baseline = place(big, icon)

    def coverage(draw) -> Image.Image:
        mask = Image.new("L", (big, big))
        draw(ImageDraw.Draw(mask))
        return mask.resize((size, size), Image.Resampling.BOX)

    corner = big * CORNER if icon.rounded else 0
    tile = coverage(lambda d: d.rounded_rectangle((0, 0, big - 1, big - 1), corner, fill=255))
    n = coverage(lambda d: d.polygon([(x + px * scale, baseline - py * scale) for px, py in outline(N)], fill=255))
    cx, cy, r = x + DOT_X * scale, baseline - DOT_Y * scale, DOT_R * scale
    dot = coverage(lambda d: d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255))

    image = Image.new("RGBA", (size, size), ImageColor.getrgb(BG))
    image.putalpha(tile)
    image.paste(ImageColor.getrgb(INK), mask=n)
    image.paste(ImageColor.getrgb(ACCENT), mask=dot)
    return image if icon.rounded else image.convert("RGB")


def outline(path: str, steps: int = 24) -> list[tuple[float, float]]:
    """The points along one closed path of M, H, V, L and Q commands, its curves cut into short lines."""
    points: list[tuple[float, float]] = []
    for command, numbers in re.findall(r"([MHVLQZ])([^MHVLQZ]*)", path):
        values = [float(value) for value in re.findall(r"-?[\d.]+", numbers)]
        x, y = points[-1] if points else (0.0, 0.0)
        if command in "ML":
            points.append((values[0], values[1]))
        elif command == "H":
            points.append((values[0], y))
        elif command == "V":
            points.append((x, values[0]))
        elif command == "Q":
            cx, cy, ex, ey = values
            for t in (step / steps for step in range(1, steps + 1)):
                a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
                points.append((a * x + b * cx + c * ex, a * y + b * cy + c * ey))
    return points


def main() -> None:
    PUBLIC.mkdir(exist_ok=True)
    (PUBLIC / "favicon.svg").write_text(svg(TAB), encoding="utf-8")
    tab = {size: png(size, TAB) for size in (16, 32, 48)}
    tab[48].save(PUBLIC / "favicon.ico", sizes=[(size, size) for size in tab], append_images=[tab[16], tab[32]])
    png(180, FULL).save(PUBLIC / "apple-touch-icon.png", optimize=True)
    for name, size, icon, _ in APP_ICONS:
        png(size, icon).save(PUBLIC / name, optimize=True)
    manifest = {
        "name": "north",
        "short_name": "north",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": BG,
        "theme_color": BG,
        "icons": [
            {"src": name, "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
            for name, size, _, purpose in APP_ICONS
        ],
    }
    (PUBLIC / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
