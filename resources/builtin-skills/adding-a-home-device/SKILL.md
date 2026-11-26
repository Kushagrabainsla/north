---
name: adding-a-home-device
description: "Use when the user wants lights, plugs or switches controlled from north (TP-Link Kasa, a Philips Hue bridge, a Govee strip, a Shelly plug, a WiZ bulb): north makes a tool for the brand and a panel on the Views page with a switch, sliders and colour for each device, and music sync if they want it."
intents: [create-tool, create-view]
domains: [general]
---
# Adding a home device

> A brand of lights, plugs or switches becomes two things north makes: one tool (`create_tool`) that lists the devices and changes one, and one panel (`create_view`) on the Views page with a row per device. The tool runs in its own process with its own packages; the panel's controls call it as the user. Do both: the job is done when the user can switch a light from the panel.

## Decide first
1. Call `create_tool` with `action='list'` and `create_view` with `action='list'`. A brand that already has a tool is fixed with `update`; a panel that exists is drafted again with the same `id`. The tool list also names the user's saved secrets.
2. Find how the brand is controlled: a local HTTP API (Hue bridge, Shelly, Elgato, WLED), a cloud API with a key (Govee, LIFX), or a Python package that speaks its protocol (TP-Link Kasa: `python-kasa`). A package goes in `packages` at one exact version: read its current version from `https://pypi.org/pypi/<name>/json` with `fetch_url`, and its README for how to log in, list and change devices.

