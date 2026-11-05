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


def test_server_command_for_unknown_suffix_is_none():
    assert L.server_command_for(".unknownlang") is None


@_needs_pyright
def test_real_find_references(tmp_path: Path):
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    src = tmp_path / "calc.py"
    src.write_text("def add(a, b):\n    return a + b\n\n\nx = add(1, 2)\n", encoding="utf-8")
    refs = L.find_references(tmp_path, src, 0, 4)  # `add` in "def add", 0-based
    # at least the definition + the call site
    assert len(refs) >= 2
    assert all(r[0] == "calc.py" for r in refs)
