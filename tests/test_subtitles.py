"""자막 모델: 찾아 바꾸기 계획, 줄 시간 한계, 어절 고치기 API"""
import json

from app import subtitles
from app.store import Job, Status
from tests.test_api import add_job, make, write_out


def cap(*lines):
    """lines: [(줄 key, [(어절 key, 글자), ...]), ...]"""
    return [{"key": k, "start": 0.0, "end": 1.0,
             "words": [{"key": wk, "text": t, "orig": t} for wk, t in ws]} for k, ws in lines]


def test_replace_keeps_particle():
    assert subtitles.replace_plan(cap(("k", [("k", "김영산이")])), "김영산", "김영선") == {"k": "김영선이"}


def test_replace_across_words():
    caps = cap(("k1", [("k1", "에이피"), ("k2", "아이를")]))
    assert subtitles.replace_plan(caps, "에이피아이", "API") == {"k1": "API", "k2": "를"}


def test_replace_across_words_with_middle():
    caps = cap(("a", [("a", "하나"), ("b", "둘"), ("c", "셋이")]))
    assert subtitles.replace_plan(caps, "나둘셋", "X") == {"a": "하X", "b": "", "c": "이"}


def test_replace_not_across_lines():
    caps = cap(("a", [("a", "에이피")]), ("b", [("b", "아이를")]))
    assert subtitles.replace_plan(caps, "에이피아이", "API") == {}


def test_replace_case_sensitive():
    caps = cap(("a", [("a", "Ian")]))
    assert subtitles.replace_plan(caps, "ian", "X") == {}
    assert subtitles.replace_plan(caps, "Ian", "X") == {"a": "X"}


def test_replace_blank_noop():
    caps = cap(("a", [("a", "Ian")]))
    assert subtitles.replace_plan(caps, "", "X") == {}
    assert subtitles.replace_plan(caps, "  ", "X") == {}


def test_replace_twice_in_one_word():
    assert subtitles.replace_plan(cap(("a", [("a", "가나가")])), "가", "다") == {"a": "다나다"}


def test_line_bounds():
    caps = cap(("a", [("a", "x")]), ("b", [("b", "y")]), ("c", [("c", "z")]))
    caps[0]["end"], caps[2]["start"] = 2.0, 5.0
    assert subtitles.line_bounds(caps, "b") == (2.0, 5.0)
    assert subtitles.line_bounds(caps, "a")[0] == 0.0


TR = {"duration": 20.0, "segments": [{"id": 0, "words": [
    {"word": " 안녕하세요", "start": 0.0, "end": 0.5}, {"word": " 여러분", "start": 0.6, "end": 1.0},
    {"word": " 오늘은", "start": 2.0, "end": 2.5}, {"word": " 좋은", "start": 2.6, "end": 3.0},
    {"word": " 날", "start": 5.0, "end": 5.4}]}]}
CUTS = {"cuts": []}


def setup(tmp_path, status=Status.REVIEW):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=status)
    write_out(job, "a.cuts.json", CUTS)
    write_out(job, "a.transcript.json", TR)
    return a, store, job, push


def test_line_time_clamped(tmp_path):
    a, store, job, _ = setup(tmp_path)
    caps = a.get_captions(job.id)
    assert len(caps) >= 2
    first, second = caps[0], caps[1]
    r = a.set_line_time(job.id, first["key"], None, second["start"] + 5)
    got = next(c for c in r if c["key"] == first["key"])
    assert got["end"] == second["start"] and got["timed"] is True
    r = a.set_line_time(job.id, first["key"], first["start"], first["start"] + 0.1)
    got = next(c for c in r if c["key"] == first["key"])
    assert abs(got["end"] - got["start"] - 0.3) < 1e-9
    assert a.set_line_time(job.id, first["key"], float("nan"), 1.0) is None
    r = a.set_line_time(job.id, first["key"], None, None)
    assert next(c for c in r if c["key"] == first["key"])["timed"] is False
    assert first["key"] not in store.get(job.id).line_times


def test_set_word_marks_review(tmp_path):
    a, store, job, push = setup(tmp_path, Status.SAVED)
    r = a.set_word(job.id, "0:1", "여러분들")
    assert store.get(job.id).status == Status.REVIEW
    words = {w["key"]: w for c in r for w in c["words"]}
    assert words["0:1"]["edited"] is True and words["0:1"]["text"] == "여러분들"
    assert store.get(job.id).edits_sha1 == subtitles.transcript_sha1(TR)
    assert push.of("job")
    r = a.set_word(job.id, "0:1", "여러분")          # 원래 글자 = 원래대로
    assert store.get(job.id).word_edits == {}
    assert a.set_word(job.id, "9:9", "x") is None


def test_set_word_rejected_while_processing(tmp_path):
    a, store, job, _ = setup(tmp_path, Status.PROCESSING)
    assert a.set_word(job.id, "0:1", "x") is None


def test_replace_text_api(tmp_path):
    a, store, job, _ = setup(tmp_path)
    assert a.replace_text(job.id, "여러분", "분들", False) == {"count": 1, "keys": ["0:1"]}
    assert store.get(job.id).word_edits == {}
    assert a.replace_text(job.id, "  ", "x", True) == {"count": 0, "keys": []}
    assert a.replace_text(job.id, "여러분", "분들", True)["count"] == 1
    assert store.get(job.id).word_edits == {"0:1": "분들"}


def test_job_from_old_dict():
    j = Job.from_dict({"id": "a" * 12, "src": "/x/a.mp4", "status": "saved"})
    assert (j.word_edits, j.line_times, j.range_ops, j.edits_sha1) == ({}, {}, [], None)


def test_captions_match_render_path_with_frame_snapping():
    """앱의 자막 줄이 렌더(cuts_from_dicts + build_captions)와 같다 — 프레임 격자에 맞추면 경계가 달라지는 컷으로"""
    from fractions import Fraction
    import pipeline
    cfg = {"_always": set(), "_single_only": set()}
    cuts_json = {"fps": "15250/509", "cuts": [
        {"start": 0.62, "end": 0.97, "word": "여러분", "reason": "filler", "context": "", "seg": 0,
         "words_idx": [[0, 1]]},
        {"start": 3.01, "end": 4.93, "word": "", "reason": "silence", "context": "", "seg": 0},
        {"start": 4.94, "end": 4.941, "word": "", "reason": "silence", "context": "", "seg": 0}]}
    fps = Fraction(15250, 509)
    raw = [pipeline.Cut(**{k: v for k, v in c.items()}) for c in cuts_json["cuts"]]
    snapped = pipeline.cuts_from_dicts(cuts_json["cuts"], fps, int(round(20.0 * float(fps))))
    assert [(c.start, c.end) for c in snapped] != [(c.start, c.end) for c in raw]
    remap = pipeline.TimeRemap(snapped)
    expect = pipeline.build_captions(TR, remap.cut_words, remap, set(), set())
    got = subtitles.captions(cuts_json, TR, cfg, {}, {})
    assert [(c["key"], c["start"], c["end"], [w["key"] for w in c["words"]]) for c in got] == \
           [(c["key"], c["start"], c["end"], [w["key"] for w in c["words"]]) for c in expect]
