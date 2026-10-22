from app import diff


def cut(s, e, word, reason="habit", idx=None):
    return {"start": s, "end": e, "word": word, "reason": reason, "words_idx": idx or []}


def test_added_word():
    old = {"cuts": [cut(1, 1.2, "어", "always", [[0, 1]])]}
    new = {"cuts": [cut(1, 1.2, "어", "always", [[0, 1]]), cut(3.1, 3.3, "그", "habit", [[0, 5]])]}
    assert diff.compare(old, new)["rows"] == [{"word": "그", "kind": "added", "start": 3.1, "end": 3.3}]


def test_restored_word():
    old = {"cuts": [cut(3.1, 3.3, "그", "habit", [[0, 5]])]}
    new = {"cuts": []}
    assert diff.compare(old, new)["rows"] == [{"word": "그", "kind": "restored", "start": 3.1, "end": 3.3}]


def test_merged_cut_counts_each_word():
    old = {"cuts": []}
    new = {"cuts": [cut(2, 2.8, "어떻게 보면", "filler", [[0, 7], [0, 8]])]}
    r = diff.compare(old, new)["rows"]
    assert [x["kind"] for x in r] == ["added", "added"]
    assert [x["word"] for x in r] == ["…", "…"]
    assert all(x["start"] == 2 and x["end"] == 2.8 for x in r)


def test_multi_word_with_transcript_rows():
    tr = {"segments": [{"words": [{"word": " 어떻게", "start": 2.0, "end": 2.4}, {"word": " 보면", "start": 2.4, "end": 2.8}]}]}
    new = {"cuts": [cut(1.95, 2.85, "어떻게 보면", "filler", [[0, 0], [0, 1]])]}
    r = diff.compare({"cuts": []}, new, tr)["rows"]
    assert r == [{"word": "어떻게", "kind": "added", "start": 2.0, "end": 2.4},
                 {"word": "보면", "kind": "added", "start": 2.4, "end": 2.8}]


def test_single_word_fallback_uses_cut_word():
    r = diff.compare({"cuts": []}, {"cuts": [cut(1, 2, "이제", "habit", [[0, 3]])]}, None)["rows"]
    assert r[0]["word"] == "이제"


def test_silence_summary_only():
    old = {"cuts": [cut(5, 6, "(무음)", "silence")]}
    new = {"cuts": []}
    r = diff.compare(old, new)
    assert r["rows"] == [] and r["silence"] == {"added": 0, "restored": 1}


def test_merged_reason_with_silence_counted():
    old = {"cuts": []}
    new = {"cuts": [cut(1, 3, "이제 + (무음)", "habit + silence", [[0, 2]])]}
    r = diff.compare(old, new)
    assert r["silence"] == {"added": 1, "restored": 0}
    assert [x["kind"] for x in r["rows"]] == ["added"]


def test_silence_boundary_shift_is_not_change():
    old = {"cuts": [cut(5, 6, "(무음)", "silence")]}
    new = {"cuts": [cut(5.05, 6.05, "(무음)", "silence")]}
    assert diff.compare(old, new)["silence"] == {"added": 0, "restored": 0}


def test_untranscribed_by_overlap():
    old = {"cuts": [cut(4, 4.3, "(전사 누락)", "filler(전사 누락)")]}
    new = {"cuts": [cut(4.05, 4.3, "(전사 누락)", "filler(전사 누락)"), cut(9, 9.2, "(전사 누락)", "filler(전사 누락)")]}
    assert diff.compare(old, new)["rows"] == [{"word": "(전사 누락)", "kind": "added", "start": 9, "end": 9.2}]


def test_boundary_shift_ignored():
    old = {"cuts": [cut(1, 1.2, "어", "always", [[0, 1]])]}
    new = {"cuts": [cut(1.05, 1.2, "어", "always", [[0, 1]])]}
    r = diff.compare(old, new)
    assert r["rows"] == [] and r["silence"] == {"added": 0, "restored": 0}


def test_rows_sorted_by_start():
    old = {"cuts": [cut(8, 8.2, "음", "always", [[0, 9]])]}
    new = {"cuts": [cut(3, 3.2, "그", "habit", [[0, 2]])]}
    assert [x["start"] for x in diff.compare(old, new)["rows"]] == [3, 8]


def test_cut_ranges_sorted():
    j = {"cuts": [cut(5, 6, "a"), cut(1, 2, "b")]}
    assert diff.cut_ranges(j) == [[1, 2], [5, 6]]
