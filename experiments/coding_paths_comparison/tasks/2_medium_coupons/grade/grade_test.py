import pytest
from shop.cart import Cart
from shop.coupons import CouponError


def cart(*prices):
    c = Cart()
    for i, p in enumerate(prices):
        c.add(f"s{i}", p)
    return c


def test_percent_rounds_down():
    c = cart(999)  # 10% of 999 = 99.9 -> 99 off
    c.apply_coupon("SAVE10")
    assert c.total_cents() == 900


def test_fixed_off():
    c = cart(2000)
    c.apply_coupon("FIVEOFF")
    assert c.total_cents() == 1500


def test_percent_first_then_fixed():
    c = cart(2000)  # 25% -> 1500, then 500 off -> 1000
    c.apply_coupon("FIVEOFF")
    c.apply_coupon("SAVE25")
    assert c.total_cents() == 1000


def test_second_percent_replaces_the_first():
    c = cart(1000)
    c.apply_coupon("SAVE10")
    c.apply_coupon("SAVE25")
    assert c.total_cents() == 750


def test_second_fixed_replaces_the_first():
    c = cart(2000)
    c.apply_coupon("FIVEOFF")
    c.apply_coupon("TENOFF")
    assert c.total_cents() == 1000


def test_same_code_twice_changes_nothing():
    c = cart(1000)
    c.apply_coupon("SAVE10")
    c.apply_coupon("SAVE10")
    assert c.total_cents() == 900


def test_never_below_zero():
    c = cart(300)
    c.apply_coupon("TENOFF")
    assert c.total_cents() == 0


def test_subtotal_is_unchanged():
    c = cart(1000)
    c.apply_coupon("SAVE25")
    assert c.subtotal_cents() == 1000


def test_unknown_code_raises_and_leaves_the_cart_alone():
    c = cart(1000)
    c.apply_coupon("SAVE10")
    with pytest.raises(CouponError):
        c.apply_coupon("NOPE")
    assert c.total_cents() == 900


def test_a_failed_replacement_keeps_the_old_coupon():
    c = cart(1000)
    c.apply_coupon("SAVE25")
    with pytest.raises(CouponError):
        c.apply_coupon("SAVE99")
    assert c.total_cents() == 750
