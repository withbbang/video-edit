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
        return (b_dur, b_video) if Path(p).read_bytes() in (b"bbbb", b"cccc") else None
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
    assert r["clips"] == [{"clip": b["clip"], "name": "Clip B", "start": 4.0, "end": 6.0, "dur": 2.0, "spans": [[4.0, 6.0]]}]
    assert r["clips"] == a.list_clips(job.id)
    assert r["history"] == {"undo": True, "redo": False}


def test_list_clips_spans(tmp_path, monkeypatch):
    """갈린 영상(B1 C B2)은 한 줄이고 spans 에 조각마다 [시작, 끝] (이어 붙인 초) — 타임라인 띠용."""
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    job.pieces = [{"id": "1", "clip": "main", "src": job.src, "name": "a", "in": 0.0, "out": 4.0},
                  {"id": "2", "clip": "b", "src": "b.mov", "name": "B", "in": 0.0, "out": 1.0},
                  {"id": "3", "clip": "c", "src": "c.mov", "name": "C", "in": 0.0, "out": 0.5},
                  {"id": "4", "clip": "b", "src": "b.mov", "name": "B", "in": 1.0, "out": 2.0},
                  {"id": "5", "clip": "main", "src": job.src, "name": "a", "in": 4.0, "out": 10.0}]
    rows = a.list_clips(job.id)
    assert [r["clip"] for r in rows] == ["b", "c"]
    assert rows[0]["spans"] == [[4.0, 5.0], [5.5, 6.5]] and rows[0]["dur"] == 2.0
    assert (rows[0]["start"], rows[0]["end"]) == (4.0, 6.5)
    assert rows[1]["spans"] == [[5.0, 5.5]]


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


def test_render_settles_stable_edits(tmp_path, monkeypatch):
    """다시 판정 뒤 화면이 자막을 읽기 전에 저장해도 렌더는 새 전사 키로 푼 자막 수정을 쓴다 (옛 키로 쓰지 않는다)."""
    import json
    from app.jobqueue import RENDER
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.set_word(job.id, "1:0", "나쁜")
    a.add_clip(job.id, 4.0)
    assert job.stable_edits
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    job.status = Status.SAVING
    pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data", lock=q.lock)
    tr = api_mod._read_json(paths.job_dir(Path(job.src)) / "out" / "a.transcript.json")
    data = json.loads((paths.job_dir(Path(job.src)) / "out" / "a.subs_edits.json").read_text(encoding="utf-8"))
    assert data["transcript_sha1"] == api_mod.subtitles.transcript_sha1(tr)
    assert data["words"] == {"2:0": "나쁜"} and job.stable_edits is None


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


def test_result_layout_follows_transcript_pieces(tmp_path, monkeypatch):
    """get_result 의 layout(전사가 만들어진 조각 자리) — 화면이 소리 줄·썸네일을 다시 읽을지 이것으로 정한다."""
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    assert a.get_result(job.id)["layout"] == []
    a.add_clip(job.id, 4.0)
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    ids = [p["id"] for p in job.pieces]
    assert a.get_result(job.id)["layout"] == [[ids[0], 0.0, 4.0], [ids[1], 4.0, 6.0], [ids[2], 6.0, 12.0]]


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


def test_same_file_twice_edits_independent(tmp_path, monkeypatch):
    """같은 파일을 두 번 넣고 두 사본의 어절을 따로 고친 뒤 다른 영상을 또 넣어도 두 수정이 따로 남는다."""
    a, store, q, job, dialogs = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)                            # A1 B A2
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    assert a.set_word(job.id, "1:0", "첫째") is not None   # 세그먼트 1 = 첫 B 사본
    a.add_clip(job.id, 1.0)                            # A1a B' A1b B A2 (같은 파일)
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    assert job.word_edits == {"3:0": "첫째"}           # 세그먼트: 0 A1a, 1 B', 2 A1b, 3 B, 4 A2
    assert a.set_word(job.id, "1:0", "둘째") is not None
    c = tmp_path / "c.mp4"
    c.write_bytes(b"cccc")
    dialogs.video = str(c)
    a.add_clip(job.id, 0.0)                            # 맨 앞에 C (전사 없음)
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch)
    caps = a.get_captions(job.id)["captions"]
    texts = [w["text"] for cap in caps for w in cap["words"]]
    assert texts.count("첫째") == 1 and texts.count("둘째") == 1 and "삽입" not in texts
    assert job.word_edits == {"1:0": "둘째", "3:0": "첫째"}


