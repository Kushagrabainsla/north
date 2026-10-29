---
name: controlling-smart-home-devices
description: "Use for lights, lamps, bulbs, plugs and other Kasa devices, lighting moods and scenes, colour and brightness, this computer's screen brightness, and any other smart-home platform (Stratis, SmartThings, Lutron, Ring, Nest, Matter)."
domains:
  - general
execution:
  agent: general
  tools:
    - kasa
    - screen_brightness
    - schedule_task
    - list_schedules
    - update_schedule
    - cancel_schedule
    - web_search
    - fetch_url
    - list_dir
    - search_files
    - create_tool
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
    - An unsupported platform got a tool built for it, never a bare shell command.
---
# Controlling smart home devices

> Lights, plugs and screens, by name: act on what the user said, confirm what changed.

## Tools

- `kasa` - TP-Link Kasa devices on the local network: plugs, switches, bulbs, light strips.
- `screen_brightness` - the brightness of this computer's screens, 0-100: the laptop's own and external monitors. Not for lamps or bulbs.
- `schedule_task`, `list_schedules`, `update_schedule`, `cancel_schedule` - automations ("turn off all lights at 11pm"): make it a flow first (`create_flow`), test it, then schedule it, in the user's local time.
- `web_search`, `fetch_url`, `list_dir`, `search_files`, `create_tool` - for a platform north has no tool for yet (below).

## kasa actions

| action | what it does | extra params |
|---|---|---|
| `on` | turn on | - |
| `off` | turn off | - |
| `toggle` | flip current state | - |
| `list` | show all devices and their current state | - |
| `brightness` | set brightness | `brightness` 0–100 |
| `color` | set colour by name or hue | `color` (name) OR `hue` 0–360 + optional `saturation` 0–100 |
| `color_temp` | set white colour temperature | `color_temp`: candlelight/warm/soft/neutral/cool/daylight or Kelvin 2500–6500 |
| `scene` | apply a lighting preset | `scene`: moody/cozy/movie/focus/romantic/party/sunset; optional `device` |

Named colours: red, orange, yellow, green, cyan, blue, purple, pink, magenta.
Named temperatures: candlelight (2500K), warm (2700K), soft (3000K), neutral (4000K), cool (5000K), daylight (6500K).

Power actions (`on`, `off`, `toggle`) need a `device` (alias or IP). `brightness`, `color`, `color_temp` and `scene` may omit `device` to apply to every discovered light. A mood request ("make the lights cozy") is a named `scene`: apply it at once, no follow-up question.

## Procedure

1. Map the request to one action. "turn off the desk lamp" → `off`, `device="desk lamp"`; "set the bedroom lamp to blue" → `color`; "dim the office light to 30%" → `brightness=30`; "make the bedroom lamp warm" → `color_temp="warm"`; "make my lights moody" → `scene="moody"` on every light; "set the living room lights for a movie" → `scene="movie"`, `device="living room"`.
2. If the device name is uncertain, or the request is "all the lights", call `list` first, then act on each device by name.
3. For screens: "dim my screens to 30%" → `screen_brightness` `brightness=30`; "set the laptop screen to 50%" or "make the monitor a bit brighter" → `action=get` to read the screens' names and levels, then `action=set` on that screen.
4. Read back and report what changed: which devices, and their new state. A device that lacks a feature (colour on a white-only bulb) is reported as such, not retried.
5. For an unrecognised mood, ask one concise question; recognised scenes run immediately.

## A platform north has no tool for

Stratis, Latch, SmartThings, Lutron, Ring, Nest, Yale, Matter/Thread, a building-management app:

1. Identify the platform. "Stratis" is a property-management smart-home platform (stratis.com), not the Linux `stratis` storage tool; never run system commands for a smart-home request.
2. `web_search` for its API: docs, authentication (OAuth, API key, token), the device list and control endpoints.
3. `list_dir` the workspace and `search_files` for an existing tool (e.g. `kasa_tool.py`) before creating anything, and follow its naming.
4. `create_tool` a Python tool for the platform in the same pattern as `kasa_tool.py`: auth, discovery, control.
5. Use the new tool for the request. If no usable API documentation turns up, say what was found and ask for credentials or app details; never fall back to bare bash.
