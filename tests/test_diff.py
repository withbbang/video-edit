from app import diff


def cut(s, e, word, reason="habit", idx=None):
    return {"start": s, "end": e, "word": word, "reason": reason, "words_idx": idx or []}


def test_cut_ranges_sorted():
    j = {"cuts": [cut(5, 6, "a"), cut(1, 2, "b")]}
    assert diff.cut_ranges(j) == [[1, 2], [5, 6]]
