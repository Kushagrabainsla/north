class LRUCache:
    """A fixed-capacity least-recently-used cache.

    get(key) returns the stored value or None, and marks key most-recently-used.
    put(key, value) inserts or updates; when the number of entries exceeds
    capacity, the least-recently-used entry is evicted.
    """

    def __init__(self, capacity):
        self.capacity = capacity

    def get(self, key):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError
