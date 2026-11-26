---
name: controlling-smart-home-devices
description: "Use for lights, lamps, bulbs, plugs and switches (Kasa, Hue, Govee, Shelly...), lighting moods and scenes, colour and brightness, this computer's screen brightness, and any other smart-home platform (Stratis, SmartThings, Lutron, Ring, Nest, Matter)."
domains:
  - general
execution:
  agent: general
  tools:
    - screen_brightness
    - create_tool
    - create_view
    - create_flow
    - web_search
    - fetch_url
  approval: on_mutation
  inputs:
    type: object
    properties:
      request:
        type: string
    additionalProperties: true
  outputs:
    type: object
    properties:
      devices_changed:
        type: array
        items:
          type: string
    additionalProperties: true
  success_criteria:
    - Every device the request named was found, or the user was told which was not.
    - Each change was read back and reported with the device's new state.
    - A brand or platform with no tool yet got one built for it, never a bare shell command.
---
# Controlling smart home devices

> Lights, plugs and screens, by name: act on what the user said, confirm what changed.

## Tools

- The brand's own tool, which north made for it (`create_tool` `action='list'` names them, such as `kasa_lights` or `hue_lights`): `action='list'` for the devices, their ids and state, then `action='set'` with `id` (or `"all"`) and any of `on`, `brightness` 1-100, `hue` 0-360 with `saturation`, `kelvin`.
- `screen_brightness` - the brightness of this computer's screens, 0-100: the laptop's own and external monitors. Not for lamps or bulbs.
- `create_flow` (`schedule`, `list`, `reschedule`, `unschedule`) - automations ("turn off all lights at 11pm"): make it a flow first, test it, then schedule it, in the user's local time.
- `create_tool`, `create_view`, `request_secret`, `web_search`, `fetch_url` - for a brand or platform north has no tool for yet (below).

## Procedure

1. Find the brand's tool with `create_tool` `action='list'`. None for this brand: follow `adding-a-home-device` first, then come back to the request.
2. Map the request to calls. "turn off the desk lamp" → `list`, then `set` with that lamp's `id` and `on=false`; "dim the office light to 30%" → `brightness=30`; "set the bedroom lamp to blue" → `hue=240`; "make the bedroom lamp warm" → `kelvin=2700`; "all the lights" → `id="all"`.
3. A mood is a set of values, applied at once with no follow-up question: cozy (2700 K, 40%), moody (hue 280, 25%), movie (2700 K, 15%), focus (5000 K, 100%), romantic (hue 340, 30%), party (a different hue per light, 100%), sunset (hue 25, 60%). Ask one concise question only for a mood not listed.
4. For screens: "dim my screens to 30%" → `screen_brightness` `brightness=30`; "set the laptop screen to 50%" or "make the monitor a bit brighter" → `action=get` for the screens' names and levels, then `action=set` on that screen.
5. Read back and report what changed: which devices, and their new state. A device that lacks a feature (colour on a white-only bulb) is reported as such, not retried.

## A platform north has no tool for

Stratis, Latch, SmartThings, Lutron, Ring, Nest, Yale, Matter/Thread, a building-management app:

1. Identify the platform. "Stratis" is a property-management smart-home platform (stratis.com), not the Linux `stratis` storage tool; never run system commands for a smart-home request.
2. `web_search` for its API: docs, authentication (OAuth, API key, token), the device list and control endpoints, or a Python package for it.
3. Lights, plugs and switches: follow `adding-a-home-device`, which makes the tool and its panel. Anything else: `create_tool` a tool for the platform (`adding-a-north-tool`).
4. Use the new tool for the request. If no usable API turns up, say what was found and what it would take; never fall back to bare bash.
