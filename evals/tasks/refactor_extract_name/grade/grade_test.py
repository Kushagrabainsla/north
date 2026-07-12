from greeting import casual_greeting, formal_greeting, full_name


def test_full_name_helper():
    assert full_name("Ada", "Lovelace") == "Lovelace, Ada"


def test_formal_unchanged():
    assert formal_greeting("Ada", "Lovelace") == "Dear Lovelace, Ada!"


def test_casual_unchanged():
    assert casual_greeting("Grace", "Hopper") == "Hey Hopper, Grace!"
