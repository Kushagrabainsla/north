from greeting import casual_greeting, formal_greeting


def test_formal():
    assert formal_greeting("Ada", "Lovelace") == "Dear Lovelace, Ada!"


def test_casual():
    assert casual_greeting("Ada", "Lovelace") == "Hey Lovelace, Ada!"