## Ask once
- A login, key or token is a secret. `create_tool` `action='list'` names the saved ones; use one that fits. Otherwise choose short names (`kasa_email`, `kasa_password`; `hue_bridge`) and call `request_secret` once with every part of the login on one card: the user types it there and north saves it. Never ask for a value in chat.
- What only the user knows (which room, whose lights) goes in one `ask_user` call with `questions`. A step they do by hand (pressing the bridge's link button) is its own `ask_user` call with `requires_user_action=true`.

## Write the tool
One `Tool` per brand (`adding-a-north-tool` has the full contract), with an `action` parameter:
- `list`: every device and how it is now, each as `{"id", "name", "on", "dims", "colors", "brightness", "hue", "saturation", "kelvin"}`, with brightness 1-100, hue 0-360, saturation 0-100 and white in kelvin. Leave out what a device cannot do. Name it in `read_only_when = {"action": ["list"]}`, so the panel can read it.
- `set`: `id` (or `"all"`) and any of `on`, `brightness`, `hue` + `saturation`, `kelvin`. Send only what is set, then return the device as the brand reports it now. Convert to the brand's own units on the way out and back.
- Use the brand's stable id, never a list position. Look devices up on each call when the brand answers a discovery call on the network (Kasa, WiZ, LIFX, a Hue bridge over SSDP), since a router can give a device a new address; otherwise keep the address in one constant at the top.
- `is_mutating = True`; return an error that says what to check when a device does not answer.

## Make its panel
A view whose data is the tool's `list`, with a row per device. Give the row a control for every setting `set` takes, so the user can do from the panel all that the tool can: a `Switch` bound to the row's `on`, a `Slider` to its `brightness`, a `ColorPicker` to its `hue`, and a `Slider` to its `kelvin` for white, with the tool's kelvin range. Each control runs a `set` action with the row's `id`. For music sync, add a `Microphone` whose action sets every light's `hue` and `brightness` from what it hears. `refresh` keeps the panel current. The calls run as the user, with no card, as their own switches.

## A complete example (Philips Hue)

The tool, with `create_tool` `action='create'`, `name='hue_lights'`, this as `content`, and `test_params` `{"action": "list"}`:

```python
from __future__ import annotations

from typing import Any

import httpx

from config.secret_store import secret
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_BRIDGE = "http://192.168.1.20"


class HueLightsTool(Tool):
    name = "hue_lights"
    description = (
        "Philips Hue lights on the bridge at 192.168.1.20. action='list' gives every light and its state; "
        "action='set' with id (or 'all') and any of on, brightness 1-100, hue 0-360 with saturation 0-100, "
        "kelvin 2000-6500 changes it."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "set"]},
            "id": {"type": "string", "description": "A light's id from list, or 'all'."},
            "on": {"type": "boolean"},
            "brightness": {"type": "integer", "minimum": 1, "maximum": 100},
            "hue": {"type": "integer", "minimum": 0, "maximum": 360},
            "saturation": {"type": "integer", "minimum": 0, "maximum": 100},
            "kelvin": {"type": "integer", "minimum": 2000, "maximum": 6500},
        },
        "required": ["action"],
    }
    is_mutating = True
    read_only_when = {"action": ["list"]}
    uses_secrets = ("hue_bridge",)

    async def run(self, input: ToolInput) -> ToolOutput:
        params = input.params
        base = f"{_BRIDGE}/api/{secret('hue_bridge')}/lights"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                lights = (await client.get(base)).json()
                if params.get("action") == "list":
                    return ToolOutput(success=True, data={"devices": [_device(i, light) for i, light in lights.items()]})
                ids = list(lights) if params.get("id") == "all" else [str(params.get("id") or "")]
                if not all(i in lights for i in ids):
                    return ToolOutput(success=False, error=f"No light {params.get('id')!r}: use an id from list.")
                state = _state(params)
                for light_id in ids:
                    (await client.put(f"{base}/{light_id}/state", json=state)).raise_for_status()
                now = (await client.get(base)).json()
        except httpx.HTTPError as exc:
            return ToolOutput(success=False, error=f"The Hue bridge at {_BRIDGE} did not answer ({exc}). Is it on?")
        return ToolOutput(success=True, data={"devices": [_device(i, now[i]) for i in ids]})


def _state(params: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if params.get("on") is not None:
        state["on"] = bool(params["on"])
    if params.get("brightness") is not None:
        state["bri"] = max(1, round(params["brightness"] * 254 / 100))
    if params.get("hue") is not None:
        state["hue"] = round(params["hue"] * 65535 / 360)
        state["sat"] = round((params.get("saturation") or 100) * 254 / 100)
    if params.get("kelvin") is not None:
        state["ct"] = round(1_000_000 / params["kelvin"])
    return state


def _device(light_id: str, light: dict[str, Any]) -> dict[str, Any]:
    state = light["state"]
    white = state.get("colormode") == "ct"
    colour = "hue" in state and not white
    return {
        "id": light_id,
        "name": light["name"],
        "on": state["on"],
        "dims": "bri" in state,
        "colors": "hue" in state,
        "brightness": max(1, round(state["bri"] * 100 / 254)) if "bri" in state else None,
        "hue": round(state["hue"] * 360 / 65535) if colour else None,
        "saturation": round(state["sat"] * 100 / 254) if colour else None,
        "kelvin": round(1_000_000 / state["ct"]) if white else None,
    }
```

Its panel, with `create_view` `action='draft'`:

```json
{
  "action": "draft",
  "view": {
    "id": "hue-lights",
    "title": "Lights",
    "description": "Each Hue light, with music sync.",
    "about": {"kind": "tool", "name": "hue_lights"},
    "data": [{"name": "lights", "tool": "hue_lights", "args": {"action": "list"}}],
    "state": {"sound": {}},
    "refresh": 30,
    "actions": {
      "power": {"tool": "hue_lights", "args": {"action": "set", "id": {"path": "id"}, "on": {"path": "on"}}, "label": "Switches a light"},
      "dim": {"tool": "hue_lights", "args": {"action": "set", "id": {"path": "id"}, "brightness": {"path": "brightness"}}, "label": "Dims a light"},
      "colour": {"tool": "hue_lights", "args": {"action": "set", "id": {"path": "id"}, "hue": {"path": "hue"}}, "label": "Colours a light"},
      "white": {"tool": "hue_lights", "args": {"action": "set", "id": {"path": "id"}, "kelvin": {"path": "kelvin"}}, "label": "Makes a light white"},
      "music": {"tool": "hue_lights", "args": {"action": "set", "id": "all", "hue": {"path": "/state/sound/hue"}, "brightness": {"path": "/state/sound/brightness"}}, "label": "Lights follow the music"}
    },
    "components": [
      {"id": "root", "component": "Column", "children": ["music_panel", "list"]},
      {"id": "music_panel", "component": "Panel", "title": "Music sync", "children": ["mic"]},
      {"id": "mic", "component": "Microphone", "label": "Lights follow the music", "value": {"path": "/state/sound"}, "action": {"event": {"name": "music"}}},
      {"id": "list", "component": "Panel", "title": "Lights", "children": {"path": "/lights/devices", "componentId": "light"}},
      {"id": "light", "component": "Column", "children": ["on", "brightness", "hue", "white"]},
      {"id": "on", "component": "Switch", "label": {"path": "name"}, "value": {"path": "on"}, "action": {"event": {"name": "power"}}},
      {"id": "brightness", "component": "Slider", "label": "Brightness", "value": {"path": "brightness"}, "min": 1, "max": 100, "unit": "%", "action": {"event": {"name": "dim"}}},
      {"id": "hue", "component": "ColorPicker", "label": "Colour", "value": {"path": "hue"}, "action": {"event": {"name": "colour"}}},
      {"id": "white", "component": "Slider", "label": "White", "value": {"path": "kelvin"}, "min": 2000, "max": 6500, "unit": "K", "action": {"event": {"name": "white"}}}
    ]
  }
}
```

## Create it and prove it
1. `create_tool` the tool. The user is asked once; north makes its environment, lists the devices as the test, and the tool is live if that works. If the test fails because a device cannot be reached (wrong address, bridge off), say so and ask the user to check it, instead of rewriting working code.
2. `create_view` `action='draft'` the panel, fix every problem it names, give the user the preview link, then `action='add'`. The user is asked once.
3. Change one device the user named and report what the device says it is now. If a device answers on some calls and not on others, the code is not broken: the device is. Report it once, with what failed and how often, and do not change working code for it.

## Common mistakes
- One tool per action, or one per device: one tool per brand, with `list` and `set`.
- `list` not named in `read_only_when`: the panel cannot read it.
- A setting `set` takes with no control on the panel, such as white: the user can make a light green there, but never white again.
- Brightness, colour or white in the brand's own units in what `list` returns, instead of 1-100, 0-360 and kelvin.
- A list position as the id: a light would change identity when another is added.
- A package imported but not in `packages`, or without one exact version.
- A login asked for in chat or written in the code: it goes through `request_secret`.
- Retries, pauses and new versions of a tool that already worked, for a device that answers only on some calls: each update costs turns and fixes nothing in the device.

## Done when
- The tool is live, its panel is on the Views page, and one change was confirmed by the device itself. If either stayed a draft, say why, and what the user can do about it.
