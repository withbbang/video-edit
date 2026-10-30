import json
from fractions import Fraction

import pytest

import sequence as seq


def P(id, clip, a, b, fkey=None):
    return seq.Piece(id=id, clip=clip, src=None, fkey=fkey or clip, t_in=a, t_out=b)


def W(word, a, b):
    return {"word": word, "start": a, "end": b, "prob": 0.9}


def make():
    trA = {"language": "ko", "duration": 5.0, "segments": [
        {"id": 0, "start": 0.9, "end": 3.2, "text": "가 나",
         "words": [W("가", 0.9, 1.1), W("나", 3.0, 3.2)]}]}
    trB = {"language": "ko", "duration": 1.0, "segments": [
        {"id": 0, "start": 0.1, "end": 0.5, "text": "다", "words": [W("다", 0.1, 0.5)]}]}
    ps = [P("a", "main", 0.0, 2.0, "A"), P("b", "c1", 0.0, 1.0, "B"), P("a2", "main", 2.0, 5.0, "A")]
    return ps, seq.spans(ps, Fraction(30)), {"A": trA, "B": trB}


def test_spans_frame_grid():
    ps = [P("a", "main", 0.0, 2.0), P("b", "c1", 0.0, 1.01), P("a2", "main", 2.0, 5.0), P("z", "c1", 1.0, 1.0)]
    sp = seq.spans(ps, Fraction(30))
    assert [(s.f_in, s.f_out, s.start_f) for s in sp] == [(0, 60, 0), (0, 30, 60), (60, 150, 90)]


def test_combine_splits_segment_at_boundary_and_keeps_provenance():
    ps, sp, trs = make()
    tr = seq.combine_transcripts(sp, trs, Fraction(30))
    assert [s["id"] for s in tr["segments"]] == [0, 1, 2]
    assert tr["segments"][2]["words"][0]["start"] == pytest.approx(4.0)
    assert tr["segments"][2]["src"] == {"fkey": "A", "sid": 0, "w0": 1}
    assert tr["segments"][0]["src"] == {"fkey": "A", "sid": 0, "w0": 0}
    assert tr["pieces"][1] == {"id": "b", "clip": "c1", "start": 2.0, "end": 3.0, "seg_from": 1, "seg_to": 2}
    assert tr["duration"] == pytest.approx(6.0)
    assert tr["language"] == "ko"
    assert tr["segments"][2]["text"] == "나"


def test_slice_piece_local_times_keep_ids():
    ps, sp, trs = make()
    tr = seq.combine_transcripts(sp, trs, Fraction(30))
    sub = seq.slice_piece(tr, tr["pieces"][2])
    assert [s["id"] for s in sub["segments"]] == [2]
    assert sub["segments"][0]["words"][0]["start"] == pytest.approx(1.0)
    assert sub["duration"] == pytest.approx(3.0)
    assert tr["segments"][2]["words"][0]["start"] == pytest.approx(4.0)  # 원본은 그대로


def test_load_sequence(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    j = tmp_path / "seq.json"
    j.write_text(json.dumps([{"id": "a", "clip": "main", "src": str(f), "in": 0, "out": 2.5}]))
    (p,) = seq.load_sequence(j)
    assert p.src == f and p.t_out == 2.5 and len(p.fkey) == 16
