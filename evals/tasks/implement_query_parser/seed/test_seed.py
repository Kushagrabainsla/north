import querystring


def test_has_function():
    assert hasattr(querystring, "parse_query")
