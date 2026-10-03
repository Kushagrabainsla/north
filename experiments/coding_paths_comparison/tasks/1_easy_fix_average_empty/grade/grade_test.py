from calc import average


def test_empty_returns_zero():
    assert average([]) == 0.0


def test_still_averages():
    assert average([2, 4, 6]) == 4
