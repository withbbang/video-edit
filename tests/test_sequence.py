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


# ---- 이어 붙인 오디오 ----
import subprocess
import shutil
import wave
from pathlib import Path

import numpy as np

import pipeline


def write_wav(path, samples, rate=48000, channels=1):
    a = np.asarray(samples, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(a.tobytes())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getnchannels()


def sine(n, rate=48000):
    return (8000 * np.sin(2 * np.pi * 440 * np.arange(n) / rate)).astype(np.int16)


def test_concat_wav_lengths_and_silence(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    write_wav(a, sine(24000)); write_wav(b, sine(24000))
    out = tmp_path / "o.wav"
    n = seq.concat_wav([(a, 0, 24000), (None, 0, 12000), (b, 0, 24000)], out, 48000, 1)
    assert n == 60000
    s, ch = read_wav(out)
    assert ch == 1 and len(s) == 60000
    assert s[:24000].any() and not s[24000:36000].any() and s[36000:].any()
    assert not out.with_name(out.name + ".part").exists()


def test_concat_wav_pads_short_source_with_zeros(tmp_path):
    a = tmp_path / "a.wav"
    write_wav(a, sine(1000))
    out = tmp_path / "o.wav"
    assert seq.concat_wav([(a, 500, 1500)], out, 48000, 1) == 1000
    s, _ = read_wav(out)
    assert s[:500].any() and not s[500:].any()


def test_concat_wav_mono_into_stereo(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    write_wav(a, sine(1000))
    st = np.stack([np.full(1000, 100), np.full(1000, 300)], axis=1).reshape(-1)
    write_wav(b, st, channels=2)
    out = tmp_path / "o.wav"
    assert seq.concat_wav([(a, 0, 1000), (b, 0, 1000)], out, 48000, 2) == 2000
    s, ch = read_wav(out)
    assert ch == 2
    s = s.reshape(-1, 2)
    assert (s[:1000, 0] == s[:1000, 1]).all() and (s[:1000, 0] == sine(1000)).all()
    assert (s[1000:, 0] == 100).all() and (s[1000:, 1] == 300).all()  # 스테레오 그대로


def test_concat_wav_stereo_into_mono_averages(tmp_path):
    b = tmp_path / "b.wav"
    write_wav(b, np.stack([np.full(10, 100), np.full(10, 300)], axis=1).reshape(-1), channels=2)
    out = tmp_path / "o.wav"
    seq.concat_wav([(b, 0, 10)], out, 48000, 1)
    s, ch = read_wav(out)
    assert ch == 1 and (s == 200).all()


def sp(clip, fkey, f_in, f_out, start_f):
    return seq.Span(piece=P(clip, clip, 0, 0, fkey), f_in=f_in, f_out=f_out, start_f=start_f)


def fa(clean, asr, ch=1):
    return pipeline.FileAnalysis({"channels": ch}, clean, asr, {"k": 1}, {}, [], 0.01, {}, {}, None)


def test_sequence_audio_boundaries_and_cache(tmp_path):
    fps = Fraction(30000, 1001)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    write_wav(a, sine(48000 * 10)); write_wav(b, sine(48000 * 10) // 2)
    an = {"A": fa(a, a), "B": fa(b, b)}
    # A 0~30 프레임, B 5~20, A 30~45
    sps = [sp("A", "A", 0, 30, 0), sp("B", "B", 5, 20, 30), sp("A", "A", 30, 45, 45)]
    work = tmp_path / "work"; work.mkdir()
    wav, meta = pipeline.sequence_audio(sps, an, work, fps, "clean")
    assert wav == work / "clean.wav"
    s, ch = read_wav(wav)
    end = round(Fraction(60) * 48000 / fps)
    assert ch == 1 and len(s) == end
    b0 = round(Fraction(30) * 48000 / fps)
    src_b = round(Fraction(5) * 48000 / fps)
    exp_b = (sine(48000 * 10) // 2)[src_b:src_b + round(Fraction(45) * 48000 / fps) - b0]
    assert (s[b0:b0 + len(exp_b)] == exp_b).all()
    m = json.loads((work / "audio.meta.json").read_text())
    assert m["sequence"] == [["A", 0, 30], ["B", 5, 20], ["A", 30, 45]]
    st = wav.stat().st_mtime_ns
    wav2, _ = pipeline.sequence_audio(sps, an, work, fps, "clean")
    assert wav2.stat().st_mtime_ns == st
    awav, _ = pipeline.sequence_audio(sps, {"A": fa(a, a), "B": fa(b, b)}, work, fps, "asr")
    assert awav == work / "asr.wav"
    assert json.loads((work / "audio.meta.json").read_text()) == {**m, "built": ["asr", "clean"]}


def test_sequence_audio_key_change_rebuilds_both_kinds(tmp_path):
    fps = Fraction(30)
    a = tmp_path / "a.wav"
    write_wav(a, sine(48000 * 4)); 
    an = {"A": fa(a, a)}
    work = tmp_path / "work"; work.mkdir()
    k1 = [sp("A", "A", 0, 30, 0)]
    k2 = [sp("A", "A", 0, 60, 0)]
    for w in ("clean", "asr"):
        pipeline.sequence_audio(k1, an, work, fps, w)
    assert len(read_wav(work / "asr.wav")[0]) == 16000
    for w in ("clean", "asr"):
        pipeline.sequence_audio(k2, an, work, fps, w)
    assert len(read_wav(work / "asr.wav")[0]) == 32000
    assert len(read_wav(work / "clean.wav")[0]) == 96000


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


@needs_ffmpeg
def test_analyze_file_silent_video(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=30:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    args = pipeline.build_parser().parse_args([str(src)])
    cfg = pipeline.load_fillers(args.fillers)
    work = tmp_path / "w"; work.mkdir()
    r = pipeline.analyze_file(src, work, work / "t.json", args, cfg, False)
    assert r.clean_wav is None and r.asr_wav is None and r.rms == []
    assert r.audio_key["silent"] is True and r.audio_key["src"] == pipeline.source_sig(src)
    assert r.tr["segments"] == [] and abs(r.tr["duration"] - 2.0) < 0.2
    assert r.info["has_audio"] is False
