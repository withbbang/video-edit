import pytest

from app.progress import ANALYZE_WEIGHTS, RENDER_WEIGHTS, Tracker


def test_weighted():
    t = Tracker(ANALYZE_WEIGHTS)
    assert t.feed('@progress {"stage": "audio", "frac": 1.0}') == pytest.approx(.10)
    assert t.feed('@progress {"stage": "transcribe", "frac": 0.5}') == pytest.approx(.40)
    assert t.feed("[12:00:00] 로그") is None


def test_skipped_stage_counts_done():
    t = Tracker(ANALYZE_WEIGHTS)
    assert t.feed('@progress {"stage": "preview", "frac": 0.0}') == pytest.approx(.80)


def test_never_decreases():
    t = Tracker(ANALYZE_WEIGHTS)
    t.feed('@progress {"stage": "transcribe", "frac": 0.5}')
    assert t.feed('@progress {"stage": "transcribe", "frac": 0.4}') == pytest.approx(.40)


def test_unknown_stage_and_bad_json_ignored():
    t = Tracker(RENDER_WEIGHTS)
    t.feed('@progress {"stage": "render", "frac": 0.5}')
    assert t.feed('@progress {"stage": "nope", "frac": 1}') == pytest.approx(.55)
    assert t.feed("@progress {oops") is None


def test_frac_clamped_and_complete():
    t = Tracker(RENDER_WEIGHTS)
    assert t.feed('@progress {"stage": "render", "frac": 2}') == pytest.approx(1.0)
