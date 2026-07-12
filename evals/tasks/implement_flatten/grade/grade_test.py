from flatten import flatten


def test_already_flat():
    assert flatten([1, 2, 3]) == [1, 2, 3]


def test_nested():
    assert flatten([1, [2, [3, 4]], 5]) == [1, 2, 3, 4, 5]


def test_empty():
    assert flatten([]) == []


def test_deeply_nested():
    assert flatten([[[[1]]], 2]) == [1, 2]


def test_strings_kept_whole():
    assert flatten(["a", ["b", "c"]]) == ["a", "b", "c"]
