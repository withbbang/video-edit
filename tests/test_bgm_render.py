"""--bgm: 곡 배율(dB 점), soft_limit, cut.wav 에 제자리로 섞기, 소리만 렌더 끝까지."""
import json
import shutil
import subprocess
import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

import pipeline as pl

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


def _ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, a)], check=True)


def test_db_interp_and_floor():
    pts = [[1.0, 0.0], [2.0, -20.0]]
    db = pl.bgm_db_at(pts, np.array([0.0, 1.0, 1.5, 2.0, 9.0]))
    assert db.tolist() == [0.0, 0.0, -10.0, -20.0, -20.0]
    g = pl.bgm_gain([[0.0, -40.0]], 0.0, np.array([0.5]))
    assert g.tolist() == [0.0]
    assert pl.bgm_gain([], 6.0, np.array([0.0]))[0] == pytest.approx(10 ** (6 / 20))


def test_soft_limit():
    x = np.array([1000.0, -29000.0, 40000.0, -90000.0])
    y = pl.soft_limit(x)
    assert y[0] == 1000.0 and y[1] == -29000.0
    assert 29490 < y[2] < 32767 and -32767 <= y[3] < -29490   # 아주 큰 값은 tanh 가 1 로 포화해 정확히 -32767


def _silent_wav(path, sec=2.0, sr=48000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(bytes(int(sec * sr) * 2))


def _samples(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


@pytest.fixture
def song(tmp_path):
    p = tmp_path / "song.m4a"
    _ff("-f", "lavfi", "-i", "sine=f=440:d=3", p)
    return p


@needs_ffmpeg
def test_mix_bgm_places_and_trims(tmp_path, song):
    cut = tmp_path / "cut.wav"
    _silent_wav(cut)
    b = {"path": str(song), "at": 1.5, "in": 0.5, "out": 3.0, "gain": 0.0, "points": [[0, 0]]}
    assert pl.mix_bgm(cut, [b]) == 1
    n, x = _samples(cut)
    assert n == 96000                                   # 넘는 1초는 버려 길이 그대로
    assert not x[:72000].any()                          # 1.5초 전은 그대로 무음
    assert np.sqrt(np.mean(x[72000:].astype(np.float64) ** 2)) > 500
    _silent_wav(cut)
    assert pl.mix_bgm(cut, [{**b, "points": [[0, -40]]}]) == 1
    assert not _samples(cut)[1].any()


def test_load_bgms_skips_missing(tmp_path):
    f = tmp_path / "a.m4a"
    f.write_bytes(b"x")
    good = {"path": str(f), "at": 1.0, "in": 0.0, "out": 2.0, "gain": -6.0, "points": [[0.0, 0.0]]}
    j = tmp_path / "b.json"
    j.write_text(json.dumps([good, {**good, "path": str(tmp_path / "없음.m4a")}, {"x": 1}]), encoding="utf-8")
    assert pl.load_bgms(j) == [good]
    assert pl.load_bgms(None) == []
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert pl.load_bgms(tmp_path / "bad.json") == []


class _FakeModel:
    def transcribe(self, path, **kw):
        seg = SimpleNamespace(start=0.5, end=1.5, text="안녕하세요",
                              words=[SimpleNamespace(word=" 안녕하세요", start=0.5, end=1.5, probability=0.9)])
        return iter([seg]), SimpleNamespace(language="ko", duration=4.0)


@needs_ffmpeg
def test_render_with_bgm_audio_only(tmp_path, monkeypatch, song):
    src = tmp_path / "a.m4a"
    _ff("-f", "lavfi", "-i", "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=4", "-c:a", "aac", src)
    monkeypatch.setattr(pl, "load_whisper", lambda *x, **k: _FakeModel())
    monkeypatch.setattr(pl, "ctc_fix_fillers", lambda *x, **k: (0, []))
    bgm = tmp_path / "bgm.json"

    def render(out, with_bgm):
        bgm.write_text(json.dumps([{"path": str(song), "at": 0.5, "in": 0.0, "out": 2.0, "gain": -6.0,
                                    "points": [[0, 0]]}]), encoding="utf-8")
        argv = ["pipeline.py", str(src), "--denoise", "none", "--no-loudnorm", "--silence", "off",
                "--filler", "off", "--habit", "off", "--out", str(tmp_path / out),
                "--work-root", str(tmp_path / "work" / out)]
        monkeypatch.setattr(sys, "argv", argv + (["--bgm", str(bgm)] if with_bgm else []))
        pl.main()
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(tmp_path / out / "a_clean.m4a")], capture_output=True, text=True,
                             check=True).stdout
        return float(dur), (tmp_path / out / "a.report.md").read_text(encoding="utf-8")

    d0, r0 = render("o0", False)
    d1, r1 = render("o1", True)
    assert abs(d0 - d1) <= 1 / 30 + 1e-3
    assert "BGM" not in r0 and "## BGM" in r1 and "song.m4a" in r1
