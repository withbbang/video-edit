"""말 없는 구간 세기 --silence-level 0~100: 0 = 끔, 50 = 지금의 --silence on, 100 = 단어 없는 틈 전부.
값이 커지면 잘리는 곳이 줄지 않고, 세기를 바꿔도 필러 시간 보정(정렬) 키는 그대로다 (자막 수정이 살아남게)."""
import json
from dataclasses import asdict
from fractions import Fraction

import pipeline
from test_silence_auto import HOP, amp, make

FPS = Fraction(30)


def base_cfg():
    return pipeline.load_fillers(pipeline.DEFAULT_FILLERS)


def judged(tr, rms, cfg, level):
    """level 의 무음 컷만으로 refine_cuts 까지 (cuts.json 에 들어갈 모양)"""
    duration = len(rms) * HOP
    suspects = pipeline.find_suspect_words(tr, pipeline.quiet_mask(rms, HOP, cfg), HOP, cfg)
    cuts, _, _ = pipeline.silence_cuts_for_level(tr, rms, HOP, duration, cfg, level, suspects)
    return pipeline.refine_cuts(cuts, rms, HOP, FPS, int(round(duration * float(FPS))), cfg)


def removed(cuts):
    return sum(c.end - c.start for c in cuts)


def mixed():
    """틈마다 길이·배경 크기가 다른 영상 (조용한 틈, 시끄러운 틈, 짧은 틈, 긴 틈)"""
    gaps = [(0.4, -45), (0.7, -28), (1.2, -35), (0.5, -27), (2.0, -50), (0.35, -29), (0.9, -26.5), (0.6, -60)]
    ws, rms, t = [], [], 0.0
    for i, (g, db) in enumerate(gaps):
        rms += [amp(db)] * int(round(g / HOP))
        t += g
        ws.append({"word": f" 말{i}", "start": round(t, 2), "end": round(t + 0.5, 2)})
        rms += [amp(-24)] * 50
        t += 0.5
    rms += [amp(-40)] * 80
    return {"segments": [{"id": 0, "words": ws}]}, rms


def test_level_cfg_values():
    cfg = base_cfg()
    at50 = {**cfg, **pipeline.silence_level_cfg(cfg, 50)}   # 50 은 지금 값 그대로
    assert (at50["silence_min_sec"], at50["silence_auto_max_db"], at50.get("silence_auto_speech_margin_db", 6)) \
        == (0.3, -30, 6)
    assert pipeline.silence_level_cfg(cfg, 50) == {"silence_min_sec": 0.3}   # 0~50 은 최소 길이만
    assert pipeline.silence_level_cfg(cfg, 51)["silence_auto_max_db"] == -29.52
    assert pipeline.silence_level_cfg(cfg, 25)["silence_min_sec"] == 0.65
    assert pipeline.silence_level_cfg(cfg, 1)["silence_min_sec"] == 0.986
    o = pipeline.silence_level_cfg(cfg, 75)
    assert o == {"silence_min_sec": 0.3, "silence_auto_max_db": -18, "silence_auto_speech_margin_db": 4}
    assert pipeline.silence_level_cfg(cfg, 100)["silence_auto_max_db"] == -6


def test_level_50_equals_default():
    for floor in (-55, -42, -35, -28):
        tr, rms = make(floor_db=floor)
        cfg = base_cfg()
        cfg.update(pipeline.auto_silence(tr, rms, HOP, cfg))   # 지금의 --silence on 판정
        suspects = pipeline.find_suspect_words(tr, pipeline.quiet_mask(rms, HOP, cfg), HOP, cfg)
        want, want_sk = pipeline.detect_silences(tr, rms, HOP, len(rms) * HOP, cfg, suspects)
        got, got_sk, _ = pipeline.silence_cuts_for_level(tr, rms, HOP, len(rms) * HOP, base_cfg(), 50, suspects)
        assert [asdict(c) for c in got] == [asdict(c) for c in want]
        assert got_sk == want_sk


def test_level_0_cuts_nothing():
    tr, rms = make(floor_db=-55)
    assert judged(tr, rms, base_cfg(), 0) == []


def test_level_monotonic():
    for tr, rms in (make(floor_db=-28, word_db=-24), mixed()):
        totals = [removed(judged(tr, rms, base_cfg(), lv)) for lv in range(0, 101, 10)]
        assert all(b >= a - 1e-9 for a, b in zip(totals, totals[1:])), totals
        assert totals[0] == 0 and totals[-1] > totals[5]


def test_level_monotonic_quiet_room_low_levels():
    """조용한 실내: 0~50 은 최소 길이만 줄어든다 → 0.4초 틈은 낮은 값에선 안 잘리고 50 에선 잘림"""
    tr, rms = mixed()
    t10, t50 = removed(judged(tr, rms, base_cfg(), 10)), removed(judged(tr, rms, base_cfg(), 50))
    assert 0 < t10 < t50


