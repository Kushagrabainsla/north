"""FetchUrlTool - download and extract readable text from a URL.

Fetches the page via the SSRF-guarded helper in utils/net (private/internal
addresses are blocked, redirects re-validated, size and time capped), strips
HTML tags, and returns the plain-text content capped at _MAX_CHARS so it fits
in a model context window.
"""

from __future__ import annotations

import asyncio

import httpx

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from utils.net import UnsafeUrlError, fetch_url_text
from utils.text import strip_html

_MAX_CHARS = 30_000


class FetchUrlTool(Tool):
    """Fetch a URL and return its readable text content."""

    name = "fetch_url"
    description = (
        "Fetch a public URL and return its readable text content. The first thing to reach "
        "for when you have a URL and need what it says: documentation, articles, job "
        "postings, any page whose words are the point. Returns plain text, not HTML, and "
        "does not run JavaScript. "
        "If the text comes back empty, tiny, or is clearly a page shell waiting for "
        "JavaScript, that page needs the browser tool instead. So does anything behind a "
        "login, or any page you must click or type into. "
        "Private/internal network addresses are blocked."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
        },
        "required": ["url"],
    }

    async def run(self, input: ToolInput) -> ToolOutput:
        url = input.params.get("url", "").strip()
        if not url:
            return ToolOutput(success=False, error="Parameter 'url' is required.")
        return await asyncio.to_thread(_fetch_sync, url)


def _fetch_sync(url: str) -> ToolOutput:
    try:
        fetched = fetch_url_text(url)
    except UnsafeUrlError as e:
        return ToolOutput(success=False, error=str(e))
    except httpx.HTTPStatusError as e:
        return ToolOutput(success=False, error=f"HTTP {e.response.status_code}: {url}")
    except httpx.RequestError as e:
        return ToolOutput(success=False, error=f"Request failed: {e}")

    text = strip_html(fetched.text) if "html" in fetched.content_type else fetched.text

    if len(text) > _MAX_CHARS:
        omitted = len(text) - _MAX_CHARS
        text = text[:_MAX_CHARS] + f"\n\n[…{omitted} chars truncated]"

    return ToolOutput(
        success=True,
        data={
            "url": fetched.url,
            "content": text,
            "content_type": fetched.content_type,
            "chars": len(text),
        },
    )
