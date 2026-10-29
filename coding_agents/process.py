"""Reading from the coding agents' processes. Shared by every backend."""

from __future__ import annotations

import asyncio

from coding_agents.constants import MAX_STDERR_CHARS


async def tail(reader: asyncio.StreamReader) -> str:
    """The last of what a process wrote to stderr; read to the end so it never blocks on a full pipe."""
    last = ""
    while chunk := await reader.read(4096):
        last = (last + chunk.decode(errors="replace"))[-MAX_STDERR_CHARS:]
    return last
