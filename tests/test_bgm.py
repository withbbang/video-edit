import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402
from app import bgm  # noqa: E402


def test_keeps_and_to_out():
    k = bgm.keeps_from_cuts([[1.0, 2.0], [1.5, 3.0]], 10.0, 30)
    assert k == [[0.0, 1.0], [3.0, 10.0]]
    assert bgm.to_out(2.0, k) == 1.0 and bgm.to_out(4.0, k) == 2.0 and bgm.out_total(k) == 8.0
    assert bgm.to_out(11.0, k) == 8.0
    assert bgm.keeps_from_cuts([[0, 10]], 10.0, 30) == []


def test_speech_spans_follow_cuts_and_join():
    k = [[0.0, 1.0], [3.0, 10.0]]
    words = [{"start": 0.5, "end": 0.9}, {"start": 3.2, "end": 3.5}, {"start": 5.0, "end": 5.5}]
    assert bgm.speech_spans(words, k) == [[0.5, 1.5], [3.0, 3.5]]


def test_auto_points_duck_and_fade():
    pts = bgm.auto_points(10.0, 2.0, 22.0, [[14.0, 16.0]])
    d = dict((round(t, 3), db) for t, db in pts)
    assert d[2.0] == -40.0 and d[2.5] == 0.0
    assert d[5.7] == 0.0 and d[6.0] == -15.0 and d[8.0] == -15.0 and d[8.3] == 0.0
    assert d[22.0] == -40.0
    assert bgm.auto_points(0.0, 0.0, 0.6, []) == [[0.0, -40.0], [0.3, 0.0], [0.6, -40.0]]


def test_auto_points_min_of_curves_and_clip():
    # 말이 맨 앞에서 시작: 페이드 인(-40→0)과 말(-15) 중 작은 값
    pts = bgm.auto_points(0.0, 0.0, 10.0, [[0.0, 1.0]])
    assert pts[0] == [0.0, -40.0]
    assert dict((round(t, 3), db) for t, db in pts)[0.5] == -15.0


def test_resolve_keeps_manual():
    b = {"at": 0, "in": 0, "out": 5, "points": [[1, -3]]}
    assert bgm.resolve_points(b, [[0, 5]]) == [[1, -3]]
    assert bgm.resolve_points({"at": 0, "in": 0, "out": 5, "points": None}, [])[0] == [0.0, -40.0]


def test_db_at_matches_pipeline():
    rnd = random.Random(1)
    pts = [[rnd.uniform(0, 10), rnd.uniform(-40, 6)] for _ in range(3)]
    ts = [rnd.uniform(-1, 11) for _ in range(20)]
    want = pipeline.bgm_db_at(pts, np.array(ts))
    for t, w in zip(ts, want):
        assert abs(bgm.db_at(pts, t) - w) < 1e-9
    assert bgm.db_at([], 3.0) == 0.0
