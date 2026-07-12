from cart import add_item


def test_appends_to_explicit_cart():
    assert add_item("x", []) == ["x"]
