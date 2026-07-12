from wordcount import count_occurrences


def test_counts_single_match():
    assert count_occurrences("cat", "cat") == 1
