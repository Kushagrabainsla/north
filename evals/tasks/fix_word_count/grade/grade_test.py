from wordcount import count_occurrences


def test_case_insensitive():
    assert count_occurrences("The cat sat on the CAT mat", "cat") == 2


def test_not_substring():
    assert count_occurrences("a cat and a category", "cat") == 1


def test_absent_word():
    assert count_occurrences("dogs everywhere", "cat") == 0


def test_query_case_insensitive():
    assert count_occurrences("Cats CATS cats", "CATS") == 3
