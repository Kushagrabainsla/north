from pricing import apply_discount


def test_discount_applied():
    assert apply_discount(100, 20) == 80


def test_zero_discount():
    assert apply_discount(50, 0) == 50


def test_half_off():
    assert apply_discount(200, 50) == 100
