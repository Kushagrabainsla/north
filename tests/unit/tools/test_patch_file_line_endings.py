"""Tests for PatchFileTool CRLF and BOM preservation.

A model writes ``old_string`` with ``\\n`` and never includes an invisible BOM,
so matching must happen in LF-normalized, BOM-stripped space. Writing must then
restore the file's original line ending and BOM byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools._read_tracker import record_read
from tools.models import ToolInput
from tools.specialized.patch_file import PatchFileTool


async def _patch(tool: PatchFileTool, path: Path, tmp_path: Path, **params: object) -> object:
    # patch_file requires the file to have been read first.
    record_read(None, str(path))
    return await tool.run(ToolInput(params={"path": str(path), "workspace": str(tmp_path), **params}))


@pytest.mark.asyncio
async def test_crlf_file_matches_lf_old_string_and_preserves_crlf(tmp_path: Path):
    f = tmp_path / "win.py"
    # Write CRLF bytes explicitly; newline="" stops Python translating them.
    f.write_text("a = 1\r\nb = 2\r\n", encoding="utf-8", newline="")

    tool = PatchFileTool()
    # old_string uses LF - exactly what a model emits.
    out = await _patch(tool, f, tmp_path, old_string="b = 2", new_string="b = 3")

    assert out.success, out.error
    raw = f.read_bytes()
    assert raw == b"a = 1\r\nb = 3\r\n"  # CRLF preserved, change applied


@pytest.mark.asyncio
async def test_bom_file_edit_preserves_bom(tmp_path: Path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfheader = 1\nvalue = 2\n")  # UTF-8 BOM prefix

    tool = PatchFileTool()
    # Matching text at the very top of the file - the BOM must not defeat it.
    out = await _patch(tool, f, tmp_path, old_string="header = 1", new_string="header = 9")

    assert out.success, out.error
    raw = f.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM still present
    assert raw == b"\xef\xbb\xbfheader = 9\nvalue = 2\n"


@pytest.mark.asyncio
async def test_crlf_and_bom_together(tmp_path: Path):
    f = tmp_path / "both.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\r\ny = 2\r\n")

    tool = PatchFileTool()
    out = await _patch(tool, f, tmp_path, old_string="y = 2", new_string="y = 20")

    assert out.success, out.error
    assert f.read_bytes() == b"\xef\xbb\xbfx = 1\r\ny = 20\r\n"


@pytest.mark.asyncio
async def test_lf_file_unchanged_behaviour(tmp_path: Path):
    """Regression: the common plain-LF, no-BOM case is byte-identical to before."""
    f = tmp_path / "plain.py"
    f.write_bytes(b"one = 1\ntwo = 2\n")

    tool = PatchFileTool()
    out = await _patch(tool, f, tmp_path, old_string="two = 2", new_string="two = 3")

    assert out.success, out.error
    assert f.read_bytes() == b"one = 1\ntwo = 3\n"


@pytest.mark.asyncio
async def test_crlf_multiline_edit_preserves_crlf(tmp_path: Path):
    f = tmp_path / "multi.py"
    f.write_text("def f():\r\n    return 1\r\n", encoding="utf-8", newline="")

    tool = PatchFileTool()
    out = await _patch(
        tool,
        f,
        tmp_path,
        old_string="def f():\n    return 1",
        new_string="def f():\n    return 2",
    )

    assert out.success, out.error
    assert f.read_bytes() == b"def f():\r\n    return 2\r\n"
