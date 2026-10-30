"""영상 끼워 넣기 API: 넣기·빼기·이름 바꾸기·목록, 자막 수정 키 옮기기, 실행 취소."""
import subprocess
import sys
from pathlib import Path

import pytest

from app import api as api_mod
from app import paths, runner
from app.api import pipeline_command
from app.jobqueue import ANALYZE
from app.store import Status
from tests.test_api import MANIFEST, FakeDialogs, add_job, make, write_out

FAKE = str(Path(__file__).parent / "fake_pipeline.py")

# A: 세그먼트 0 (0~3초), 세그먼트 1 (5~6초)
TR_A = {"duration": 10.0, "segments": [
    {"id": 0, "words": [{"word": " 안녕하세요", "start": 0.0, "end": 0.5}, {"word": " 여러분", "start": 0.6, "end": 1.0},
                        {"word": " 오늘은", "start": 2.0, "end": 2.5}]},
    {"id": 1, "words": [{"word": " 좋은", "start": 5.0, "end": 5.4}, {"word": " 날", "start": 5.5, "end": 6.0}]}]}
TR_B = {"duration": 2.0, "segments": [{"id": 0, "words": [{"word": " 삽입", "start": 0.2, "end": 0.8}]}]}


class VideoDialogs(FakeDialogs):
    def __init__(self, video=None):
        super().__init__()
        self.video = video

    def open_video(self):
        return self.video


def setup(tmp_path, monkeypatch, b_dur=2.0, b_video=True, cuts=None):
    b = tmp_path / "Clip B.MOV"
    b.write_bytes(b"bbbb")
    dialogs = VideoDialogs(str(b))
    a, store, q, push = make(tmp_path, dialogs)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 10.0}
    write_out(job, "a.cuts.json", {"fps": "30/1", "cuts": cuts or []})
    write_out(job, "a.transcript.json", TR_A)
    Path(job.src + ".fake_tr.json").write_text(__import__("json").dumps(TR_A), encoding="utf-8")
    probes = {job.src: (10.0, True)}

    def probe(path):
        p = str(path)
        if p in probes:
            return probes[p]
        return (b_dur, b_video) if Path(p).read_bytes() == b"bbbb" else None
    monkeypatch.setattr(api_mod, "probe_media", probe)
    return a, store, q, job, dialogs


def run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=None):
    """대기열의 다시 판정 대신: 가짜 파이프라인으로 이어 붙인 전사를 만든다."""
    if b_tr is not None:
        for p in job.pieces:
            if p["clip"] != "main":
                Path(p["src"] + ".fake_tr.json").write_text(__import__("json").dumps(b_tr), encoding="utf-8")
    monkeypatch.setattr(runner, "pipeline_cmd", lambda: [sys.executable, FAKE])
    cmd, _, _ = pipeline_command(job, ANALYZE, store, MANIFEST, tmp_path / "data", lock=q.lock)
    subprocess.run(cmd, check=True)


def test_add_clip_copies_and_splits(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch, b_dur=2.01)
    r = a.add_clip(job.id, 4.01)
    assert r is not None and ("rejudge", job.id) in q.calls
    ps = job.pieces
    assert [(p["clip"] == "main", p["in"], p["out"]) for p in ps] == [(True, 0.0, 4.0), (False, 0.0, 2.0), (True, 4.0, 10.0)]
    b = ps[1]
    assert b["name"] == "Clip B" and Path(b["src"]).parent == paths.job_dir(Path(job.src)) / "clips"
    assert Path(b["src"]).suffix == ".mov" and Path(b["src"]).read_bytes() == b"bbbb"
    assert r["clips"] == [{"clip": b["clip"], "name": "Clip B", "start": 4.0, "end": 6.0, "dur": 2.0}]
    assert r["clips"] == a.list_clips(job.id)
    assert r["history"] == {"undo": True, "redo": False}


def test_add_clip_rejects_audio_only_and_cancel(tmp_path, monkeypatch):
    a, store, q, job, dialogs = setup(tmp_path, monkeypatch, b_video=False)
    assert a.add_clip(job.id, 1.0) is None and job.pieces == []
    dialogs.video = None
    assert a.add_clip(job.id, 1.0) is None
    assert not [c for c in q.calls if c[0] == "rejudge"]


def test_add_clip_inside_cut_snaps_to_cut_start(tmp_path, monkeypatch):
    cut = {"start": 2.6, "end": 3.4, "word": "(무음)", "reason": "silence", "context": "", "seg": -1, "words_idx": []}
    a, store, q, job, _ = setup(tmp_path, monkeypatch, cuts=[cut])
    a.add_clip(job.id, 3.0)
    assert [(p["in"], p["out"]) for p in job.pieces][0] == (0.0, pytest.approx(2.6))
    # 되살린 컷 안이면 당기지 않는다
    (tmp_path / "x").mkdir()
    a2, store2, q2, job2, _ = setup(tmp_path / "x", monkeypatch, cuts=[cut])
    job2.restored = [[2.6, 3.4]]
    a2.add_clip(job2.id, 3.0)
    assert job2.pieces[0]["out"] == pytest.approx(3.0)


def test_add_clip_shifts_edits(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    job.range_ops = [{"op": "restore", "start": 3.0, "end": 5.0}]   # 남기기는 잘린 구간이 아니라 at 을 당기지 않는다
    job.overlays = [{"id": "o", "img": "x.png", "start": 6.0, "end": 7.0, "x": 0, "y": 0, "w": 0.2}]
    a.add_clip(job.id, 4.0)
    assert job.range_ops == [{"op": "restore", "start": 3.0, "end": 4.0}, {"op": "restore", "start": 6.0, "end": 7.0}]
    assert (job.overlays[0]["start"], job.overlays[0]["end"]) == (8.0, 9.0)


def test_insert_keeps_word_edits(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.set_word(job.id, "1:0", "나쁜")
    a.set_line_time(job.id, "1:0", 4.8, 6.2)
    assert job.edits_sha1
    a.add_clip(job.id, 4.0)
    assert job.stable_edits and job.stable_edits["word_edits"]
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    caps = a.get_captions(job.id)
    assert caps["edits_dropped"] is False
    texts = {w["text"] for c in caps["captions"] for w in c["words"]}
    assert "나쁜" in texts and "좋은" not in texts and "삽입" in texts
    # 새 전사: A1 의 세그먼트 0, B 세그먼트 1, A2 의 세그먼트 2
    assert job.word_edits == {"2:0": "나쁜"}
    assert job.line_times == {"2:0": [6.8, 8.2]}
    assert job.stable_edits is None and job.edits_sha1 == api_mod.subtitles.transcript_sha1(
        api_mod._read_json(paths.job_dir(Path(job.src)) / "out" / "a.transcript.json"))
    assert a._hist(job.id)["undo"] is True       # 역사는 남는다

    # 빼기도 키를 옮긴다
    clip = a.list_clips(job.id)[0]["clip"]
    r = a.remove_clip(job.id, clip)
    assert r == {"clips": [], "history": {"undo": True, "redo": False}}
    assert [(p["clip"], p["in"], p["out"]) for p in job.pieces] == [("main", 0.0, 10.0)]
    assert job.line_times == {"2:0": [4.8, pytest.approx(6.2)]}       # 시간은 바로 당겨지고 키는 다시 판정 뒤 옮긴다
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch)
    a.get_captions(job.id)
    assert job.word_edits == {"1:0": "나쁜"} and job.line_times == {"1:0": [4.8, pytest.approx(6.2)]}


def test_remove_main_refused(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)
    assert a.remove_clip(job.id, "main") is None
    assert a.remove_clip(job.id, "nope") is None
    assert a.rename_clip(job.id, "main", "x") is None
    assert len(job.pieces) == 3


def test_rename_clip(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)
    clip = job.pieces[1]["clip"]
    q.calls.clear()
    r = a.rename_clip(job.id, clip, "  인트로 ")
    assert r["clips"][0]["name"] == "인트로" and job.pieces[1]["name"] == "인트로"
    assert a.rename_clip(job.id, clip, "   ") is None
    a.undo(job.id)                                     # 이름만 바뀐 실행 취소는 다시 판정하지 않는다
    assert job.pieces[1]["name"] == "Clip B" and not q.calls


def test_undo_insert_rejudges(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)
    q.calls.clear()
    assert a.undo(job.id) is not None
    assert job.pieces == [] and ("rejudge", job.id) in q.calls
    q.calls.clear()
    a.redo(job.id)
    assert len(job.pieces) == 3 and ("rejudge", job.id) in q.calls
    q.calls.clear()
    a.add_range_op(job.id, "cut", 1.0, 1.5)
    a.undo(job.id)                                     # 조각이 그대로면 다시 판정하지 않는다
    assert not q.calls


def test_edit_blocked_while_processing(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.set_word(job.id, "1:0", "나쁜")
    a.add_clip(job.id, 4.0)
    job.status = Status.PROCESSING                    # 대기열이 다시 판정을 시작한 상태
    clip = job.pieces[1]["clip"]
    assert a.add_clip(job.id, 1.0) is None
    assert a.remove_clip(job.id, clip) is None
    assert a.set_word(job.id, "0:0", "x") is None
    assert a.add_range_op(job.id, "cut", 1.0, 2.0) is None
    assert a.undo(job.id) is None
    # 처리 중엔 옛 전사 기준 수정을 버리지도 옮기지도 않는다
    a.get_captions(job.id)
    assert job.stable_edits is not None
    # 다시 판정이 끝나기 전(stable_edits 가 남아 있음)엔 또 넣거나 뺄 수 없다
    job.status = Status.REVIEW
    assert a.add_clip(job.id, 1.0) is None
    assert a.remove_clip(job.id, clip) is None


def test_stale_transcript_not_dropped_when_pieces_differ(tmp_path, monkeypatch):
    """실행 취소 직후 (다시 판정 전): 디스크의 전사는 아직 이어 붙인 것이라 지문이 달라도 수정을 버리지 않는다."""
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.set_word(job.id, "1:0", "나쁜")
    a.add_clip(job.id, 4.0)
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    a.undo(job.id)
    assert job.pieces == [] and job.word_edits == {"1:0": "나쁜"}
    assert a.get_captions(job.id)["edits_dropped"] is False
    assert job.word_edits == {"1:0": "나쁜"}


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg 없음")
def test_probe_media_and_store_clip(tmp_path):
    v, m = tmp_path / "v.mp4", tmp_path / "s.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=32x32:r=30:d=0.5",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.5", "-shortest", str(v)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.5", str(m)], check=True)
    dur, has_video = api_mod.probe_media(v)
    assert has_video and dur == pytest.approx(0.5, abs=0.05)
    assert api_mod.probe_media(m)[1] is False
    assert api_mod.probe_media(tmp_path / "none.mp4") is None
    dst = api_mod.store_clip(v, tmp_path / "clips")
    assert dst.read_bytes() == v.read_bytes() and len(dst.stem) == 16
    mtime = dst.stat().st_mtime_ns
    assert api_mod.store_clip(v, tmp_path / "clips") == dst and dst.stat().st_mtime_ns == mtime
