from interval import merge_overlapping


def test_touching_intervals_merge():
    assert merge_overlapping([[1, 4], [4, 5]]) == [[1, 5]]
