from querystring import parse_query


def test_empty():
    assert parse_query("") == {}


def test_single_pair():
    assert parse_query("a=1") == {"a": "1"}


def test_distinct_keys():
    assert parse_query("a=1&b=2") == {"a": "1", "b": "2"}


def test_repeated_key_becomes_list():
    assert parse_query("a=1&b=2&a=3") == {"a": ["1", "3"], "b": "2"}


def test_three_repeats():
    assert parse_query("x=1&x=2&x=3") == {"x": ["1", "2", "3"]}
