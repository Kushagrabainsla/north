from roman import to_roman


def test_units():
    assert to_roman(4) == "IV"
    assert to_roman(9) == "IX"


def test_tens():
    assert to_roman(40) == "XL"
    assert to_roman(90) == "XC"


def test_large_values():
    assert to_roman(1994) == "MCMXCIV"
    assert to_roman(3888) == "MMMDCCCLXXXVIII"


def test_boundaries():
    assert to_roman(1) == "I"
    assert to_roman(3999) == "MMMCMXCIX"
