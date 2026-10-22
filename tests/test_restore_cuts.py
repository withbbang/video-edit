"""잘린 곳 되살리기: 결과 화면에서 컷 하나를 골라 살리면 미리보기·요약에 바로 반영되고 저장 때 그 컷만 빼고 렌더한다."""
import json
from pathlib import Path

from app import diff, paths, runner
from app.api import pipeline_command
from app.jobqueue import RENDER
from app.store import Job, Status, Store
from tests.test_api import MANIFEST, add_job, make, write_out

CUTS = {"transcript_sha1": "abc", "cuts": [
    {"start": 1.0, "end": 1.5, "word": "(무음)", "reason": "silence"},
    {"start": 3.0, "end": 3.4, "word": "어,", "reason": "filler"},
    {"start": 6.0, "end": 7.0, "word": "(무음) + 좀", "reason": "silence + habit"},
]}


def setup(tmp_path, status=Status.REVIEW):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=status)
    job.summary = {"orig_sec": 10.0, "final_sec": 8.1, "cuts": 3, "removed_sec": 1.9}
    write_out(job, "a.cuts.json", CUTS)
    write_out(job, "a.transcript.json", {"duration": 10.0, "segments": []})
    return a, store, job, push


def test_cut_items_lists_every_cut_with_label():
    items = diff.cut_items(CUTS, [])
    assert [(i["start"], i["word"], i["kind"], i["restored"]) for i in items] == [
        (1.0, "", "silence", False), (3.0, "어,", "filler", False), (6.0, "좀", "habit", False)]


def test_kind_follows_real_word():
    c = {"start": 0, "end": 1, "word": "(전사 누락) + 좀", "reason": "filler(전사 누락) + habit"}
    assert [(i["kind"], i["word"]) for i in diff.cut_items({"cuts": [c]}, [])] == [("habit", "좀")]


def test_restored_matches_by_overlap():
    """다시 판정해 컷 경계가 조금 달라져도 같은 자리면 되살림이 유지된다 (컷 길이의 절반 이상 겹치면)"""
    assert diff.is_restored({"start": 3.05, "end": 3.45}, [[3.0, 3.4]])
    assert not diff.is_restored({"start": 3.3, "end": 4.0}, [[3.0, 3.4]])


def test_restore_updates_result(tmp_path):
    a, store, job, push = setup(tmp_path)
    r = a.set_restored(job.id, 3.0, 3.4, True)
    assert r is not None
    res = a.get_result(job.id)
    assert res["cuts"] == [[1.0, 1.5], [6.0, 7.0]]                 # 미리보기가 건너뛰는 구간
    assert [i["restored"] for i in res["cut_items"]] == [False, True, False]
    s = res["summary"]
    assert (s["cuts"], s["restored"]) == (2, 1)
    assert abs(s["removed_sec"] - 1.5) < 1e-9 and abs(s["final_sec"] - 8.5) < 1e-9
    assert Store.load(store.path).get(job.id).restored == [[3.0, 3.4]]
    assert push.of("job")                                             # 목록·화면이 다시 읽게


def test_cut_again(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_restored(job.id, 3.0, 3.4, True)
    a.set_restored(job.id, 3.02, 3.38, False)
    assert job.restored == []
    assert a.get_result(job.id)["summary"] == {**job.summary, "silence_cuts": 2, "silence_sec": 1.5}


def test_restore_refused_while_busy_or_unknown(tmp_path):
    a, store, job, _ = setup(tmp_path, status=Status.SAVING)
    assert a.set_restored(job.id, 3.0, 3.4, True) is None and job.restored == []
    assert a.set_restored("nope", 3.0, 3.4, True) is None


def test_render_uses_cuts_without_restored(tmp_path):
    a, store, job, _ = setup(tmp_path)
    a.set_restored(job.id, 6.0, 7.0, True)
    cmd, _, _ = pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data", tmp_path / "saved")
    used = Path(cmd[cmd.index("--apply-cuts") + 1])
    data = json.loads(used.read_text(encoding="utf-8"))
    assert [c["start"] for c in data["cuts"]] == [1.0, 3.0]
    assert data["transcript_sha1"] == "abc"                            # 파이프라인의 전사 확인은 그대로
    original = paths.job_dir(Path(job.src)) / "out" / "a.cuts.json"
    assert len(json.loads(original.read_text(encoding="utf-8"))["cuts"]) == 3   # 판정 결과는 그대로


def test_old_state_without_restored_loads():
    j = Job.from_dict({"id": "x", "src": "/a.mp4"})
    assert j.restored == []


def test_render_command_has_subs_edits(tmp_path):
    a, store, job, _ = setup(tmp_path)
    job.word_edits, job.line_times, job.edits_sha1 = {"0:1": "x"}, {"0:0": [0.0, 1.0]}, "abc"
    cmd, _, _ = pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data", tmp_path / "saved")
    p = Path(cmd[cmd.index("--subs-edits") + 1])
    assert json.loads(p.read_text(encoding="utf-8")) == {"transcript_sha1": "abc", "words": {"0:1": "x"},
                                                         "lines": {"0:0": [0.0, 1.0]}}
    job.word_edits, job.line_times = {}, {}
    cmd, _, _ = pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data", tmp_path / "saved")
    assert "--subs-edits" not in cmd and not p.exists()
