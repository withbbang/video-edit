"""--sequence 렌더·미리보기 영상: 조각마다 A 에 맞춰 trim·scale·pad·fps 한 뒤 concat (실제 ffmpeg, 합성 영상)."""
import json
import shutil
import subprocess
import sys
import wave
from fractions import Fraction
from types import SimpleNamespace

import pytest

import pipeline
import sequence as seq

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")

A_WORDS = [("안녕하세요", 0.5, 1.5), ("반갑습니다", 2.5, 3.5)]


class _FakeModel:
    def __init__(self, calls):
        self.calls = calls

    def transcribe(self, path, **kw):
        with wave.open(str(path), "rb") as w:
            dur = w.getnframes() / w.getframerate()
        self.calls.append(round(dur, 1))
        seg = SimpleNamespace(start=A_WORDS[0][1], end=A_WORDS[-1][2], text=" ".join(x[0] for x in A_WORDS),
                              words=[SimpleNamespace(word=" " + x, start=s, end=e, probability=0.9)
                                     for x, s, e in A_WORDS])
        return iter([seg]), SimpleNamespace(language="ko", duration=dur)


def _ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, a)], check=True)


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,duration,width,height,nb_read_frames", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mov", tmp_path / "b.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=4",
        "-f", "lavfi", "-i", "aevalsrc=0.3*sin(2*PI*440*t):s=48000:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-shortest", a)
    _ff("-f", "lavfi", "-i", "testsrc=size=160x160:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", b)
    calls = []
    model = _FakeModel(calls)
    monkeypatch.setattr(pipeline, "load_whisper", lambda *x, **k: model)
    monkeypatch.setattr(pipeline, "ctc_fix_fillers", lambda *x, **k: (0, []))
    sq = tmp_path / "seq.json"

    def write_seq(b_out=1.0):
        sq.write_text(json.dumps([
            {"id": "a1", "clip": "A", "src": str(a), "in": 0, "out": 2},
            {"id": "b1", "clip": "B", "src": str(b), "in": 0, "out": b_out},
            {"id": "a2", "clip": "A", "src": str(a), "in": 2, "out": 4}]), encoding="utf-8")
    write_seq()

    def run(*extra):
        monkeypatch.setattr(sys, "argv", ["pipeline.py", str(a), "--denoise", "none", "--no-loudnorm",
                                          "--silence", "off", "--filler", "off", "--habit", "off", "--hw", "off",
                                          "--out", str(tmp_path / "out"), "--work-root", str(tmp_path / "work"),
                                          "--sequence", str(sq), *map(str, extra)])
        pipeline.main()
    return SimpleNamespace(a=a, b=b, sq=sq, write_seq=write_seq, run=run, tmp=tmp_path, calls=calls)


@needs_ffmpeg
def test_render_mixed_sources(env):
    env.run()
    mp4 = env.tmp / "out" / "a_clean.mp4"
    st = _probe(mp4)
    v, au = st["video"], st["audio"]
    fr = 1 / 30
    assert abs(float(v["duration"]) - 5.0) <= fr + 1e-6
    assert abs(float(v["duration"]) - float(au["duration"])) <= fr + 1e-6
    assert int(v["nb_read_frames"]) == 150
    assert (int(v["width"]), int(v["height"])) == (320, 240)
    assert "subtitle" in st   # 자막 트랙은 그대로
    # 2.5초 = B 구간 (160x160 → 240x240 + 좌우 40px 검은 여백)
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "2.5", "-i", str(mp4),
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    assert len(raw) == 320 * 240
    rows = [raw[y * 320:(y + 1) * 320] for y in range(0, 240, 20)]
    assert all(max(r[:36]) < 30 and max(r[-36:]) < 30 for r in rows)
    assert max(max(r[44:276]) for r in rows) > 60   # 가운데는 B 그림
    report = (env.tmp / "out" / "a.report.md").read_text(encoding="utf-8")
    assert "## 조각" in report and "b.mp4" in report
    assert env.calls == [4.0]   # 소리 없는 B 는 전사하지 않음


@needs_ffmpeg
def test_render_apply_cuts_sequence(env):
    env.run("--review")
    cuts = env.tmp / "out" / "a.cuts.json"
    data = json.loads(cuts.read_text(encoding="utf-8"))
    data["cuts"] = [{"start": 1.0, "end": 1.5, "word": "x", "reason": "manual", "context": "", "seg": -1},
                    {"start": 2.5, "end": 2.8, "word": "y", "reason": "manual", "context": "", "seg": -1}]
    cuts.write_text(json.dumps(data), encoding="utf-8")
    env.run("--apply-cuts", cuts)
    st = _probe(env.tmp / "out" / "a_clean.mp4")
    assert int(st["video"]["nb_read_frames"]) == 150 - 15 - 9
    assert abs(float(st["video"]["duration"]) - float(st["audio"]["duration"])) <= 1 / 30 + 1e-6


