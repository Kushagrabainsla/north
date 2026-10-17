"""FetchUrlTool - download and extract readable text from a URL.

Fetches the page via the SSRF-guarded helper in utils/net (private/internal
addresses are blocked, redirects re-validated, size and time capped), strips
HTML tags, and returns the plain-text content capped at _MAX_CHARS so it fits
in a model context window. An Atom or RSS feed comes back as one compact block
per entry, cut only between entries.
"""

from __future__ import annotations

import asyncio
from xml.etree import ElementTree

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

    feed = _feed_text(fetched.text) if _is_xml(fetched.content_type, fetched.text) else None
    text = fetched.text if feed is None else feed
    if feed is None and "html" in fetched.content_type:
        text = strip_html(text)

    if feed is None and len(text) > _MAX_CHARS:
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


def _is_xml(content_type: str, text: str) -> bool:
    return any(kind in content_type for kind in ("xml", "rss", "atom")) or text.lstrip().startswith("<?xml")


def _feed_text(xml: str) -> str | None:
    """An Atom or RSS feed as one block per entry, or None for any other document.

    A raw arXiv feed runs about 2,560 characters a paper, a third of it markup,
    so the cap cut a 25-paper search to its first 11. Compact, 18 fit in the
    same space, and the cut falls between entries instead of inside one.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    entries = [element for element in root.iter() if _local(element.tag) in ("entry", "item")]
    if not entries:
        return None
    blocks: list[str] = []
    used = 0
    for entry in entries:
        block = _entry_text(entry)
        if blocks and used + len(block) > _MAX_CHARS:
            break
        blocks.append(block[:_MAX_CHARS])
        used += len(block) + 2
    text = "\n\n".join(blocks)
    if len(blocks) < len(entries):
        text += f"\n\n[{len(entries) - len(blocks)} more entries not shown]"
    return text


def _entry_text(entry: ElementTree.Element) -> str:
    fields: dict[str, list[str]] = {}
    for child in entry:
        name = _local(child.tag)
        if name == "link" and child.get("href"):
            value = child.get("href", "") if child.get("rel", "alternate") == "alternate" else ""
        else:
            value = " ".join("".join(child.itertext()).split())
        if value:
            fields.setdefault(name, []).append(value)

    def first(*names: str) -> str:
        return next((fields[name][0] for name in names if name in fields), "")

    summary = first("summary", "description", "content")
    where = first("link", "id", "guid")
    when = first("published", "pubDate", "date", "updated")
    lines = [first("title"), " | ".join(part for part in (where, when) if part)]
    authors = fields.get("author", []) + fields.get("creator", [])
    if authors:
        lines.append("Authors: " + ", ".join(authors))
    if comment := first("comment"):
        lines.append(f"Comment: {comment}")
    if journal := first("journal_ref"):
        lines.append(f"Journal: {journal}")
    lines.append(strip_html(summary) if "<" in summary else summary)
    return "\n".join(line for line in lines if line)


def _local(tag: object) -> str:
    """A tag without its namespace: {http://www.w3.org/2005/Atom}entry is entry."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
