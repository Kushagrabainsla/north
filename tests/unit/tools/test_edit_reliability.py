"""Making an edit land the first time.

Exact substring matching is right, and brittle in one specific way: a model
reproducing a block from memory gets the characters right and the leading
whitespace wrong. The edit failed with "check exact whitespace and newlines" -
which never said what the whitespace actually was, so the retry was another
guess, at a full round-trip on a model generating around twenty tokens a second.

Three changes: read the file before editing it, match on line content when the
exact bytes miss, and show the real text when nothing matches at all.
"""

from __future__ import annotations

import pytest

from tools._read_tracker import record_read, reset, was_read
from tools.models import ToolInput
from tools.specialized._edit_match import Match, find_unique, indents_for, reindent
from tools.specialized.patch_file import PatchFileTool

SOURCE = '''class Cart:
    def apply_discount(self, amount, percent):
        """Apply a percentage discount."""
        return amount - (amount * percent)

    def subtotal(self, items):
        return sum(i["price"] for i in items)
'''


@pytest.fixture(autouse=True)
def _clean_tracker():
    reset()
    yield
    reset()


class TestFindingTheTextToReplace:
    def test_an_exact_match_is_used_as_is(self) -> None:
        match, error = find_unique(SOURCE, "        return amount - (amount * percent)")

        assert error == ""
        assert match is not None and match.how == "exact"

    def test_a_single_line_at_any_indentation_is_still_an_exact_substring(self) -> None:
        """One line without its leading spaces still occurs verbatim inside the
        indented line, so it needs no tolerance - and the span excludes the
        indentation, so the replacement lands correctly anyway."""
        match, error = find_unique(SOURCE, "return amount - (amount * percent)")

        assert error == ""
        assert match is not None and match.how == "exact"
        assert SOURCE[match.start : match.end] == "return amount - (amount * percent)"

    def test_a_multi_line_block_at_the_wrong_indentation_matches(self) -> None:
        remembered = 'def subtotal(self, items):\n    return sum(i["price"] for i in items)'

        match, error = find_unique(SOURCE, remembered)

        assert error == ""
        assert match is not None and match.how == "reindented"

    def test_an_ambiguous_exact_match_is_refused_with_line_numbers(self) -> None:
        """Silently editing the wrong of two identical blocks is far worse than
        failing, so uniqueness is never traded for tolerance."""
        content = "x = 1\ny = 2\nx = 1\n"

        match, error = find_unique(content, "x = 1")

        assert match is None
        assert "appears 2 times" in error
        assert "lines 1, 3" in error

    def test_an_ambiguous_tolerant_match_is_refused_too(self) -> None:
        """Two blocks that differ only in indentation are still two blocks."""
        content = "def a():\n    x = 1\n    return x\n\ndef b():\n        x = 1\n        return x\n"

        match, error = find_unique(content, "x = 1\nreturn x")

        assert match is None
        assert "when indentation is ignored" in error

    def test_a_miss_shows_what_is_actually_in_the_file(self) -> None:
        """"not found" tells the model nothing it did not already know."""
        match, error = find_unique(SOURCE, "        return amount - (amount * pct)")

        assert match is None
        assert "not found" in error
        assert "closest text in the file" in error
        assert "return amount - (amount * percent)" in error
        assert "|" in error, "the snippet should carry line numbers"

    def test_a_miss_with_nothing_similar_says_so_without_a_misleading_snippet(self) -> None:
        match, error = find_unique(SOURCE, "zzzzzzzz_nothing_like_this_zzzzzzzz")

        assert match is None
        assert "not found" in error