@needs_ffmpeg
def test_preview_video_sequence_length(env):
    pv = env.tmp / "pv"
    env.run("--review", "--preview", pv)
    st = _probe(pv / "video.mp4")
    assert abs(float(st["video"]["duration"]) - 5.0) <= 1 / 30 + 1e-6
    assert (int(st["video"]["width"]), int(st["video"]["height"])) == (320, 240)
    aud = _probe(pv / "audio_full.m4a")["audio"]
    assert abs(float(aud["duration"]) - 5.0) < 0.05
    assert json.loads((pv / "video.index.json").read_text())["frame"] == pytest.approx(1 / 30)
    env.write_seq(0.5)   # 조각이 바뀌면 미리보기 영상도 다시
    env.run("--review", "--preview", pv)
    assert abs(float(_probe(pv / "video.mp4")["video"]["duration"]) - 4.5) <= 1 / 30 + 1e-6


def _info(w, h, fps, pix="yuv420p", high=False, color=None, has_video=True):
    return {"width": w, "height": h, "fps": Fraction(fps), "pix_fmt": pix, "high_bit": high, "has_video": has_video,
            "rotation": 0, "color": color or {"color_primaries": None, "color_transfer": None, "color_space": None}}


def _spans(tmp_path, names):
    pieces = []
    for i, (n, t0, t1) in enumerate(names):
        p = tmp_path / n
        if not p.exists():
            p.write_bytes(b"x")
        pieces.append(seq.Piece(f"p{i}", n, p, seq.file_key(p), t0, t1))
    return seq.spans(pieces, Fraction(30))


def test_sequence_video_filter_split_and_inputs(tmp_path):
    sp = _spans(tmp_path, [("a.mov", 0, 2), ("b.mp4", 0, 1), ("a.mov", 2, 4)])
    a, b = sp[0].piece.fkey, sp[1].piece.fkey
    infos = {a: _info(320, 240, 30), b: _info(160, 160, 25)}
    frag, inputs, nxt = pipeline.sequence_video_filter(sp, infos, infos[a], 3, 320, 240)
    assert inputs == ["-i", str(tmp_path / "a.mov"), "-i", str(tmp_path / "b.mp4")] and nxt == 5
    assert "[3:v]split=2" in frag and "[4:v]setpts" in frag
    assert "trim=start_frame=0:end_frame=60" in frag and "trim=start_frame=60:end_frame=120" in frag
    assert "trim=start_frame=0:end_frame=30" in frag
    assert frag.count("pad=320:240:(ow-iw)/2:(oh-ih)/2") == 3
    assert "concat=n=3:v=1:a=0" in frag and frag.endswith("[seq]")
    assert "colorspace" not in frag and "zscale" not in frag


def test_sequence_video_filter_no_video_piece(tmp_path):
    sp = _spans(tmp_path, [("a.mov", 0, 2), ("s.wav", 0, 1)])
    a, s = sp[0].piece.fkey, sp[1].piece.fkey
    infos = {a: _info(320, 240, 30), s: _info(0, 0, 30, pix="", has_video=False)}
    frag, inputs, nxt = pipeline.sequence_video_filter(sp, infos, infos[a], 0, 320, 240)
    assert inputs == ["-i", str(tmp_path / "a.mov")] and nxt == 1
    assert "color=c=black:s=320x240:r=30/1" in frag


HDR = {"color_primaries": "bt2020", "color_transfer": "arib-std-b67", "color_space": "bt2020nc"}


@pytest.mark.parametrize("zscale", [True, False])
def test_sequence_video_filter_mixed_depth(tmp_path, monkeypatch, zscale):
    monkeypatch.setattr(pipeline, "ffmpeg_has", lambda kind, name: zscale if name == "zscale" else True)
    sp = _spans(tmp_path, [("a.mov", 0, 2), ("b.mp4", 0, 1)])
    a, b = sp[0].piece.fkey, sp[1].piece.fkey
    infos = {a: _info(320, 240, 30, "yuv420p10le", True, HDR), b: _info(160, 160, 25)}
    frag, _, _ = pipeline.sequence_video_filter(sp, infos, infos[a], 0, 320, 240)
    a_chain, b_chain = frag.split(";")[:2]
    assert ("zscale" in b_chain) == zscale and ("colorspace" in b_chain) != zscale
    assert "zscale" not in a_chain and "colorspace" not in a_chain   # A 는 그대로
    assert b_chain.endswith("format=yuv420p10le[pk1]")
    # 반대 (A 가 SDR, B 가 HDR)
    infos = {a: _info(320, 240, 30), b: _info(160, 160, 25, "yuv420p10le", True, HDR)}
    frag, _, _ = pipeline.sequence_video_filter(sp, infos, infos[a], 0, 320, 240)
    assert ("zscale" in frag) == zscale and frag.split(";")[1].endswith("format=yuv420p[pk1]")


