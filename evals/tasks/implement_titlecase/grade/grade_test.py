from textutils import title_case


def test_basic():
    assert title_case("hello world") == "Hello World"


def test_mixed_case():
    assert title_case("hELLO wORLD") == "Hello World"


def test_single_word():
    assert title_case("python") == "Python"
