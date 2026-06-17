import tempfile
from pathlib import Path

import pytest

from tools.analysis.check_types import _parse_error_line
from tools.models import ToolInput
from tools.semantic.search_symbols import SearchSymbolsTool
from tools.specialized.patch_file import PatchFileTool


@pytest.mark.asyncio
async def test_patch_file_search_replace_blocks():
    tool = PatchFileTool()
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w+", delete=False) as tmp:
        tmp.write("def foo():\n    print('hello')\n\ndef bar():\n    print('world')\n")
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        # Test applying a single SEARCH/REPLACE block
        new_string = (
            "<<<<<<< SEARCH\n"
            "def foo():\n"
            "    print('hello')\n"
            "=======\n"
            "def foo():\n"
            "    print('hello world!')\n"
            ">>>>>>> REPLACE"
        )
        res = await tool.run(ToolInput(params={"path": str(tmp_path), "new_string": new_string}))
        assert res.success
        assert "hello world!" in tmp_path.read_text()

        # Test applying multiple SEARCH/REPLACE blocks
        new_string_multi = (
            "<<<<<<< SEARCH\n"
            "def foo():\n"
            "    print('hello world!')\n"
            "=======\n"
            "def foo_new():\n"
            "    print('hello new!')\n"
            ">>>>>>> REPLACE\n\n"
            "<<<<<<< SEARCH\n"
            "def bar():\n"
            "    print('world')\n"
            "=======\n"
            "def bar_new():\n"
            "    print('world new!')\n"
            ">>>>>>> REPLACE"
        )
        res = await tool.run(ToolInput(params={"path": str(tmp_path), "new_string": new_string_multi}))
        assert res.success
        content = tmp_path.read_text()
        assert "foo_new" in content
        assert "bar_new" in content
    finally:
        tmp_path.unlink()


@pytest.mark.asyncio
async def test_search_symbols_multi_language():
    tool = SearchSymbolsTool()

    # JS/TS Test
    with tempfile.NamedTemporaryFile(suffix=".ts", mode="w+", delete=False) as tmp:
        tmp.write("class UserService {\n  async getUser(id: number) {\n  }\n}\nfunction helper() {}")
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        res = await tool.run(ToolInput(params={"path": str(tmp_path), "type": "all"}))
        assert res.success
        symbols = res.data["symbols"]
        assert len(symbols) == 3
        assert symbols[0]["name"] == "UserService"
        assert symbols[0]["type"] == "class"
        assert symbols[1]["name"] == "getUser"
        assert symbols[1]["type"] == "function"
        assert symbols[2]["name"] == "helper"
    finally:
        tmp_path.unlink()

    # Go Test
    with tempfile.NamedTemporaryFile(suffix=".go", mode="w+", delete=False) as tmp:
        tmp.write("package main\ntype Config struct {}\nfunc (c *Config) Load() {}")
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        res = await tool.run(ToolInput(params={"path": str(tmp_path), "type": "all"}))
        assert res.success
        symbols = res.data["symbols"]
        assert len(symbols) == 2
        assert symbols[0]["name"] == "Config"
        assert symbols[0]["type"] == "class"
        assert symbols[1]["name"] == "Load"
        assert symbols[1]["type"] == "function"
    finally:
        tmp_path.unlink()


@pytest.mark.asyncio
async def test_search_symbols_python_async_def():
    """ast.AsyncFunctionDef is a distinct node - async defs must not be missed (R5#31)."""
    tool = SearchSymbolsTool()
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w+", delete=False) as tmp:
        tmp.write(
            "def sync_fn():\n    pass\n\n"
            "async def async_fn(x):\n    return x\n\n"
            "class Service:\n"
            "    async def handler(self):\n        pass\n"
        )
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        res = await tool.run(ToolInput(params={"path": str(tmp_path), "type": "function"}))
        assert res.success
        by_name = {s["name"]: s for s in res.data["symbols"]}
        assert set(by_name) == {"sync_fn", "async_fn", "handler"}
        assert by_name["sync_fn"]["signature"].startswith("def ")
        assert by_name["async_fn"]["signature"].startswith("async def ")
        assert by_name["handler"]["signature"].startswith("async def ")
    finally:
        tmp_path.unlink()


def test_parse_error_line():
    # Python mypy parser test
    parsed = _parse_error_line("main.py:10: error: Need type annotation", "python")
    assert parsed is not None
    assert parsed["file"] == "main.py"
    assert parsed["line"] == 10
    assert parsed["severity"] == "error"
    assert parsed["message"] == "Need type annotation"

    # TypeScript parser test (pattern 1)
    parsed = _parse_error_line("src/index.ts(12,5): error TS2322: Type 'string' is not assignable", "typescript")
    assert parsed is not None
    assert parsed["file"] == "src/index.ts"
    assert parsed["line"] == 12
    assert parsed["column"] == 5
    assert parsed["message"] == "Type 'string' is not assignable"

    # Go vet parser test
    parsed = _parse_error_line("main.go:5:2: printf format %s reads arg of type int", "go")
    assert parsed is not None
    assert parsed["file"] == "main.go"
    assert parsed["line"] == 5
    assert parsed["column"] == 2
    assert parsed["message"] == "printf format %s reads arg of type int"