def test_failed_rejudge_can_undo_and_remove(tmp_path, monkeypatch):
    """넣은 뒤 다시 판정이 실패(FAILED)해도 실행 취소와 빼기는 된다."""
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.set_word(job.id, "1:0", "나쁜")
    a.add_clip(job.id, 4.0)
    job.status, job.error = Status.FAILED, "B 분석 실패"   # 대기열의 다시 판정이 실패한 상태 (전사는 그대로)
    assert a.add_clip(job.id, 1.0) is None              # 실패 상태에서 또 넣기는 안 된다
    q.calls.clear()
    assert a.undo(job.id) is not None
    assert job.pieces == [] and job.status == Status.REVIEW and ("rejudge", job.id) in q.calls
    assert job.word_edits == {"1:0": "나쁜"} and job.stable_edits is None

    a.redo(job.id)
    job.status = Status.FAILED
    clip = job.pieces[1]["clip"]
    q.calls.clear()
    r = a.remove_clip(job.id, clip)
    assert r is not None and r["clips"] == [] and ("rejudge", job.id) in q.calls
    assert job.status == Status.REVIEW and job.error == ""
    assert [(p["clip"], p["in"], p["out"]) for p in job.pieces] == [("main", 0.0, 10.0)]
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch)
    a.get_captions(job.id)
    assert job.word_edits == {"1:0": "나쁜"}


# ---- 영상 보관함 -------------------------------------------------------------

def _second_job(tmp_path, a, store, monkeypatch, name="z.mp4"):
    """같은 Api 에 다른 영상 작업 하나 더 (10초, 영상 있음)."""
    job = add_job(tmp_path, a, store, name=name, status=Status.REVIEW)
    job.summary = {"orig_sec": 10.0}
    stem = Path(name).stem
    write_out(job, f"{stem}.cuts.json", {"fps": "30/1", "cuts": []})
    write_out(job, f"{stem}.transcript.json", TR_A)
    Path(job.src + ".fake_tr.json").write_text(__import__("json").dumps(TR_A), encoding="utf-8")
    old = api_mod.probe_media
    monkeypatch.setattr(api_mod, "probe_media", lambda p: (10.0, True) if str(p) == job.src else old(p))
    return job


