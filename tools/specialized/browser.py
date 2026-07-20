"""Browser automation via Playwright — navigate, fill, click, scrape.

Allows north agents to interact with web UIs: check grades, fill applications,
scrape bookings, take screenshots. Uses a single persistent Chromium context
so login sessions span calls within the same task.

Requires ``playwright install chromium`` after the package is installed.
"""

from __future__ import annotations

import base64
from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class BrowserTool(Tool):
    """Automates a headless Chromium browser for web interactions.

    Each task gets a shared browser context (cookies, localStorage persist
    within the task so login forms only need one fill). Supports navigate,
    click, fill, screenshot, extract text, and get page info.
    """

    name = "browser"
    is_mutating = False
    description = (
        "Automate a real web browser (headless Chromium) to interact with web UIs. "
        "Use it to log into portals, fill forms, check grades/status, scrape data from "
        "pages that require JavaScript rendering, or take screenshots of live websites. "
        "Actions: 'navigate' (go to URL), 'click' (click element by CSS selector), "
        "'fill' (type into an input), 'screenshot' (capture page), 'extract' (get page "
        "text), 'info' (current URL and title). The browser is shared per task, so login "
        "state carries between actions."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "fill", "screenshot", "extract", "info"],
                "description": "Action to perform: navigate, click, fill, screenshot, extract, or info",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for 'navigate' action)",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for 'click' or 'fill' actions, e.g. '#email', '.submit-btn', 'button'",
            },
            "value": {
                "type": "string",
                "description": "Text to type for 'fill' action",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Timeout in milliseconds (default: 10000)",
                "default": 10000,
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def _ensure_browser(self) -> None:
        """Lazy-launch the browser on first use."""
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from None

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action", "")
        if action == "navigate":
            return f"Navigated to **{data.get('url', '')}** (title: {data.get('title', '')})"
        elif action == "click":
            return f"Clicked `{data.get('selector', '')}`"
        elif action == "fill":
            return f"Filled `{data.get('selector', '')}`"
        elif action == "screenshot":
            return f"Screenshot taken: {data.get('path', '')} ({data.get('size_bytes', 0)} bytes)"
        elif action == "extract":
            return data.get("text", "(no content)")
        elif action == "info":
            return f"URL: `{data.get('url', '')}` — Title: {data.get('title', '')}"
        return str(data)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = input.params.get("action", "")
        if not action:
            return ToolOutput(success=False, error="Parameter 'action' is required.")

        try:
            await self._ensure_browser()
        except RuntimeError as exc:
            return ToolOutput(success=False, error=str(exc))
        except Exception as exc:
            return ToolOutput(success=False, error=f"Browser launch failed: {exc}")

        timeout = input.params.get("timeout_ms", 10000)

        try:
            if action == "navigate":
                url = input.params.get("url", "")
                if not url:
                    return ToolOutput(success=False, error="Parameter 'url' is required for navigate action.")
                await self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                await self._page.wait_for_load_state("networkidle", timeout=timeout)
                result = {
                    "action": "navigate",
                    "url": self._page.url,
                    "title": await self._page.title(),
                }
                return ToolOutput(success=True, data=result)

            elif action == "click":
                selector = input.params.get("selector", "")
                if not selector:
                    return ToolOutput(success=False, error="Parameter 'selector' is required for click action.")
                await self._page.wait_for_selector(selector, timeout=timeout)
                await self._page.click(selector)
                await self._page.wait_for_load_state("networkidle", timeout=timeout)
                return ToolOutput(
                    success=True,
                    data={"action": "click", "selector": selector, "url": self._page.url},
                )

            elif action == "fill":
                selector = input.params.get("selector", "")
                value = input.params.get("value", "")
                if not selector:
                    return ToolOutput(success=False, error="Parameter 'selector' is required for fill action.")
                await self._page.wait_for_selector(selector, timeout=timeout)
                await self._page.fill(selector, value)
                return ToolOutput(
                    success=True,
                    data={"action": "fill", "selector": selector, "value_preview": value[:50]},
                )

            elif action == "screenshot":
                from tools._path import resolve_path

                path_str = input.params.get("path", "browser_screenshot.png")
                resolved = resolve_path(path_str, input.params.get("workspace"))
                if resolved is None:
                    return ToolOutput(
                        success=False,
                        error="Path escapes workspace root or points to blocked directory.",
                    )
                resolved.parent.mkdir(parents=True, exist_ok=True)
                await self._page.screenshot(path=str(resolved), full_page=True)
                image_bytes = resolved.read_bytes()
                b64 = base64.b64encode(image_bytes).decode("ascii")
                return ToolOutput(
                    success=True,
                    data={
                        "action": "screenshot",
                        "path": str(resolved),
                        "size_bytes": len(image_bytes),
                        "base64_image": b64,
                        "mime_type": "image/png",
                    },
                )

            elif action == "extract":
                text = await self._page.evaluate("document.body.innerText")
                title = await self._page.title()
                # Truncate to avoid context blowout
                if len(text) > 10000:
                    text = text[:10000] + "\n\n[truncated — page too long]"
                return ToolOutput(
                    success=True,
                    data={"action": "extract", "title": title, "text": text},
                )

            elif action == "info":
                return ToolOutput(
                    success=True,
                    data={
                        "action": "info",
                        "url": self._page.url,
                        "title": await self._page.title(),
                    },
                )

            else:
                return ToolOutput(
                    success=False,
                    error=f"Unknown action: {action!r}."
                    " Valid: navigate, click, fill, screenshot, extract, info",
                )

        except Exception as exc:
            return ToolOutput(success=False, error=f"Browser {action} failed: {exc}")
