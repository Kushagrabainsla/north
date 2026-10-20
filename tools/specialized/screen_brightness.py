"""ScreenBrightnessTool - read or set the brightness of the screens north's computer drives.

The platform work sits behind `_display.current()`; this tool only picks the
screens, asks the approval layer before a change, and reports the level each
screen reads back, not the level it was asked for.
"""

from __future__ import annotations

from typing import Any

from approval.approvals import Request
from approval.policy import Action, ActionKind
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized._display import Display, Screen, current


class ScreenBrightnessTool(Tool):
    """Read or set the brightness of this computer's screens."""

    name = "screen_brightness"
    is_mutating = True
    description = (
        "Read or set the brightness of this computer's screens - the laptop's own screen and any "
        "external monitors - from 0 to 100%. action='get' reads each screen's level and name; "
        "action='set' with brightness changes it. Pass screen to pick one by a name from a get; "
        'leave it out for every screen. For a relative change ("a bit brighter"), get '
        "first, then set. Lamps and smart bulbs use the kasa tool instead. "
        "Setting follows the approval mode; reading never asks."
    )
    # A read is asked for by name. When leaving brightness out meant "read", an
    # agent asked to make the screen "a bit brighter" read it by sending
    # brightness=0, which set it to 0%. A brightness with no action still sets,
    # named as the kasa tool names it: asked to "set my screen brightness to
    # 24%", the planner sent brightness=24 and nothing else.
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set"],
                "description": "'get' reads the levels and the screens' names. 'set' changes them to brightness.",
            },
            "brightness": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "The level to set, 0 to 100. Used with action='set'.",
            },
            "screen": {
                "type": "string",
                "description": (
                    "One screen, by its name or a part of it, as action='get' lists them. Omit it for every screen."
                ),
            },
        },
    }

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        params = params or {}
        action = str(params.get("action") or "").strip().lower()
        return action == "set" or (not action and "brightness" in params)

    async def describe(self, input: ToolInput) -> Request | None:
        level = _level_of(input.params)
        if level is None:
            return None  # run() refuses it, so there is nothing to approve
        target = str(input.params.get("screen") or "").strip() or "every screen"
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.DEVICE,
                summary=f"set brightness of {target} to {level}%",
                operation="set_brightness",
                args=f"{target}={level}",
            ),
            title="Screen Brightness - Approval Required",
            message=f"Set the brightness of **{target}** to **{level}%**",
            declined="Brightness change cancelled by user.",
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        setting = self.mutates(input.params)
        level = _level_of(input.params) if setting else None
        if setting and level is None:
            return ToolOutput(success=False, error="Parameter 'brightness' must be a whole number from 0 to 100.")
        display = current()
        if display is None:
            return ToolOutput(success=False, error="Screen brightness is not supported on this computer yet.")
        screens = await display.screens()
        wanted = str(input.params.get("screen") or "").strip()
        targets = _pick(screens, wanted)
        if not targets:
            names = ", ".join(screen.name for screen in screens) or "none found"
            return ToolOutput(success=False, error=f"No screen matches {wanted!r}. Screens: {names}.")
        results = [await _apply(display, screen, level) for screen in targets]
        if not any("brightness" in result for result in results):
            return ToolOutput(success=False, error=_lines(results, setting))
        return ToolOutput(success=True, data={"screens": results, "changed": setting})

    def format_output(self, data: dict[str, Any]) -> str:
        return _lines(data["screens"], data.get("changed", False))


async def _apply(display: type[Display], screen: Screen, level: int | None) -> dict[str, Any]:
    try:
        return {"name": screen.name, "brightness": await display.brightness(screen, level)}
    except OSError as exc:
        return {"name": screen.name, "error": str(exc)}


def _pick(screens: list[Screen], wanted: str) -> list[Screen]:
    """The screens *wanted* names, by their name or any part of it; every screen when nothing is named.

    A word the names do not contain ("laptop", a brand) matches nothing, and the
    error lists the names, so the agent asks again with one of them.
    """
    if not wanted:
        return screens
    query = wanted.lower()
    exact = [screen for screen in screens if screen.name.lower() == query]
    return exact or [screen for screen in screens if query in screen.name.lower()]


def _lines(results: list[dict[str, Any]], changed: bool) -> str:
    """One list item per screen: as plain lines, Markdown ran them into one paragraph."""
    done, failed = ("set to", "not changed") if changed else ("at", "not read")
    return "\n".join(
        f"- {result['name']}: {done} {result['brightness']}%"
        if "brightness" in result
        else f"- {result['name']}: {failed} - {result['error']}"
        for result in results
    )


def _level_of(params: dict[str, Any]) -> int | None:
    try:
        level = round(float(params["brightness"]))
    except (KeyError, TypeError, ValueError):
        return None
    return level if 0 <= level <= 100 else None
