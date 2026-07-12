from pagination import paginate


def test_first_page():
    assert paginate(list(range(10)), 1, 3) == [0, 1, 2]


def test_second_page():
    assert paginate(list(range(10)), 2, 3) == [3, 4, 5]


def test_last_partial_page():
    assert paginate(list(range(10)), 4, 3) == [9]


def test_out_of_range_is_empty():
    assert paginate(list(range(10)), 5, 3) == []