def test_preset_insert_brings_edits(tmp_path, monkeypatch):
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)                            # A1 B(4~6) A2
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    assert a.set_word(job.id, "1:0", "인사") is not None   # 세그먼트 1 = B
    assert a.add_range_op(job.id, "cut", 4.2, 4.5) is not None
    png = paths.job_dir(Path(job.src)) / "overlays" / "0123456789abcdef.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(api_mod.PNG_SIG + b"logo")
    job.overlays = [{"id": "o1", "img": png.name, "name": "로고", "start": 3.0, "end": 5.0, "x": 0, "y": 0, "w": 0.2}]
    clip = job.pieces[1]["clip"]
    items = a.save_clip_preset(job.id, clip, "  인트로 ")
    assert items is not None and len(items) == 1
    p = items[0]
    assert p["name"] == "인트로" and p["dur"] == 2.0 and p["file"].endswith(".mov")
    pdir = tmp_path / "data" / "clip_presets" / p["id"]
    assert (pdir / p["file"]).read_bytes() == b"bbbb"
    ed = p["edits"]
    assert ed["word_edits"] == {"0|0": "인사"}
    assert [(o["op"], o["start"], o["end"]) for o in ed["range_ops"]] == [("cut", pytest.approx(0.2), pytest.approx(0.5))]
    assert [(o["start"], o["end"]) for o in ed["overlays"]] == [(0.0, 1.0)]     # B 안쪽 부분만
    assert (pdir / ed["overlays"][0]["img"]).read_bytes() == api_mod.PNG_SIG + b"logo"
    assert a.list_clip_presets() == items

    # 같은 파일을 다시 보관하면 그 항목을 고친다
    a.add_range_op(job.id, "cut", 5.0, 5.5)
    again = a.save_clip_preset(job.id, clip, "인사말")
    assert len(again) == 1 and again[0]["id"] == p["id"] and again[0]["name"] == "인사말"
    assert len(again[0]["edits"]["range_ops"]) == 2

    # 다른 영상에 넣으면 편집이 따라온다 (이미지 파일도 그 작업 폴더로)
    job2 = _second_job(tmp_path, a, store, monkeypatch)
    r = a.insert_clip_preset(job2.id, p["id"], 6.0)
    assert r is not None and ("rejudge", job2.id) in q.calls
    assert r["clips"] == [{"clip": job2.pieces[1]["clip"], "name": "인사말", "start": 6.0, "end": 8.0, "dur": 2.0,
                          "spans": [[6.0, 8.0]]}]
    b = job2.pieces[1]
    assert Path(b["src"]).parent == paths.job_dir(Path(job2.src)) / "clips" and Path(b["src"]).read_bytes() == b"bbbb"
    assert [(o["op"], o["start"], o["end"]) for o in job2.range_ops] == [
        ("cut", pytest.approx(6.2), pytest.approx(6.5)), ("cut", pytest.approx(7.0), pytest.approx(7.5))]
    assert [(o["start"], o["end"]) for o in job2.overlays] == [(6.0, 7.0)]
    assert (paths.job_dir(Path(job2.src)) / "overlays" / job2.overlays[0]["img"]).is_file()
    run_fake_analyze(job2, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    caps = a.get_captions(job2.id)
    assert job2.word_edits == {"2:0": "인사"}          # 세그먼트: 0·1 A1, 2 B
    assert "인사" in {w["text"] for c in caps["captions"] for w in c["words"]}
    a.undo(job2.id)                                    # 넣기 한 번에 되돌린다
    assert job2.pieces == [] and job2.range_ops == [] and job2.overlays == []


def test_preset_insert_skips_transcribe(tmp_path, monkeypatch):
    """보관함의 분석 캐시는 넣는 작업의 복사본 서명으로 옮겨져 파이프라인이 전사를 다시 하지 않는다."""
    import json
    pl = api_mod.subtitles.pipeline_module()
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)
    bsrc = Path(job.pieces[1]["src"])
    fw = pl.file_work(paths.job_dir(Path(job.src)) / "work" / "a", pl.seq.file_key(bsrc))
    fw.mkdir(parents=True)
    akey = {"v": pl.CACHE_VERSION, "src": pl.source_sig(bsrc), "filter": "anull", "ac": []}
    (fw / "clean.wav").write_bytes(b"c")
    (fw / "asr.wav").write_bytes(b"a")
    (fw / "audio.meta.json").write_text(json.dumps(akey), encoding="utf-8")
    tkey = pl.transcribe_cache_key(akey, "m", "int8", "")
    (fw / "transcript.json").write_text(json.dumps({**TR_B, "cache_key": tkey}), encoding="utf-8")
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    p = a.save_clip_preset(job.id, job.pieces[1]["clip"], "인트로")[0]
    pfile = tmp_path / "data" / "clip_presets" / p["id"] / p["file"]
    pfw = pfile.parent / "files" / pl.seq.file_key(pfile)
    assert json.loads((pfw / "audio.meta.json").read_text(encoding="utf-8"))["src"] == pl.source_sig(pfile)

    job2 = _second_job(tmp_path, a, store, monkeypatch)
    assert a.insert_clip_preset(job2.id, p["id"], 4.0) is not None
    copy_ = Path(job2.pieces[1]["src"])
    fw2 = pl.file_work(paths.job_dir(Path(job2.src)) / "work" / "z", pl.seq.file_key(copy_))
    want = {**akey, "src": pl.source_sig(copy_)}
    assert pl.cached_meta(fw2 / "audio.meta.json", want, [fw2 / "clean.wav", fw2 / "asr.wav"], False)

    def no_model(*a_, **k):
        raise AssertionError("전사를 다시 했다")
    monkeypatch.setattr(pl, "load_whisper", no_model, raising=False)
    tr = pl.transcribe(fw2 / "asr.wav", fw2 / "transcript.json", want, "m", "cpu", "int8", "", False,
                       tmp_path / "probe.json")
    assert tr["segments"] == TR_B["segments"]


