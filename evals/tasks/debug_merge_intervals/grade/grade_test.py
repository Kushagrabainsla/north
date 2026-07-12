from interval import merge_overlapping


def test_basic_overlap():
    assert merge_overlapping([[1, 3], [2, 6], [8, 10]]) == [[1, 6], [8, 10]]


def test_touching_merges():
    assert merge_overlapping([[1, 4], [4, 5]]) == [[1, 5]]


def test_no_overlap():
    assert merge_overlapping([[1, 2], [3, 4]]) == [[1, 2], [3, 4]]


def test_unsorted_input():
    assert merge_overlapping([[8, 10], [1, 3], [2, 6]]) == [[1, 6], [8, 10]]


def test_empty():
    assert merge_overlapping([]) == []


def test_fully_nested():
    assert merge_overlapping([[1, 10], [2, 3]]) == [[1, 10]]
