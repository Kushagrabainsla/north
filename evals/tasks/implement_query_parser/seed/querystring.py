def parse_query(qs):
    """Parse a URL query string into a dict.

    A key seen once maps to its string value; a key seen multiple times maps to a
    list of its values in order. Empty input returns {}.
    E.g. parse_query("a=1&b=2&a=3") == {"a": ["1", "3"], "b": "2"}.
    """
    raise NotImplementedError
