"""무음 기준 자동 맞춤: 말 없는 틈의 배경 소리 크기를 재서 무음 기준(-40dB)을 영상마다 올린다.
야외 브이로그처럼 말 사이에도 차·바람 소리가 깔린 영상은 -40dB 아래로 내려가지 않아 무음이 거의 안 잡힌다."""
import pipeline

HOP = 0.01
CFG = {"silence_threshold_db": -40, "silence_bridge_max_db": -30,
       "silence_auto": True, "silence_auto_margin_db": 5, "silence_auto_max_db": -30}


def amp(db: float) -> float:
    return 10 ** (db / 20)


def make(floor_db: float, word_db: float = -24, words: int = 10, word_sec: float = 0.5, gap_sec: float = 0.7):
    """말(word_db) 과 틈(floor_db) 이 번갈아 나오는 영상"""
    ws, rms, t = [], [], 0.0
    for i in range(words):
        rms += [amp(floor_db)] * int(gap_sec / HOP)
        t += gap_sec
        ws.append({"word": f" 말{i}", "start": round(t, 2), "end": round(t + word_sec, 2)})
        rms += [amp(word_db)] * int(word_sec / HOP)
        t += word_sec
    rms += [amp(floor_db)] * int(gap_sec / HOP)
    return {"segments": [{"id": 0, "words": ws}]}, rms


def test_quiet_room_keeps_base_threshold():
    tr, rms = make(floor_db=-55)
    o = pipeline.auto_silence(tr, rms, HOP, CFG)
    assert o["silence_threshold_db"] == -40 and o["silence_bridge_max_db"] == -30


def test_noisy_background_raises_threshold_to_cap():
    tr, rms = make(floor_db=-35)
    o = pipeline.auto_silence(tr, rms, HOP, CFG)
    assert o["silence_threshold_db"] == -30          # 바닥 -35 + 5, 상한 -30
    assert o["silence_bridge_max_db"] == -20         # 무음 기준보다 10dB 위 (실내 -40/-30 과 같은 간격)
    assert o["silence_noise_floor_db"] == -35


def test_moderate_background_floor_plus_margin():
    tr, rms = make(floor_db=-42)
    assert pipeline.auto_silence(tr, rms, HOP, CFG)["silence_threshold_db"] == -37


def test_threshold_stays_below_speech():
    """말소리 자체가 작으면 기준이 말을 덮지 않게 말소리 중간값보다 6dB 아래로 묶는다"""
    tr, rms = make(floor_db=-33, word_db=-33.5)
    tr2, rms2 = make(floor_db=-36, word_db=-38)
    assert pipeline.auto_silence(tr, rms, HOP, CFG)["silence_threshold_db"] == -39.5
    assert pipeline.auto_silence(tr2, rms2, HOP, CFG)["silence_threshold_db"] == -40   # 기본보다 내려가진 않는다


def test_too_little_gap_keeps_base():
    tr, rms = make(floor_db=-30, gap_sec=0.15)   # 단어 앞뒤 0.1초를 빼면 틈이 거의 없다
    assert pipeline.auto_silence(tr, rms, HOP, CFG)["silence_threshold_db"] == -40


def test_disabled():
    tr, rms = make(floor_db=-35)
    o = pipeline.auto_silence(tr, rms, HOP, {**CFG, "silence_auto": False})
    assert o["silence_threshold_db"] == -40 and o["silence_bridge_max_db"] == -30


def test_fillers_json_enables_auto():
    import json
    from pathlib import Path
    cfg = json.loads((Path(__file__).resolve().parent.parent / "fillers.json").read_text(encoding="utf-8"))
    assert cfg["silence_auto"] is True and cfg["silence_auto_margin_db"] == 5 and cfg["silence_auto_max_db"] == -30
