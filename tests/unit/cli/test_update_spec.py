from __future__ import annotations

from cli.update_spec import pinned_git_spec


def test_pinned_git_spec_pins_unreferenced_urls_to_main() -> None:
    assert pinned_git_spec("https://github.com/o/north.git") == "git+https://github.com/o/north.git@main"


def test_pinned_git_spec_preserves_existing_references() -> None:
    assert pinned_git_spec("https://github.com/o/north.git@main") == "git+https://github.com/o/north.git@main"
    assert pinned_git_spec("https://github.com/o/north.git@v1.2.3") == "git+https://github.com/o/north.git@v1.2.3"