def test_preset_cache_keys_use_resolved_path(tmp_path, monkeypatch):
    """작업 폴더를 심볼릭 링크로 거쳐 가도(맥 링크 폴더·윈도우 subst) 앱의 파일별 캐시 위치·키는 파이프라인(load_sequence 가
    경로를 resolve)과 같다 — 보관할 때 캐시를 찾고, 넣을 때 옮긴 캐시를 파이프라인이 그대로 쓴다."""
    import json
    import os
    real = tmp_path / "real-data"
    real.mkdir()
    os.symlink(real, tmp_path / "link-data", target_is_directory=True)
    monkeypatch.setenv("YTEDIT_DATA_DIR", str(tmp_path / "link-data"))
    pl = api_mod.subtitles.pipeline_module()
    a, store, q, job, _ = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)
    bsrc = Path(job.pieces[1]["src"]).resolve()     # 파이프라인이 보는 경로
    assert bsrc != Path(job.pieces[1]["src"])
    fw = pl.file_work(paths.job_dir(Path(job.src)) / "work" / "a", pl.seq.file_key(bsrc))
    fw.mkdir(parents=True)
    akey = {"v": pl.CACHE_VERSION, "src": pl.source_sig(bsrc), "filter": "anull", "ac": []}
    (fw / "clean.wav").write_bytes(b"c")
    (fw / "asr.wav").write_bytes(b"a")
    (fw / "audio.meta.json").write_text(json.dumps(akey), encoding="utf-8")
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    p = a.save_clip_preset(job.id, job.pieces[1]["clip"], "인트로")[0]
    pfile = (tmp_path / "data" / "clip_presets" / p["id"] / p["file"]).resolve()
    assert (pfile.parent / "files" / pl.seq.file_key(pfile) / "audio.meta.json").is_file()   # 보관 때 캐시를 찾았다

    job2 = _second_job(tmp_path, a, store, monkeypatch)
    assert a.insert_clip_preset(job2.id, p["id"], 4.0) is not None
    copy_ = Path(job2.pieces[1]["src"]).resolve()
    fw2 = pl.file_work(paths.job_dir(Path(job2.src)) / "work" / "z", pl.seq.file_key(copy_))
    want = {**akey, "src": pl.source_sig(copy_)}
    assert pl.cached_meta(fw2 / "audio.meta.json", want, [fw2 / "clean.wav", fw2 / "asr.wav"], False)


def test_preset_rename_duplicate(tmp_path, monkeypatch):
    a, store, q, job, dialogs = setup(tmp_path, monkeypatch)
    a.add_clip(job.id, 4.0)
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch, b_tr=TR_B)
    a.get_captions(job.id)
    c = tmp_path / "c.mp4"
    c.write_bytes(b"cccc")
    dialogs.video = str(c)
    a.add_clip(job.id, 0.0)
    run_fake_analyze(job, store, a, q, tmp_path, monkeypatch)
    a.get_captions(job.id)
    cb, cc = job.pieces[2]["clip"], job.pieces[0]["clip"]
    a.save_clip_preset(job.id, cb, "인트로")
    items = a.save_clip_preset(job.id, cc, "아웃트로")
    assert [i["name"] for i in items] == ["인트로", "아웃트로"]
    pid = items[1]["id"]
    assert a.rename_clip_preset(pid, "인트로") is None              # 같은 이름은 안 된다
    assert a.rename_clip_preset(pid, "가" * 41) is None
    assert a.rename_clip_preset(pid, "  ") is None
    assert a.rename_clip_preset("nope", "새 이름") is None
    assert [i["name"] for i in a.rename_clip_preset(pid, "끝인사")] == ["인트로", "끝인사"]
    assert a.save_clip_preset(job.id, cb, "끝인사") is None          # 다른 항목과 같은 이름으로 보관도 안 된다
    assert a.save_clip_preset(job.id, "main", "본편") is None
    assert a.insert_clip_preset(job.id, "nope", 1.0) is None
    left = a.delete_clip_preset(pid)
    assert [i["name"] for i in left] == ["인트로"]
    assert not (tmp_path / "data" / "clip_presets" / pid).exists()
