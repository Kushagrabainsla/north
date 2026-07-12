from pagination import paginate


def test_returns_list():
    assert isinstance(paginate([1, 2, 3], 1, 2), list)
