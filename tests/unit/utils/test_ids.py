"""Tests for utils.ids — generate_id, generate_task_id."""

from __future__ import annotations

from utils.ids import generate_id, generate_task_id


def test_generate_id_returns_32_char_hex() -> None:
    value = generate_id()
    assert len(value) == 32
    assert all(c in "0123456789abcdef" for c in value)


def test_generate_id_is_unique_across_calls() -> None:
    ids = {generate_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_generate_task_id_has_expected_prefix_and_shape() -> None:
    value = generate_task_id()
    assert value.startswith("task_")
    assert len(value) == len("task_") + 12


def test_generate_task_id_is_unique_across_calls() -> None:
    ids = {generate_task_id() for _ in range(1000)}
    assert len(ids) == 1000
