"""구간 직접 자르기·되살리기: 원본 시간 구간을 잘라 내거나 되살린다 (나중 작업이 이김)."""
import json
from pathlib import Path

from app import diff, paths
from app.api import save_cuts
from app.store import Status
from tests.test_api import add_job, make, write_out

TR = {"duration": 10.0, "segments": [{"id": 0, "words": [
    {"word": "하나", "start": 1.0, "end": 1.4}, {"word": "둘", "start": 1.6, "end": 2.0},
    {"word": "셋", "start": 2.2, "end": 2.6}, {"word": "넷", "start": 5.0, "end": 5.4}]}]}


def cuts(*items):
    return {"transcript_sha1": "x", "cuts": list(items)}


def pairs(res):
    return sorted((round(c["start"], 3), round(c["end"], 3)) for c in res["cuts"])


def test_manual_cut_adds_cut_and_words():
    res = diff.effective_cuts(cuts(), [], [{"op": "cut", "start": 1.5, "end": 2.5}], TR)
    (c,) = res["cuts"]
    assert (c["start"], c["end"], c["word"], c["reason"]) == (1.5, 2.5, "(직접 자름)", "manual")
    assert sorted(map(tuple, c["words_idx"])) == [(0, 1), (0, 2)]


def test_manual_cut_words_by_midpoint():
    # 둘(1.8) 안, 셋(2.4) 안, 하나(1.2) 밖
    res = diff.effective_cuts(cuts(), [], [{"op": "cut", "start": 1.5, "end": 2.5}], TR)
    assert sorted(map(tuple, res["cuts"][0]["words_idx"])) == [(0, 1), (0, 2)]
    res = diff.effective_cuts(cuts(), [], [{"op": "cut", "start": 1.5, "end": 2.3}], TR)
    assert sorted(map(tuple, res["cuts"][0]["words_idx"])) == [(0, 1)]


def test_restore_range_splits_auto_cut():
    c = {"start": 1.0, "end": 3.0, "word": "(무음)", "reason": "silence"}
    res = diff.effective_cuts(cuts(c), [], [{"op": "restore", "start": 1.5, "end": 2.0}], TR)
    assert pairs(res) == [(1.0, 1.5), (2.0, 3.0)]


def test_restore_range_returns_word_to_subs():
    c = {"start": 1.0, "end": 2.7, "word": "하나 + 둘 + 셋", "reason": "filler", "words_idx": [[0, 0], [0, 1], [0, 2]]}
    res = diff.effective_cuts(cuts(c), [], [{"op": "restore", "start": 1.5, "end": 2.1}], TR)
    assert pairs(res) == [(1.0, 1.5), (2.1, 2.7)]
    got = [sorted(map(tuple, x["words_idx"])) for x in sorted(res["cuts"], key=lambda x: x["start"])]
    assert got == [[(0, 0)], [(0, 2)]]


def test_tiny_pieces_dropped():
    c = {"start": 1.0, "end": 2.0, "word": "x", "reason": "silence"}
    res = diff.effective_cuts(cuts(c), [], [{"op": "restore", "start": 1.03, "end": 2.0}], TR)
    assert res["cuts"] == []


def test_later_op_wins():
    a = diff.effective_cuts(cuts(), [], [{"op": "cut", "start": 2, "end": 4}, {"op": "restore", "start": 3, "end": 4}], TR)
    assert pairs(a) == [(2, 3)]
    b = diff.effective_cuts(cuts(), [], [{"op": "restore", "start": 3, "end": 4}, {"op": "cut", "start": 2, "end": 4}], TR)
    assert pairs(b) == [(2, 4)]


def test_list_restored_still_applies():
    c = {"start": 1.0, "end": 2.0, "word": "x", "reason": "silence"}
    assert diff.effective_cuts(cuts(c), [[1.0, 2.0]], [], TR)["cuts"] == []


def test_cut_items_manual_and_split():
    c = {"start": 1.0, "end": 3.0, "word": "(무음)", "reason": "silence"}
    ops = [{"op": "restore", "start": 1.5, "end": 2.0}, {"op": "cut", "start": 6, "end": 7}]
    items = diff.cut_items(cuts(c), [], ops, TR)
    assert [(i["start"], i["end"], i["kind"], i["restored"]) for i in items] == [
        (1.0, 1.5, "silence", False), (2.0, 3.0, "silence", False), (6, 7, "manual", False)]
    m = items[-1]
    assert m["word"] == "" and m["op"] == 1


def setup(tmp_path, ops=None):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 10.0, "final_sec": 9.0, "cuts": 1, "removed_sec": 1.0}
    write_out(job, "a.cuts.json", cuts({"start": 1.0, "end": 2.0, "word": "(무음)", "reason": "silence"}))
    write_out(job, "a.transcript.json", TR)
    job.range_ops = ops or []
    return a, store, job, push


def test_list_restore_removes_manual_op(tmp_path):
    a, store, job, push = setup(tmp_path)
    assert a.add_range_op(job.id, "cut", 6.0, 7.0) is not None
    assert job.range_ops == [{"op": "cut", "start": 6.0, "end": 7.0}]
    res = a.get_result(job.id)
    assert res["cuts"] == [[1.0, 2.0], [6.0, 7.0]]
    assert res["summary"]["cuts"] == 2 and abs(res["summary"]["removed_sec"] - 2.0) < 1e-9
    assert a.remove_range_op(job.id, 0) is not None
    assert a.get_result(job.id)["cuts"] == [[1.0, 2.0]]
    assert job.range_ops == [] and push.of("job")
    assert a.remove_range_op(job.id, 5) is None


def test_range_op_rejects_bad_range(tmp_path):
    a, store, job, _ = setup(tmp_path)
    for s, e in [(3, 3), (4, 3), (-1, 2), (9.5, 10.5), (1, 1.04)]:
        assert a.add_range_op(job.id, "cut", s, e) is None
    assert a.add_range_op(job.id, "nope", 1, 2) is None
    assert a.add_range_op("nope", "cut", 1, 2) is None
    assert job.range_ops == []


def test_range_op_refused_while_busy(tmp_path):
    a, store, job, _ = setup(tmp_path)
    job.status = Status.SAVING
    assert a.add_range_op(job.id, "cut", 1, 2) is None


def test_saved_goes_back_to_review(tmp_path):
    a, store, job, _ = setup(tmp_path)
    job.status = Status.SAVED
    a.add_range_op(job.id, "cut", 6, 7)
    assert job.status == Status.REVIEW


def test_save_cuts_includes_manual(tmp_path):
    a, store, job, _ = setup(tmp_path, [{"op": "cut", "start": 6.0, "end": 7.0}])
    p = save_cuts(job)
    assert p is not None
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    assert any(c.get("reason") == "manual" for c in data["cuts"])


def test_summary_unions_overlapping_cuts(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.add_range_op(job.id, "cut", 1.5, 2.5)
    s = a.get_result(job.id)["summary"]
    assert s["cuts"] == 1 and abs(s["removed_sec"] - 1.5) < 1e-9 and abs(s["final_sec"] - 8.5) < 1e-9


def test_range_op_rejects_non_finite(tmp_path):
    a, store, job, _ = setup(tmp_path)
    for s, e in [(float("nan"), 2), (1, float("inf")), (float("-inf"), 2), (1, float("nan"))]:
        assert a.add_range_op(job.id, "cut", s, e) is None
    assert job.range_ops == []
