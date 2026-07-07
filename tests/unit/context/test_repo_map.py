"""Unit tests for the repository map (context/repo_map.py)."""

from __future__ import annotations

from pathlib import Path

from context.repo_map import build_repo_map


def test_lists_python_top_level_symbols(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "class Foo:\n    def method(self):\n        pass\n\ndef bar(x, y):\n    return x\n"
    )
    m = build_repo_map(str(tmp_path))
    assert "app.py" in m
    assert "class Foo" in m
    assert "def bar(x, y)" in m
    # Nested method is not a top-level symbol.
    assert "method" not in m


def test_regex_symbols_for_non_python(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text("export function greet(name) {}\nclass Widget {}\n")
    m = build_repo_map(str(tmp_path))
    assert "index.js" in m
    assert "greet" in m
    assert "Widget" in m


def test_empty_or_missing_workspace_returns_empty(tmp_path: Path) -> None:
    assert build_repo_map("") == ""
    assert build_repo_map(str(tmp_path / "does-not-exist")) == ""


def test_skips_pruned_and_hidden_dirs(tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("function hidden() {}\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.py").write_text("def secret():\n    pass\n")
    (tmp_path / "real.py").write_text("def visible():\n    pass\n")

    m = build_repo_map(str(tmp_path))
    assert "visible" in m
    assert "hidden" not in m
    assert "secret" not in m


def test_respects_char_budget(tmp_path: Path) -> None:
    for i in range(40):
        (tmp_path / f"mod{i}.py").write_text(f"def f{i}():\n    pass\n")
    m = build_repo_map(str(tmp_path), max_chars=200)
    assert len(m) <= 260  # roughly bounded; never dumps the whole repo


def test_entry_point_files_rank_first(tmp_path: Path) -> None:
    (tmp_path / "zzz.py").write_text("def z():\n    pass\n")
    (tmp_path / "main.py").write_text("def m():\n    pass\n")
    m = build_repo_map(str(tmp_path))
    assert m.index("main.py") < m.index("zzz.py")


def test_files_with_no_symbols_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_text("# just a comment\nX = 1\n")
    (tmp_path / "has.py").write_text("def something():\n    pass\n")
    m = build_repo_map(str(tmp_path))
    assert "has.py" in m
    assert "empty.py" not in m
