---
name: adding-a-home-device
description: "Use when the user wants lights, plugs or switches of a brand north does not control yet added to the Devices page or controlled from chat: a Philips Hue bridge, a Govee strip, a Shelly plug, a WiZ bulb."
intents: [create-tool]
domains: [general]
---
# Adding a home device

> A brand of lights, plugs or switches becomes one tool, a `DeviceTool`. You write how to list its devices and how to change one; north gives it chat actions, approval cards, and a place on the Devices page beside the user's Kasa bulbs, with the same switch, sliders, scenes and music sync.

## Decide first
1. Kasa devices are already there (the `kasa` tool). Call `create_tool` with `action='list'`: a brand that already has a tool is fixed with `update`, not added again. The list also names the user's saved secrets.
2. Find out how the brand is controlled with what north has: a local HTTP API (Hue bridge, Shelly, Elgato), or a cloud API with a key (Govee, LIFX cloud), through `httpx`. A brand that needs a package north does not have cannot be added this way: say so, and what it needs.

## Ask once
In one `ask_user` call with `questions`, ask only what you cannot find out: where the device or its bridge is on the network, and which login, key or pairing the brand needs.
- A key or token is a secret. `create_tool` `action='list'` names the saved ones; use one that fits. Otherwise choose a short name (`hue_bridge`) and ask the user to add it on the dashboard, under Settings, Secrets, with that name. Never ask for its value in chat.
- A step the user does by hand (pressing the bridge's link button, then saying done) is its own `ask_user` call with `requires_user_action=true`.

## Write it
- Subclass `DeviceTool` (from `tools.home`). Set `name` to the brand in snake_case (`hue_lights`), `description` to which devices and where, and `uses_secrets` to the secrets it reads.
- `async def find(self) -> list[Device]`: every device and how it is now. `Device(id, name, on, dims=..., colors=..., white=(warmest, coolest), brightness=..., hue=..., saturation=..., kelvin=...)`, with brightness 1-100, hue 0-360, saturation 0-100 and white in kelvin. Leave out what a device cannot do: a plug has only `on`, and a slider for a lamp that cannot dim would do nothing.
- `async def change(self, device_id: str, change: LightChange) -> Device`: send only the fields that are set (`None` means leave it as it is), then return the device as the brand reports it now. Convert to the brand's own units on the way out and back.
- Use the brand's stable id for `id`, never a list position, and `httpx.AsyncClient(timeout=5)`. Raise when a request fails: north reports it beside the device.
- Do not write `run`: `DeviceTool` gives the tool its `list` and `set` actions.

## A complete example

```python
from __future__ import annotations

from typing import Any

import httpx

from config.secret_store import secret
from tools.home import Device, DeviceTool, LightChange

_BRIDGE = "http://192.168.1.20"


class HueLightsTool(DeviceTool):
    name = "hue_lights"
    description = "Philips Hue lights on the bridge at 192.168.1.20: switch, dim, colour and white."
    uses_secrets = ("hue_bridge",)

    async def find(self) -> list[Device]:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{_BRIDGE}/api/{secret('hue_bridge')}/lights")
        response.raise_for_status()
        return [_device(light_id, light) for light_id, light in response.json().items()]

    async def change(self, device_id: str, change: LightChange) -> Device:
        state: dict[str, Any] = {}
        if change.on is not None:
            state["on"] = change.on
        if change.brightness is not None:
            state["bri"] = max(1, round(change.brightness * 254 / 100))
        if change.hue is not None:
            state["hue"] = round(change.hue * 65535 / 360)
            state["sat"] = round((change.saturation or 0) * 254 / 100)
        if change.kelvin is not None:
            state["ct"] = round(1_000_000 / change.kelvin)
        if change.transition_ms is not None:
            state["transitiontime"] = round(change.transition_ms / 100)
        light = f"{_BRIDGE}/api/{secret('hue_bridge')}/lights/{device_id}"
        async with httpx.AsyncClient(timeout=5) as client:
            (await client.put(f"{light}/state", json=state)).raise_for_status()
            response = await client.get(light)
        response.raise_for_status()
        return _device(device_id, response.json())


def _device(light_id: str, light: dict[str, Any]) -> Device:
    state = light["state"]
    white = state.get("colormode") == "ct"
    colour = "hue" in state and not white
    return Device(
        id=light_id,
        name=light["name"],
        on=state["on"],
        dims="bri" in state,
        colors="hue" in state,
        white=(2000, 6500) if "ct" in state else None,
        brightness=max(1, round(state["bri"] * 100 / 254)) if "bri" in state else None,
        hue=round(state["hue"] * 360 / 65535) if colour else None,
        saturation=round(state["sat"] * 100 / 254) if colour else None,
        kelvin=round(1_000_000 / state["ct"]) if white else None,
    )
```

## Create it and prove it
1. Call `create_tool` with `action='create'`, the tool's `name`, the whole file as `content`, and `test_params` `{"action": "list"}`. The user is asked once; north lists the devices as the test, and the tool is live if that works. Its devices then show on the Devices page with no restart.
2. If the test fails, read why. A fix in the code: send the whole corrected file again. A device north cannot reach (wrong address, bridge off): say so, and ask the user to check it, instead of rewriting working code.
3. Change one device the user named, from chat with `action='set'` (north asks first), and report what the device says it is now.

## Common mistakes
- Writing `run`, or one tool per action: one `DeviceTool` per brand.
- Brightness, colour or white in the brand's own units in `Device`, instead of 1-100, 0-360 and kelvin.
- A list position as the id: the device would change identity when another is added.
- A key in the code or asked for in chat: it is a secret the user adds on the dashboard.

## Done when
- The tool is live, its devices show on the Devices page, and one change was confirmed by the device itself. If it stayed a draft, say why, and what the user can do about it.
