from lru import LRUCache


def test_constructs_with_capacity():
    cache = LRUCache(2)
    assert cache.capacity == 2
