from stats import moving_average


def test_window_two():
    assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]


def test_window_three():
    assert moving_average([1, 2, 3, 4, 5], 3) == [2.0, 3.0, 4.0]


def test_full_window():
    assert moving_average([2, 4], 2) == [3.0]


def test_k_larger_than_input():
    assert moving_average([1, 2], 5) == []
