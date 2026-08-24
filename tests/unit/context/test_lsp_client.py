"""Tests for the LSP client (#1). Pure-function tests run everywhere; the real
language-server tests are skipped when pyright-langserver isn't installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from context import lsp_client as L

_HAS_PYRIGHT = shutil.which("pyright-langserver") is not None
_needs_pyright = pytest.mark.skipif(not _HAS_PYRIGHT, reason="pyright-langserver not installed")


def test_python_symbol_position_locates_class_and_def():
    text = "import os\n\n\nclass Foo:\n    def bar(self):\n        return 1\n"
    cls = L.python_symbol_position(text, "Foo")
    assert cls == (3, 6)  # 0-based line 3, char after "class "
    fn = L.python_symbol_position(text, "bar")
    assert fn == (4, 8)


def test_python_symbol_position_locates_async_def():
    text = "async def fetch_data(url: str):\n    pass\n"
    pos = L.python_symbol_position(text, "fetch_data")
    assert pos == (0, 10)  # 0-based line 0, char 10 after "async def "


def test_python_symbol_position_locates_async_method_in_class():
    text = "class Handler:\n    async def handle_request(self):\n        pass\n"
    pos = L.python_symbol_position(text, "handle_request")
    assert pos == (1, 14)


def test_python_symbol_position_missing_returns_none():
    assert L.python_symbol_position("x = 1\n", "nope") is None


def test_apply_edits_replaces_ranges_last_to_first():
    text = "alpha beta gamma\n"
    # replace "alpha" (0,0)-(0,5) with "one" and "gamma" (0,11)-(0,16) with "three"
    edits = [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "newText": "one"},
        {"range": {"start": {"line": 0, "character": 11}, "end": {"line": 0, "character": 16}}, "newText": "three"},
    ]
    assert L._apply_edits(text, edits) == "one beta three\n"


def test_apply_workspace_edit_documentchanges(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("value = 1\nprint(value)\n", encoding="utf-8")
    edit = {
        "documentChanges": [
            {
                "textDocument": {"uri": "file://" + str(f), "version": 1},
                "edits": [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
                        "newText": "total",
                    },
                    {
                        "range": {"start": {"line": 1, "character": 6}, "end": {"line": 1, "character": 11}},
                        "newText": "total",
                    },
                ],
            }
        ]
    }
    files, edits, changed = L._apply_workspace_edit(tmp_path, edit)
    assert files == 1 and edits == 2 and changed == ["m.py"]
    assert f.read_text(encoding="utf-8") == "total = 1\nprint(total)\n"


def test_apply_workspace_edit_ignores_paths_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside.py"
    edit = {
        "changes": {
            "file://" + str(outside): [
                {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "newText": "X"}
            ]
        }
    }
    files, edits, changed = L._apply_workspace_edit(tmp_path, edit)
    assert files == 0 and changed == []


def test_server_command_for_unknown_suffix_is_none():
    assert L.server_command_for(".unknownlang") is None


@_needs_pyright
def test_real_rename_updates_definition_and_references(tmp_path: Path):
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    src = tmp_path / "calc.py"
    src.write_text(
        "def add(a, b):\n    return a + b\n\n\ndef use():\n    return add(1, 2)\n",
        encoding="utf-8",
    )
    files, edits, changed = L.rename_symbol(tmp_path, src, "add", "plus")
    assert files == 1
    after = src.read_text(encoding="utf-8")
    assert "def plus(a, b):" in after
    assert "return plus(1, 2)" in after
    assert "add(" not in after


@_needs_pyright
def test_real_find_references(tmp_path: Path):
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    src = tmp_path / "calc.py"
    src.write_text("def add(a, b):\n    return a + b\n\n\nx = add(1, 2)\n", encoding="utf-8")
    pos = L.python_symbol_position(src.read_text(), "add")
    refs = L.find_references(tmp_path, src, *pos)
    # at least the definition + the call site
    assert len(refs) >= 2
    assert all(r[0] == "calc.py" for r in refs)
