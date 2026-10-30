import json

import pipeline


def _single_cache(work, out_dir, stem, meta=None):
    work.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (work / "clean.wav").write_bytes(b"clean")
    (work / "asr.wav").write_bytes(b"asr")
    (work / "audio.meta.json").write_text(json.dumps(meta or {"k": 1}), encoding="utf-8")
    (out_dir / f"{stem}.transcript.json").write_text('{"segments": []}', encoding="utf-8")


def test_file_work():
    assert pipeline.file_work(pipeline.Path("/w"), "abc") == pipeline.Path("/w/files/abc")


def test_adopt_copies_single_cache(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    _single_cache(work, out, "a")
    fw = work / "files" / "fa"
    pipeline.adopt_single_cache(work, out, "a", fw)
    assert (fw / "clean.wav").read_bytes() == b"clean"
    assert (fw / "asr.wav").read_bytes() == b"asr"
    assert json.loads((fw / "audio.meta.json").read_text()) == {"k": 1}
    assert (fw / "transcript.json").read_text() == '{"segments": []}'
    assert (work / "clean.wav").exists()  # 복사지 이동이 아니다


def test_adopt_leaves_existing_alone(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    _single_cache(work, out, "a")
    fw = work / "files" / "fa"
    fw.mkdir(parents=True)
    (fw / "audio.meta.json").write_text('{"mine": 1}', encoding="utf-8")
    pipeline.adopt_single_cache(work, out, "a", fw)
    assert json.loads((fw / "audio.meta.json").read_text()) == {"mine": 1}
    assert not (fw / "clean.wav").exists()


def test_adopt_skips_sequence_cache(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    _single_cache(work, out, "a", meta={"k": 1, "sequence": True})
    fw = work / "files" / "fa"
    pipeline.adopt_single_cache(work, out, "a", fw)
    assert not (fw / "audio.meta.json").exists()


def test_adopt_without_cache_or_missing_files(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    fw = work / "files" / "fa"
    pipeline.adopt_single_cache(work, out, "a", fw)  # 캐시 없음: 아무 일 없음
    _single_cache(work, out, "a")
    (work / "asr.wav").unlink()
    pipeline.adopt_single_cache(work, out, "a", fw)  # 없는 파일은 건너뜀
    assert (fw / "clean.wav").exists() and not (fw / "asr.wav").exists()


# --- --sequence 분석: 조각별 컷 판정 (합성 영상, 전사는 가짜 모델) ---------------------------------------

import shutil
import subprocess
import sys
import wave
from types import SimpleNamespace

import pytest

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")

FPS = 30
# A (5초): 0.5~2.6 · 3.4~4.5 에 소리. "음" 은 필러, 2.6~3.4 는 말 사이 무음, 앞뒤 무음
A_WORDS = [("안녕", 0.5, 1.0), ("음", 1.0, 1.3), ("하세요", 1.3, 2.0), ("여러분", 2.0, 2.6),
           ("반갑습니다", 3.4, 4.5)]
A_TONE = r"between(t\,0.5\,2.6)+between(t\,3.4\,4.5)"
# B (2초): 앞뒤 0.5초 무음
B_WORDS = [("비디오", 0.5, 1.5)]
B_TONE = r"between(t\,0.5\,1.5)"


def _media(path, dur, tone):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=size=160x120:rate={FPS}:duration={dur}",
                    "-f", "lavfi", "-i", f"aevalsrc=0.3*sin(2*PI*440*t)*({tone}):s=48000:d={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-shortest", str(path)],
                   check=True)


class _FakeModel:
    """wav 길이로 A·B 를 가려 미리 정한 단어를 돌려준다. 불린 길이를 calls 에 남긴다"""
    def __init__(self, calls):
        self.calls = calls

    def transcribe(self, path, **kw):
        with wave.open(str(path), "rb") as w:
            dur = w.getnframes() / w.getframerate()
        self.calls.append(round(dur, 1))
        words = A_WORDS if dur > 3.5 else B_WORDS
        seg = SimpleNamespace(start=words[0][1], end=words[-1][2], text=" ".join(x[0] for x in words),
                              words=[SimpleNamespace(word=" " + x, start=s, end=e, probability=0.9)
                                     for x, s, e in words])
        return iter([seg]), SimpleNamespace(language="ko", duration=dur)


@pytest.fixture
def seq_env(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mov", tmp_path / "b.mov"
    _media(a, 5, A_TONE)
    _media(b, 2, B_TONE)
    calls = []
    model = _FakeModel(calls)
    monkeypatch.setattr(pipeline, "load_whisper", lambda *x, **k: model)
    monkeypatch.setattr(pipeline, "ctc_fix_fillers", lambda *x, **k: (0, []))
    sq = tmp_path / "seq.json"
    sq.write_text(json.dumps([
        {"id": "a1", "clip": "A", "src": str(a), "in": 0, "out": 2},
        {"id": "b1", "clip": "B", "src": str(b), "in": 0, "out": 2},
        {"id": "a2", "clip": "A", "src": str(a), "in": 2, "out": 5}]), encoding="utf-8")

    def run(inp, out, *extra):
        monkeypatch.setattr(sys, "argv", ["pipeline.py", str(inp), "--review", "--denoise", "none",
                                          "--no-loudnorm", "--out", str(out),
                                          "--work-root", str(tmp_path / "work"), *map(str, extra)])
        pipeline.main()
        return json.loads((out / f"{pipeline.Path(inp).stem}.cuts.json").read_text(encoding="utf-8"))
    return SimpleNamespace(a=a, b=b, sq=sq, calls=calls, run=run, tmp=tmp_path, mp=monkeypatch)


def _frames(cuts):
    return [(c["start_frame"], c["end_frame"]) for c in cuts]


@needs_ffmpeg
def test_inserting_b_keeps_a_cuts(seq_env):
    out = seq_env.tmp / "out"
    single = seq_env.run(seq_env.a, out)
    combined = seq_env.run(seq_env.a, out, "--sequence", seq_env.sq)
    assert single["cuts"]
    b0, b1 = 2 * FPS, 4 * FPS   # B 는 이어 붙인 시간 2~4초
    a_part = [(s if s < b0 else s - (b1 - b0), e if e <= b0 else e - (b1 - b0))
              for s, e in _frames(combined["cuts"]) if e <= b0 or s >= b1]
    assert a_part == _frames(single["cuts"])
    assert [p["id"] for p in combined["pieces"]] == ["a1", "b1", "a2"]
    tr = json.loads((out / "a.transcript.json").read_text(encoding="utf-8"))
    assert "sequence_key" in tr and "cache_key" not in tr
    assert combined["transcript_sha1"] == pipeline.transcript_sha1(tr)


@needs_ffmpeg
def test_b_edges_silence_fully_cut(seq_env):
    out = seq_env.tmp / "out"
    data = seq_env.run(seq_env.a, out, "--sequence", seq_env.sq)
    b_cuts = [(s, e) for s, e in _frames(data["cuts"]) if 2 * FPS <= s and e <= 4 * FPS]
    assert b_cuts[0][0] == 2 * FPS and b_cuts[0][1] <= 2.5 * FPS     # 앞 무음: 경계 쪽은 여유 없이
    assert b_cuts[-1][1] == 4 * FPS and b_cuts[-1][0] >= 3.5 * FPS   # 뒤 무음: 경계 쪽은 여유 없이
    assert all(c["reason"] == "silence" for c in data["cuts"]
               if 2 * FPS <= c["start_frame"] and c["end_frame"] <= 4 * FPS)


@needs_ffmpeg
def test_sequence_does_not_retranscribe_a(seq_env):
    out = seq_env.tmp / "out"
    seq_env.run(seq_env.a, out)
    assert seq_env.calls == [5.0]
    seq_env.run(seq_env.a, out, "--sequence", seq_env.sq)
    assert seq_env.calls == [5.0, 2.0]   # B 만 새로 전사
    seq_env.run(seq_env.a, out, "--sequence", seq_env.sq)
    assert seq_env.calls == [5.0, 2.0]   # 다시 돌려도 전사 없음


@needs_ffmpeg
def test_sequence_input_must_be_first_piece(seq_env, capsys):
    with pytest.raises(SystemExit) as e:
        seq_env.run(seq_env.b, seq_env.tmp / "out", "--sequence", seq_env.sq)
    assert "첫 조각" in str(e.value.code)
    assert seq_env.calls == []


@needs_ffmpeg
def test_sequence_preview_cut_audio_from_cache(seq_env):
    out = seq_env.tmp / "out"
    data = seq_env.run(seq_env.a, out, "--sequence", seq_env.sq)
    tr_before = (out / "a.transcript.json").read_text(encoding="utf-8")
    m4a = seq_env.tmp / "cut.m4a"
    seq_env.mp.setattr(sys, "argv", ["pipeline.py", str(seq_env.a), "--denoise", "none", "--no-loudnorm", "--out", str(out),
                "--work-root", str(seq_env.tmp / "work"), "--sequence", str(seq_env.sq), "--denoise-level", "0",
                "--apply-cuts", str(out / "a.cuts.json"), "--preview-cut-audio", str(m4a)])
    pipeline.main()
    assert seq_env.calls == [5.0, 2.0]   # 전사 캐시만
    assert (out / "a.transcript.json").read_text(encoding="utf-8") == tr_before
    work = seq_env.tmp / "work" / "a"
    assert json.loads((work / "render.meta.json").read_text())["sequence"][1][1:] == [0, 2 * FPS]
    with wave.open(str(work / "render.wav"), "rb") as w:
        assert w.getnframes() == 7 * 48000   # A 5초 + B 2초
    kept = 7 * FPS - sum(e - s for s, e in _frames(data["cuts"]))
    idx = json.loads(m4a.with_suffix(".index.json").read_text())
    assert m4a.exists() and abs(idx["duration"] - kept / FPS) < 0.05


@needs_ffmpeg
def test_silent_b_is_not_judged(seq_env):
    b = seq_env.tmp / "silent.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=160x120:rate={FPS}:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(b)], check=True)
    items = json.loads(seq_env.sq.read_text())
    items[1]["src"] = str(b)
    seq_env.sq.write_text(json.dumps(items), encoding="utf-8")
    data = seq_env.run(seq_env.a, seq_env.tmp / "out", "--sequence", seq_env.sq)
    assert seq_env.calls == [5.0]   # 소리 없는 B 는 전사하지 않음
    assert not [c for c in data["cuts"] if c["start_frame"] < 4 * FPS and c["end_frame"] > 2 * FPS]


@needs_ffmpeg
def test_filler_edge_does_not_cross_piece_boundary(seq_env):
    """A 의 필러 "음"(1.0~1.3) 끝에서 0.07초 뒤에 B 를 넣는다. B 는 작은 소리로 시작해 (A 의 소리보다 조용)
    이어 붙인 에너지로 경계를 넓히면 B 쪽으로 넘어간다 — 조각마다 다듬어야 넘지 않는다."""
    b = seq_env.tmp / "b2.mov"
    _media(b, 2, r"0.07*between(t\,0\,0.1)+between(t\,0.1\,1.5)")
    split = 41   # 1.3667초 (프레임)
    seq_env.sq.write_text(json.dumps([
        {"id": "a1", "clip": "A", "src": str(seq_env.a), "in": 0, "out": split / FPS},
        {"id": "b1", "clip": "B", "src": str(b), "in": 0, "out": 2},
        {"id": "a2", "clip": "A", "src": str(seq_env.a), "in": split / FPS, "out": 5}]), encoding="utf-8")
    old = list(B_WORDS)
    B_WORDS[:] = [("비디오", 0.0, 1.5)]
    try:
        data = seq_env.run(seq_env.a, seq_env.tmp / "out", "--sequence", seq_env.sq)
    finally:
        B_WORDS[:] = old
    edges = (split, split + 2 * FPS)
    assert any("filler" in c["reason"] for c in data["cuts"])
    for c in data["cuts"]:
        for f in edges:
            assert not (c["start_frame"] < f < c["end_frame"]), c   # 어떤 컷도 조각 경계를 가로지르지 않는다


@needs_ffmpeg
def test_undo_insert_reuses_file_cache(seq_env):
    """끼워 넣기를 되돌려 단독으로 다시 돌리면 work/files/ 캐시를 되돌려 써 A 를 다시 전사하지 않고 지문도 처음과 같다."""
    out = seq_env.tmp / "out"
    first = seq_env.run(seq_env.a, out)
    sha = pipeline.transcript_sha1(json.loads((out / "a.transcript.json").read_text(encoding="utf-8")))
    seq_env.run(seq_env.a, out, "--sequence", seq_env.sq)
    assert seq_env.calls == [5.0, 2.0]
    again = seq_env.run(seq_env.a, out)
    assert seq_env.calls == [5.0, 2.0]   # 세 번째엔 전사 없음
    tr = json.loads((out / "a.transcript.json").read_text(encoding="utf-8"))
    assert "cache_key" in tr and "sequence_key" not in tr
    assert pipeline.transcript_sha1(tr) == sha == again["transcript_sha1"]
    assert _frames(again["cuts"]) == _frames(first["cuts"])
