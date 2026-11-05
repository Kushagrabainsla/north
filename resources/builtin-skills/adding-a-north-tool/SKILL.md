---
name: adding-a-north-tool
description: "Use when North must create or update an atomic executable capability because no registered tool can perform a required action."
intents: [create-tool]
domains: [general, engineering]
---
# Adding a tool to north

> A tool is one action north can take, written as one Python file. Reuse first; create only for a real gap; the job is done when the tool is live and the user's request works with it, not when the file exists.

## Decide first
- Call `create_tool` with `action='list'`. If a tool already owns the action, use it, or `read` and `update` it. A new phrasing or workflow is not a reason for another tool.
- A one-off job is done with `bash` or `write_file`, not a tool. A procedure (steps and judgement) is a skill; an ordered, repeatable process is a flow.
- Build one tool for the user's task, not one per API endpoint: `get_weather` that answers the question, not `geocode` plus `forecast`.

## Ask once, before writing
Work out what you can yourself (which public service, its URL, its response format). Then, if anything only the user can decide is still open (which account, which device, what counts as done, whether it may change or send anything), ask it all in **one** `ask_user` call, one item of `questions` each. Do not ask what you can find out, and do not ask again for what the user already said.

Prefer a service that needs no key (Open-Meteo for weather, for example). When a key or password is unavoidable, it is one of the user's secrets. `create_tool` `action='list'` names the secrets already saved; use one that fits. Otherwise choose a short name (`openweather`), and in the same `ask_user` call ask the user to add it on the dashboard, under Settings, Secrets, with that name (`requires_user_action=true`, its own call, if nothing else is open). You only ever see a secret's name: never ask for its value in chat and never write it into the code.

## What north expects from the file
North checks most of these itself before the test call, and a draft that misses one comes back with the exact fix to make.

1. One class that subclasses `Tool` (from `tools.base`), takes no constructor arguments, and sets:
   - `name`: snake_case, specific (`get_weather`, not `weather_tool` or `helper`).
   - `description`: what it does, when to use it, and when not to. Write it for someone who has never seen the service: name the inputs, units and limits.
   - `parameters_schema`: a JSON object schema. Unambiguous names (`city`, not `q`; `user_id`, not `user`), a description for each, an `enum` for a fixed set of choices, and `required` listing only what has no sensible default.
2. `async def run(self, input: ToolInput) -> ToolOutput`. The call's arguments are the dict `input.params`; nothing else carries them.
3. Return, never raise: `ToolOutput(success=True, data={...})`, or `ToolOutput(success=False, error="...")` with an error that says what to change, ideally with a valid example (`"Unknown city 'Pariss'. Pass a city name such as 'Paris' or 'Paris, Texas'."`). Use `failure_kind="not_found"` when the thing asked for does not exist.
4. Say when a call changes something. A tool that writes files, sends, posts, buys, deletes or switches a device sets `is_mutating = True`, so north asks before it runs. A tool where only some calls change things overrides `mutates(params)` instead (below). A tool that only reads sets neither.
5. Never freeze north: it is `async`. Use `httpx.AsyncClient(timeout=...)` for the web, `await asyncio.sleep` to wait, and `await asyncio.to_thread(fn, ...)` for a blocking library call. `requests` is not installed.
6. Only the standard library and what north already installs: `httpx`, `bs4` (BeautifulSoup), `pydantic`, `yaml`, `numpy`, `psutil`, `PIL`, `pypdf`, `docx`. North cannot install packages for a custom tool.
7. Files the tool keeps live under `settings.north_home / "tool-data" / "<tool name>"` (`from config.settings import settings`). Never write a fixed path to north's home or a user folder (`/Users/...`) into the code: north's home can be anywhere. Never read `os.environ`: a key comes from `secret(name)`, below.
8. Return what a person or the next step needs, by name, not raw API payloads: `"city": "Paris"`, not `"geo_id": 2988507`. Keep it small; cut long lists and say how many were left out. `format_output(data)` turns `data` into one or two readable lines.

A tool whose calls are mixed says which ones change things:

