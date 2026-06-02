"""FetchUrlTool — download and extract readable text from a URL.

Fetches the page with httpx (already a project dependency), strips HTML
tags via BeautifulSoup (already a project dependency), and returns the
plain-text content capped at _MAX_CHARS so it fits in a model context window.
"""

from __future__ import annotations

import asyncio

import httpx

from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_MAX_CHARS = 30_000
_TIMEOUT = 20.0


class FetchUrlTool(Tool):
    """Fetch a URL and return its readable text content."""

    name = "fetch_url"
    description = (
        "Fetch a URL and return its readable text content. "
        "Use for reading documentation pages, articles, job postings, or any specific URL "
        "whose full content matters. Returns plain text, not HTML."
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
        if not url.startswith(("http://", "https://")):
            return ToolOutput(success=False, error="URL must start with http:// or https://")
        return await asyncio.to_thread(_fetch_sync, url)


def _fetch_sync(url: str) -> ToolOutput:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(
                url,
                headers={"User-Agent": "north/1.0 (personal AI assistant)"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return ToolOutput(success=False, error=f"HTTP {e.response.status_code}: {url}")
    except httpx.RequestError as e:
        return ToolOutput(success=False, error=f"Request failed: {e}")

    content_type = resp.headers.get("content-type", "")
    text = _strip_html(resp.text) if "html" in content_type else resp.text

    if len(text) > _MAX_CHARS:
        omitted = len(text) - _MAX_CHARS
        text = text[:_MAX_CHARS] + f"\n\n[…{omitted} chars truncated]"

    return ToolOutput(
        success=True,
        data={
            "url": url,
            "content": text,
            "content_type": content_type,
            "chars": len(text),
        },
    )


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup  # already in project deps

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return " ".join(soup.get_text(separator="\n").split())