def test_level_100_cuts_noisy_gap():
    tr, rms = make(floor_db=-26)   # 배경이 말소리(-24)와 붙어 있어 소리 크기로는 못 가른다
    assert removed(judged(tr, rms, base_cfg(), 75)) == 0
    cuts = judged(tr, rms, base_cfg(), 100)
    gaps = [c for c in cuts if c.reason == "gap"]
    assert gaps and all(c.word == "(무음)" and c.words_idx == [] for c in gaps)
    # 맨 앞 틈은 여유 없이, 단어 사이 틈(1.2~1.9초)은 앞뒤 0.08초 남기고
    assert abs(cuts[0].start) < 1e-9 and abs(cuts[0].end - 0.6333) < 0.02
    assert any(abs(c.start - 1.2667) < 0.02 and abs(c.end - 1.8333) < 0.02 for c in gaps)


def test_word_gaps_ignore_suspect_words():
    tr = {"segments": [{"id": 0, "words": [
        {"word": " 가", "start": 0.0, "end": 0.5}, {"word": " 숨", "start": 0.6, "end": 1.6},
        {"word": " 나", "start": 1.7, "end": 2.2}]}]}
    cfg = {"silence_min_sec": 0.3, "silence_pad_sec": 0.08}
    assert pipeline.detect_word_gaps(tr, 2.2, cfg, {}) == []
    cuts = pipeline.detect_word_gaps(tr, 2.2, cfg, {(0, 1): 0.5})
    assert [(round(c.start, 2), round(c.end, 2), c.reason) for c in cuts] == [(0.58, 1.62, "gap")]


def test_cli_without_level_unchanged():
    a = pipeline.build_parser().parse_args(["x.mp4"])
    assert a.silence_level is None and a.silence == "on"
    assert pipeline.build_parser().parse_args(["x.mp4", "--silence-level", "75"]).silence_level == 75


# --- process() 전체: 정렬 키·cuts.json 모드 ---------------------------------------------------------

def run_process(tmp_path, monkeypatch, name, argv, tr):
    tr_path_holder = {}
    info = {"fps": FPS, "duration": 0.0, "width": 1920, "height": 1080, "sample_rate": 48000, "channels": 1,
            "pix_fmt": "yuv420p", "vfr": False, "has_video": True}
    trs, rms = tr
    info["duration"] = len(rms) * HOP
    calls = []
    monkeypatch.setattr(pipeline, "ffprobe", lambda src: dict(info))
    monkeypatch.setattr(pipeline, "prepare_audio", lambda *a, **k: (tmp_path / "c.wav", tmp_path / "a.wav", {}))
    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: trs)
    monkeypatch.setattr(pipeline, "load_rms", lambda p: (rms, HOP))

    def fake_ctc(t, *a, **k):
        calls.append(1)
        return 0, []
    monkeypatch.setattr(pipeline, "ctc_fix_fillers", fake_ctc)
    out = tmp_path / name
    out.mkdir()
    args = pipeline.build_parser().parse_args(["v.mp4", "--review", "--out", str(out), *argv])
    pipeline.process(args, tmp_path / "v.mp4", "v", out, tmp_path / "work", 0.0)
    tr_path_holder["cuts"] = json.loads((out / "v.cuts.json").read_text(encoding="utf-8"))
    return trs.get("align"), len(calls), tr_path_holder["cuts"]


def test_align_key_independent_of_level(tmp_path, monkeypatch):
    tr = make(floor_db=-28)
    key0, n0, default = run_process(tmp_path, monkeypatch, "default", [], tr)
    assert n0 == 1 and default["silence"] == "on"
    for lv in (0, 50, 100):
        key, n, data = run_process(tmp_path, monkeypatch, f"lv{lv}", ["--silence-level", str(lv)], tr)
        assert key == key0, lv
        assert n == 0, lv    # 정렬을 다시 하지 않는다 → transcript.json·transcript_sha1 그대로
        assert data["silence"] == f"level:{lv}"
        assert data["transcript_sha1"] == default["transcript_sha1"]
    _, _, d50 = run_process(tmp_path, monkeypatch, "again50", ["--silence-level", "50"], tr)
    assert d50["cuts"] == default["cuts"] and d50["skipped"] == default["skipped"]


def test_level_0_overrides_silence_on(tmp_path, monkeypatch):
    tr = make(floor_db=-55)
    _, _, off = run_process(tmp_path, monkeypatch, "off", ["--silence", "off"], tr)
    _, _, l0 = run_process(tmp_path, monkeypatch, "l0", ["--silence", "on", "--silence-level", "0"], tr)
    _, _, l50 = run_process(tmp_path, monkeypatch, "l50", ["--silence", "off", "--silence-level", "50"], tr)
    assert l0["cuts"] == off["cuts"] == []
    assert l50["cuts"]   # --silence-level 이 --silence 를 이긴다


def test_diff_shows_gap_as_silence():
    from app import diff
    gap = {"start": 1.0, "end": 2.0, "word": "(무음)", "reason": "gap", "words_idx": []}
    both = {"start": 3.0, "end": 4.0, "word": "(무음) + (무음)", "reason": "silence + gap", "words_idx": []}
    with_word = {"start": 5.0, "end": 6.0, "word": "(무음) + 음", "reason": "gap + filler", "words_idx": [[0, 1]]}
    items = diff.cut_items({"cuts": [gap, both, with_word]}, [])
    assert [i["kind"] for i in items] == ["silence", "silence", "filler"]
    r = diff.compare({"cuts": []}, {"cuts": [gap]})
    assert r["silence"] == {"added": 1, "restored": 0} and r["rows"] == []