class TestKeepingIndentationCorrect:
    def test_the_replacement_is_shifted_to_the_files_indentation(self) -> None:
        """Pasting the model's indentation into a file that uses another leaves
        the file syntactically wrong - worse than the failed edit this rescues."""
        replacement = "return 0"

        assert reindent(replacement, "", "        ") == "        return 0"

    def test_a_multi_line_replacement_keeps_its_internal_shape(self) -> None:
        replacement = "if x:\n    return 1\nreturn 0"

        shifted = reindent(replacement, "", "    ")

        assert shifted == "    if x:\n        return 1\n    return 0"

    def test_blank_lines_are_left_alone(self) -> None:
        assert reindent("a\n\nb", "", "  ") == "  a\n\n  b"

    def test_an_exact_match_needs_no_shift(self) -> None:
        match = Match(0, 5, "exact")

        assert indents_for(SOURCE, "class", match) == ("", "")


class TestReadingBeforeEditing:
    async def _edit(self, tool, path, task_id, old, new):
        return await tool.run(
            ToolInput(params={"path": str(path), "old_string": old, "new_string": new, "task_id": task_id})
        )

    async def test_editing_an_unread_file_is_refused(self, tmp_path) -> None:
        target = tmp_path / "cart.py"
        target.write_text(SOURCE, encoding="utf-8")

        result = await self._edit(PatchFileTool(), target, "t1", "percent)", "pct)")

        assert result.success is False
        assert "Read" in result.error and "before editing" in result.error
        assert target.read_text() == SOURCE, "a refused edit must not touch the file"

    async def test_editing_after_reading_is_allowed(self, tmp_path) -> None:
        target = tmp_path / "cart.py"
        target.write_text(SOURCE, encoding="utf-8")
        record_read("t1", str(target))

        result = await self._edit(PatchFileTool(), target, "t1", "amount * percent", "amount * percent / 100")

        assert result.success is True, result.error
        assert "percent / 100" in target.read_text()

    async def test_a_read_by_one_task_does_not_license_an_edit_by_another(self, tmp_path) -> None:
        target = tmp_path / "cart.py"
        target.write_text(SOURCE, encoding="utf-8")
        record_read("t1", str(target))

        result = await self._edit(PatchFileTool(), target, "t2", "percent)", "pct)")

        assert result.success is False

    async def test_a_successful_edit_leaves_the_file_editable_again(self, tmp_path) -> None:
        """After an edit the model knows the new contents - it just wrote them -
        so a second edit in the same turn must not be blocked."""
        target = tmp_path / "cart.py"
        target.write_text(SOURCE, encoding="utf-8")
        record_read("t1", str(target))
        tool = PatchFileTool()

        first = await self._edit(tool, target, "t1", "amount * percent", "amount * percent / 100")
        second = await self._edit(tool, target, "t1", 'sum(i["price"]', 'sum(i["cost"]')

        assert first.success is True and second.success is True, second.error

    async def test_a_call_with_no_task_is_never_blocked(self, tmp_path) -> None:
        """The direct-tool path and anything outside a task must keep working -
        this is a guard rail for agents, not a lock on the tool."""
        target = tmp_path / "cart.py"
        target.write_text(SOURCE, encoding="utf-8")

        result = await self._edit(PatchFileTool(), target, None, "amount * percent", "amount * 0")

        assert result.success is True, result.error


class TestTheTrackerItself:
    def test_a_read_is_remembered_per_task_and_path(self) -> None:
        record_read("t1", "/a.py")

        assert was_read("t1", "/a.py") is True
        assert was_read("t1", "/b.py") is False
        assert was_read("t2", "/a.py") is False

    def test_an_unattributed_call_is_always_allowed(self) -> None:
        assert was_read(None, "/never-read.py") is True
        assert was_read("", "/never-read.py") is True

    def test_it_does_not_grow_without_bound(self) -> None:
        """A guard rail must never become a leak."""
        from tools import _read_tracker

        for i in range(_read_tracker._MAX_ENTRIES + 50):
            record_read("t1", f"/f{i}.py")

        assert len(_read_tracker._reads) <= _read_tracker._MAX_ENTRIES
        assert was_read("t1", f"/f{_read_tracker._MAX_ENTRIES + 49}.py") is True, "newest kept"
        assert was_read("t1", "/f0.py") is False, "oldest dropped"
