"""write_file normalizes dashes in prose it writes, but never in code/data files."""

from __future__ import annotations

from tools.models import ToolInput
from tools.universal.write_file import WriteFileTool


async def _write(tmp_path, name: str, content: str) -> str:
    tool = WriteFileTool()
    out = await tool.run(ToolInput(params={"path": name, "content": content, "workspace": str(tmp_path)}))
    assert out.success, out.error
    return (tmp_path / name).read_text(encoding="utf-8")


async def test_prose_file_has_dashes_normalized(tmp_path):
    written = await _write(tmp_path, "report.md", "north—the OS covers pages 10–20")
    assert "\u2014" not in written and "\u2013" not in written
    assert written == "north - the OS covers pages 10-20"


async def test_code_file_dashes_are_preserved(tmp_path):
    # A dash inside a string literal is test-verified data - it must survive verbatim.
    src = 'assert fmt(3, 5) == "3\u20135"  # en dash is intentional\n'
    written = await _write(tmp_path, "module.py", src)
    assert written == src


async def test_data_file_dashes_are_preserved(tmp_path):
    payload = '{"range": "10\u201320"}'
    written = await _write(tmp_path, "data.json", payload)
    assert written == payload