```python
    def mutates(self, params: dict | None = None) -> bool:
        return (params or {}).get("action") != "status"  # reading the status changes nothing
```

A tool that needs a key declares it, and reads it inside `run`. North gives the tool only the secrets it declares, names them on the approval card, and hides their values from anything the tool returns:

```python
from config.secret_store import secret


class GetForecastTool(Tool):
    uses_secrets = ("openweather",)

    async def run(self, input: ToolInput) -> ToolOutput:
        key = secret("openweather")
```

## A complete example

```python
from __future__ import annotations

from typing import Any

import httpx

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST = "https://api.open-meteo.com/v1/forecast"


class GetWeatherTool(Tool):
    name = "get_weather"
    description = (
        "Current weather and today's high and low for a city, from Open-Meteo (no account needed). "
        "Use for questions like 'what is the weather in Pune'. Not for forecasts beyond today."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name, optionally with country: 'Pune' or 'Paris, France'."},
            "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "description": "Default celsius."},
        },
        "required": ["city"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return f"{data['city']}: {data['now']}{data['unit']} now, {data['low']}-{data['high']}{data['unit']} today"

    async def run(self, input: ToolInput) -> ToolOutput:
        city = str(input.params.get("city") or "").strip()
        units = input.params.get("units") or "celsius"
        if not city:
            return ToolOutput(success=False, error="Pass a city name, such as 'Pune'.")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                found = (await client.get(_GEOCODE, params={"name": city, "count": 1})).json().get("results")
                if not found:
                    return ToolOutput(
                        success=False, failure_kind="not_found",
                        error=f"No city called {city!r}. Check the spelling or add the country: 'Paris, France'.",
                    )
                place = found[0]
                response = await client.get(_FORECAST, params={
                    "latitude": place["latitude"], "longitude": place["longitude"], "current": "temperature_2m",
                    "daily": "temperature_2m_max,temperature_2m_min", "forecast_days": 1, "temperature_unit": units,
                })
                response.raise_for_status()
                weather = response.json()
        except httpx.HTTPError as exc:
            return ToolOutput(success=False, error=f"Open-Meteo did not answer ({exc}). Try again in a minute.")
        return ToolOutput(success=True, data={
            "city": f"{place['name']}, {place.get('country', '')}".strip(", "),
            "unit": "°C" if units == "celsius" else "°F",
            "now": weather["current"]["temperature_2m"],
            "high": weather["daily"]["temperature_2m_max"][0],
            "low": weather["daily"]["temperature_2m_min"][0],
        })
```

## Create it and prove it
1. Call `create_tool` with `action='create'`, the tool's `name`, the full file as `content`, and `test_params`: the smallest real input that shows it works (`{"city": "Pune"}`). The user is asked once. North then checks the file, makes that one real call (anything it changes still asks), and switches the tool on if it succeeds.
2. If it comes back as a draft, read the error, fix exactly that, and send the whole corrected file again (`create` replaces your own draft; `update` changes a live tool). A failing version never replaces the live one. If the test failed because a service could not be reached, say so instead of rewriting working code.
3. Then use the tool for the user's original request and report the real result.

## Common mistakes
- Guessing how arguments arrive (`input.parameters`, `input.get(...)`): it is always `input.params`.
- `raise ValueError(...)` for a bad input: return `ToolOutput(success=False, error=...)` that says how to fix it.
- `requests.get(...)` or `time.sleep(...)`: both freeze north; use `httpx.AsyncClient` and `await asyncio.sleep`.
- A tool that sends, writes or switches something without `is_mutating = True`: north would run it without asking.
- Returning a whole API response: pick the fields that answer the question.
- A key asked for in chat, written into the code, or read from `os.environ`: it is a secret the user adds on the dashboard, and the tool reads it with `secret(name)`.
- A test call that changes something real when a reading call would prove the same.

## Done when
- `create_tool` reports the tool live, the user's request was answered with it, and you said what it returned. If its test failed, say "tool draft created" and why; creating the file is never the user's outcome.