@needs_ffmpeg
def test_sequence_video_filter_mixed_depth_runs(tmp_path):
    """10비트 HDR 표시가 붙은 A 에 8비트 B: 로컬 ffmpeg 가 그래프를 만들고 끝까지 돌린다 (색이 조금 어긋나는 건 받아들임)."""
    a, b = tmp_path / "a.mov", tmp_path / "b.mp4"
    try:
        _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=30:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p10le",
            "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", a)
    except subprocess.CalledProcessError:
        pytest.skip("10비트 libx264 없음")
    _ff("-f", "lavfi", "-i", "testsrc=size=32x32:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", b)
    infos = {seq.file_key(p): pipeline.ffprobe(p, require_audio=False) for p in (a, b)}
    for first, second in ((a, b), (b, a)):   # A 가 HDR / SDR 인 경우 모두
        fps = infos[seq.file_key(first)]["fps"]
        pieces = [seq.Piece("1", "A", first, seq.file_key(first), 0, 0.5),
                  seq.Piece("2", "B", second, seq.file_key(second), 0, 0.5),
                  seq.Piece("3", "A", first, seq.file_key(first), 0.5, 1)]
        frag, inputs, _ = pipeline.sequence_video_filter(seq.spans(pieces, fps), infos, infos[seq.file_key(first)],
                                                         0, 64, 48)
        out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", frag,
                              "-map", "[seq]", "-f", "null", "-"], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr


def test_sequence_video_filter_10bit_sdr_no_conversion(tmp_path):
    """10비트 SDR(bt709) A + 8비트 bt709 B: HDR 이 아니므로 색 변환 없이 깊이만 format= 으로"""
    sp = _spans(tmp_path, [("a.mov", 0, 2), ("b.mp4", 0, 1)])
    a, b = sp[0].piece.fkey, sp[1].piece.fkey
    bt709 = {"color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709"}
    infos = {a: _info(320, 240, 30, "yuv420p10le", True, bt709), b: _info(160, 160, 25, color=dict(bt709))}
    frag, _, _ = pipeline.sequence_video_filter(sp, infos, infos[a], 0, 320, 240)
    assert "zscale" not in frag and "colorspace" not in frag
    assert frag.split(";")[1].endswith("format=yuv420p10le[pk1]")


@needs_ffmpeg
@pytest.mark.parametrize("pieces,expect", [
    ([("a", 0, 2), ("b", 0, 1 / 30), ("a", 2, 4)], 121),       # 1프레임 B
    ([("a", 0, 1 / 30), ("b", 0, 1), ("a", 1 / 30, 4)], 150),  # 1프레임 A1
    ([("a", 0, 1 / 30), ("b", 0, 1 / 30), ("a", 1, 1 + 1 / 30), ("b", 0.5, 0.6), ("a", 3, 4)], 36),
])
def test_one_frame_pieces_keep_every_frame(tmp_path, pieces, expect):
    """concat 이 1프레임 조각의 끝을 어림해 다음 조각과 겹치면 fps 가 한 프레임을 버린다 → 프레임 수가 정확해야 한다"""
    a, b = tmp_path / "a.mov", tmp_path / "b.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=30:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", a)
    _ff("-f", "lavfi", "-i", "testsrc=size=32x32:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", b)
    files = {"a": a, "b": b}
    infos = {seq.file_key(p): pipeline.ffprobe(p, require_audio=False) for p in (a, b)}
    ps = [seq.Piece(str(i), n, files[n], seq.file_key(files[n]), t0, t1) for i, (n, t0, t1) in enumerate(pieces)]
    sp = seq.spans(ps, Fraction(30))
    assert sum(s.f_out - s.f_in for s in sp) == expect
    frag, inputs, _ = pipeline.sequence_video_filter(sp, infos, infos[seq.file_key(a)], 0, 64, 48)
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex",
                          f"{frag};[seq]select=1,setpts=N/FRAME_RATE/TB[v]", "-map", "[v]", "-f", "framemd5", "-"],
                         capture_output=True, text=True, check=True).stdout
    assert sum(1 for line in out.splitlines() if line and not line.startswith("#")) == expect
