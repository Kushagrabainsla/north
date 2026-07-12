from fizzbuzz import fizzbuzz


def test_fizzbuzz_15():
    assert fizzbuzz(15) == "FizzBuzz"


def test_fizz():
    assert fizzbuzz(9) == "Fizz"


def test_buzz():
    assert fizzbuzz(10) == "Buzz"


def test_plain():
    assert fizzbuzz(7) == "7"


def test_thirty():
    assert fizzbuzz(30) == "FizzBuzz"
