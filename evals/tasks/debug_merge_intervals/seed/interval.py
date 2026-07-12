def merge_overlapping(intervals):
    """Merge overlapping or touching [start, end] intervals; output sorted by start.

    E.g. merge_overlapping([[1, 3], [2, 6], [8, 10]]) == [[1, 6], [8, 10]].
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start < last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return merged
