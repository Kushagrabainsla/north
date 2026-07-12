from cart import add_item


def test_independent_calls_start_empty():
    assert add_item("a") == ["a"]
    assert add_item("b") == ["b"]


def test_explicit_cart_accumulates():
    cart = []
    add_item("x", cart)
    add_item("y", cart)
    assert cart == ["x", "y"]
