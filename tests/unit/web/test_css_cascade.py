"""Two CSS classes on one element must not fight over the same property.

The Memory tabs shipped broken because of exactly this. `.segmented` hardcoded
`grid-template-columns: repeat(3,1fr)` and `.memory-tabs` overrode it with
`repeat(5,1fr)` - but `.memory-tabs` was declared *earlier* in the file, and two
single-class selectors have equal specificity, so the later rule won. Five tabs
landed in a three-column grid and wrapped to 3 + 2.

Nothing catches that by reading either rule on its own: each is correct, and the
bug is in the distance between them. So the check is over the pairs of classes
that actually appear together on an element, which the markup already states.

Layout properties only. A colour or a padding losing a cascade race is a
cosmetic difference someone will notice; a grid definition losing one silently
rearranges the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB_SRC = Path(__file__).parent.parent.parent.parent / "web" / "src"
STYLESHEET = WEB_SRC / "styles.css"

# Properties where a silently-lost override changes the shape of the page.
LAYOUT_PROPERTIES = (
    "grid-template-columns",
    "grid-template-rows",
    "grid-auto-flow",
    "flex-direction",
    "display",
)


def _class_combinations() -> set[frozenset[str]]:
    """Every set of classes the markup puts on one element together."""
    combinations = set()
    for source in WEB_SRC.rglob("*.tsx"):
        text = source.read_text(encoding="utf-8")
        for literal in re.findall(r'className="([^"{}]+)"', text):
            names = {n for n in literal.split() if n}
            if len(names) > 1:
                combinations.add(frozenset(names))
    return combinations


def _without_at_rules(css: str) -> str:
    """CSS with every `@media`/`@supports` block removed.

    A rule inside one is a deliberate override gated on a condition, which is
    the opposite of the accident this looks for.
    """
    out: list[str] = []
    index = 0
    while index < len(css):
        at = css.find("@", index)
        if at == -1:
            out.append(css[index:])
            break
        opening = css.find("{", at)
        if opening == -1:
            out.append(css[index:])
            break
        out.append(css[index:at])
        depth, cursor = 1, opening + 1
        while cursor < len(css) and depth:
            depth += (css[cursor] == "{") - (css[cursor] == "}")
            cursor += 1
        index = cursor
    return "".join(out)


def _single_class_rules() -> dict[str, list[tuple[int, str]]]:
    """class name -> [(source order, declaration block)] for bare `.name` rules."""
    css = STYLESHEET.read_text(encoding="utf-8")
    # Strip comments so a selector mentioned in prose is not read as a rule.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = _without_at_rules(css)
    rules: dict[str, list[tuple[int, str]]] = {}
    for order, match in enumerate(re.finditer(r"(?<![,\w.\-])\.([a-zA-Z][\w-]*)\s*\{([^}]*)\}", css)):
        rules.setdefault(match.group(1), []).append((order, match.group(2)))
    return rules


def _declares(block: str, prop: str) -> bool:
    return re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:", block) is not None


@pytest.mark.parametrize("prop", LAYOUT_PROPERTIES)
def test_no_layout_property_is_silently_overridden_by_source_order(prop: str) -> None:
    rules = _single_class_rules()
    conflicts = []
    for combination in _class_combinations():
        setters = [
            (order, name) for name in combination for order, block in rules.get(name, []) if _declares(block, prop)
        ]
        if len(setters) < 2:
            continue
        setters.sort()
        winner = setters[-1][1]
        losers = [name for _, name in setters[:-1] if name != winner]
        if not losers:
            continue  # one class declared twice is a plain redefinition, not this bug
        conflicts.append(
            f'class="{" ".join(sorted(combination))}": .{winner} wins {prop} over '
            f"{', '.join('.' + name for name in losers)} on source order alone"
        )
    assert not conflicts, (
        "Two classes on the same element set the same layout property with equal "
        "specificity, so whichever is written last silently wins:\n  " + "\n  ".join(sorted(conflicts))
    )


def test_a_segmented_control_sizes_itself_to_its_buttons() -> None:
    """The specific shape the Memory tabs needed.

    A hardcoded column count means every control with a different number of
    buttons needs a modifier, and each modifier is another chance to lose the
    race above. Sizing to the children removes the need for any of them.
    """
    rules = _single_class_rules()
    assert "segmented" in rules, "the segmented control's rule was renamed - update this test"
    block = rules["segmented"][0][1]
    assert "grid-auto-flow" in block, ".segmented must size to its children, not a written-down count"
    assert not re.search(r"grid-template-columns\s*:\s*repeat\(\s*\d", block), (
        "a hardcoded column count breaks the moment a tab is added"
    )
