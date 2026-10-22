"""편집 되돌리기·다시 실행: 잘린 곳 되살리기, 구간 작업, 자막 고치기를 한 단계씩."""
from app import api as api_mod
from app.store import Status
from tests.test_api import add_job, make, write_out
from tests.test_subtitles import TR


def setup(tmp_path, status=Status.REVIEW):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=status)
    job.summary = {"orig_sec": 20.0}
    cut = {"start": 1.0, "end": 1.5, "word": "(무음)", "reason": "silence", "context": "", "seg": -1, "words_idx": []}
    write_out(job, "a.cuts.json", {"cuts": [cut]})
    write_out(job, "a.transcript.json", TR)
    return a, store, job, push


def test_undo_redo_across_kinds(tmp_path):
    a, store, job, _ = setup(tmp_path)
    assert a.get_result(job.id)["history"] == {"undo": False, "redo": False}
    a.set_restored(job.id, 1.0, 1.5, True)
    a.add_range_op(job.id, "cut", 6.0, 7.0)
    r = a.set_word(job.id, "0:1", "여러분들")
    assert r["history"] == {"undo": True, "redo": False}

    assert a.undo(job.id) == {"history": {"undo": True, "redo": True}}
    assert job.word_edits == {} and job.edits_sha1 is None
    a.undo(job.id)
    assert job.range_ops == [] and job.restored == [[1.0, 1.5]]
    a.undo(job.id)
    assert job.restored == []
    assert a.undo(job.id) is None                       # 더 되돌릴 게 없다
    assert a.get_result(job.id)["history"] == {"undo": False, "redo": True}

    a.redo(job.id)
    a.redo(job.id)
    assert job.restored == [[1.0, 1.5]] and job.range_ops == [{"op": "cut", "start": 6.0, "end": 7.0}]
    a.redo(job.id)
    assert job.word_edits == {"0:1": "여러분들"}
    assert a.redo(job.id) is None


def test_new_edit_clears_redo(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.add_range_op(job.id, "cut", 6.0, 7.0)
    a.undo(job.id)
    a.add_range_op(job.id, "cut", 8.0, 9.0)
    assert a.redo(job.id) is None
    assert job.range_ops == [{"op": "cut", "start": 8.0, "end": 9.0}]


def test_no_change_is_not_recorded(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_word(job.id, "0:1", "여러분")              # 원래 글자 그대로
    assert a.set_restored(job.id, 1.0, 1.5, False) is not None   # 이미 잘린 곳을 다시 자름
    assert a.add_range_op(job.id, "cut", 19.5, 20.5) is None     # 영상 밖이라 거절
    assert a.undo(job.id) is None


def test_undo_saved_goes_review_and_persists(tmp_path):
    a, store, job, push = setup(tmp_path)
    a.add_range_op(job.id, "cut", 6.0, 7.0)
    job.status = Status.SAVED
    a.undo(job.id)
    assert job.status == Status.REVIEW and push.of("job")
    assert type(store).load(store.path).get(job.id).range_ops == []


def test_undo_refused_while_busy(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.add_range_op(job.id, "cut", 6.0, 7.0)
    job.status = Status.SAVING
    assert a.undo(job.id) is None and job.range_ops != []


def test_history_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "HISTORY_MAX", 3)
    a, store, job, _ = setup(tmp_path)
    for i in range(5):
        a.add_range_op(job.id, "cut", 2.0 + i, 2.5 + i)
    n = 0
    while a.undo(job.id):
        n += 1
    assert n == 3 and len(job.range_ops) == 2


def test_stale_transcript_clears_history(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_word(job.id, "0:1", "여러분들")
    job.edits_sha1 = "old"                             # 받아쓰기가 바뀐 것처럼
    assert a.get_captions(job.id)["edits_dropped"] is True
    assert a.undo(job.id) is None


def test_remove_job_clears_history(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.add_range_op(job.id, "cut", 6.0, 7.0)
    a.remove_job(job.id)
    assert job.id not in a._history
