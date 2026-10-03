from shop.cart import Cart


def test_subtotal_adds_up():
    cart = Cart()
    cart.add("a", 250, 2)
    cart.add("b", 100)
    assert cart.subtotal_cents() == 600


def test_same_item_merges():
    cart = Cart()
    cart.add("a", 250)
    cart.add("a", 250)
    assert len(cart.items) == 1 and cart.items[0].qty == 2


def test_total_without_coupons_is_the_subtotal():
    cart = Cart()
    cart.add("a", 999)
    assert cart.total_cents() == 999
