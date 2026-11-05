"""A secret saved from the dashboard is read back exactly as it was typed, or refused saying why."""

from __future__ import annotations

import pytest

from config.env_file import remove_env_key, write_env_key
from config.settings import Settings

TRICKY = ["p@ss w#rd", "$HOME and 'quotes' \"too\"", "back\\slash in it", "=equals=", "a\\nb"]


@pytest.mark.parametrize("password", TRICKY)
def test_a_password_is_read_back_exactly(tmp_path, password: str) -> None:
    """`p@ss w#rd` was cut at the `#`, read as a comment, when a value was written as it was."""
    write_env_key(tmp_path, "NORTH_KASA_PASSWORD", password)

    assert Settings(_env_file=tmp_path / ".env").kasa_password == password
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_a_plain_value_is_written_as_it_is_and_can_be_removed(tmp_path) -> None:
    write_env_key(tmp_path, "NORTH_GROQ_API_KEY", "gsk_real")
    write_env_key(tmp_path, "NORTH_KASA_USERNAME", "me@example.com")
    remove_env_key(tmp_path, "NORTH_GROQ_API_KEY")

    assert (tmp_path / ".env").read_text() == "NORTH_KASA_USERNAME=me@example.com\n"


@pytest.mark.parametrize(
    ("value", "why"), [("two\nlines", "line break"), ("x${HOME}", '"\\$\\{"'), ("ends\\", "backslash")]
)
def test_a_value_the_file_cannot_hold_exactly_is_refused(tmp_path, value: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        write_env_key(tmp_path, "NORTH_KASA_PASSWORD", value)
    assert not (tmp_path / ".env").exists()
