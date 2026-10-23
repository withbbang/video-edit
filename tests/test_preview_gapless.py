"""끊김 없는 미리보기: fMP4 조각 목록 읽기(합성 바이트, ffmpeg 없이) + 기준 프레임 조각 영상·전체 소리 폴더."""
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pipeline  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


def box(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def sample_fmp4(audio: bool = False) -> bytes:
    mdhd = box(b"mdhd", struct.pack(">B3xIII", 0, 0, 0, 1000) + b"\0" * 8)  # v0: 오프셋 12 에 timescale
    if audio:
        entry = box(b"mp4a", b"\0" * 28 + box(b"esds", b"\0" * 8))
    else:
        entry = box(b"avc1", b"\0" * 78 + box(b"avcC", bytes([1, 0x64, 0x00, 0x20]) + b"\0" * 4))
    stsd = box(b"stsd", b"\0" * 8 + entry)
    moov = box(b"moov", box(b"trak", box(b"mdia", mdhd + box(b"minf", box(b"stbl", stsd))))
               + box(b"mvex", box(b"trex", b"\0" * 24)))
    tfdt1 = box(b"tfdt", struct.pack(">B3xQ", 1, 0))
    tfdt0 = box(b"tfdt", struct.pack(">B3xI", 0, 500))
    frag1 = box(b"moof", box(b"mfhd", b"\0" * 8) + box(b"traf", tfdt1)) + box(b"mdat", b"x" * 20)
    frag2 = box(b"moof", box(b"traf", tfdt0)) + box(b"mdat", b"y" * 30)
    return box(b"ftyp", b"isom\0\0\0\0isom") + moov + frag1 + frag2


def moov_end(data: bytes) -> int:
    return data.index(b"moof") - 4


def test_fmp4_index_reads_fragments(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(sample_fmp4())
    idx = pipeline.fmp4_index(p)
    end = moov_end(p.read_bytes())
    assert idx["timescale"] == 1000 and idx["codec"] == "avc1.640020"
    assert idx["init"] == [0, end]
    assert [f[0] for f in idx["frags"]] == [0.0, 0.5]
    assert idx["frags"][0][1] == end and idx["frags"][-1][2] == p.stat().st_size
    assert idx["frags"][0][2] == idx["frags"][1][1]


def test_fmp4_index_audio_codec(tmp_path):
    p = tmp_path / "a.m4a"
    p.write_bytes(sample_fmp4(audio=True))
    assert pipeline.fmp4_index(p)["codec"] == "mp4a.40.2"


def test_write_index_merges_extra(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(sample_fmp4())
    out = pipeline.write_index(p, {"keeps": [[0, 1]]})
    assert out == tmp_path / "v.index.json"
    d = json.loads(out.read_text())
    assert d["keeps"] == [[0, 1]] and d["timescale"] == 1000 and len(d["frags"]) == 2


def test_fmp4_index_empty_file(tmp_path):
    p = tmp_path / "e.mp4"
    p.write_bytes(b"")
    assert pipeline.fmp4_index(p)["frags"] == []


def synth(tmp_path, video=True):
    src = tmp_path / ("s.mp4" if video else "s.wav")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=2"]
    cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"] if video else ["-c:a", "pcm_s16le"]
    subprocess.run([*cmd, src], check=True)
    wav = tmp_path / "clean.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn", "-ar", "48000",
                    "-c:a", "pcm_s16le", wav], check=True)
    return src, wav


def probe_frames(path):
    def out(*extra):
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *extra, "-of", "csv=p=0",
                               path], capture_output=True, text=True, check=True).stdout.split()
    wh = out("-show_entries", "stream=width,height")[0].split(",")
    nkey = len(out("-skip_frame", "nokey", "-show_entries", "frame=pts_time"))
    nall = len(out("-show_entries", "frame=pts_time"))
    return int(wh[0]), int(wh[1]), nkey, nall


def make(tmp_path, src, wav, key=None):
    info = pipeline.ffprobe(src)
    pipeline.make_preview(src, wav, key or {"k": 1}, info, tmp_path / "preview", tmp_path / "work", False, False)
    return info


@needs_ffmpeg
def test_make_preview_writes_intra_fragments(tmp_path):
    src, wav = synth(tmp_path)
    make(tmp_path, src, wav)
    v = tmp_path / "preview" / "video.mp4"
    idx = json.loads((tmp_path / "preview" / "video.index.json").read_text())
    assert len(idx["frags"]) >= 3 and idx["codec"].startswith("avc1.")
    w, h, nkey, nall = probe_frames(v)
    assert max(w, h) == 960 and nkey == nall
    assert idx["frame"] == pytest.approx(1 / 30)
    assert (tmp_path / "preview" / "audio_full.m4a").is_file()
    assert (tmp_path / "preview" / "audio_full.index.json").is_file()
    assert json.loads((tmp_path / "preview" / "video.meta.json").read_text())["v"] == 3
    assert not list((tmp_path / "preview").glob("*.part.*"))


@needs_ffmpeg
def test_make_preview_audio_only(tmp_path):
    src, wav = synth(tmp_path, video=False)
    make(tmp_path, src, wav)
    d = tmp_path / "preview"
    assert not (d / "video.mp4").exists() and not (d / "video.index.json").exists()
    assert (d / "audio_full.m4a").is_file() and (d / "audio_full.index.json").is_file()


@needs_ffmpeg
def test_make_preview_reuses_video_when_audio_changes(tmp_path):
    src, wav = synth(tmp_path)
    make(tmp_path, src, wav)
    v, a = tmp_path / "preview" / "video.mp4", tmp_path / "preview" / "audio_full.m4a"
    v1, a1 = v.stat().st_mtime_ns, a.stat().st_mtime_ns
    make(tmp_path, src, wav)                      # 그대로 → 둘 다 재사용
    assert (v.stat().st_mtime_ns, a.stat().st_mtime_ns) == (v1, a1)
    make(tmp_path, src, wav, {"k": 2})            # 소리 키만 바뀜
    assert v.stat().st_mtime_ns == v1 and a.stat().st_mtime_ns != a1


def test_try_preview_cleans_up_on_failure(tmp_path, monkeypatch):
    d = tmp_path / "preview"
    d.mkdir()
    for n in ("video.mp4", "video.part.mp4", "audio_full.m4a", "audio_full.part.m4a"):
        (d / n).write_bytes(b"x")

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "ffmpeg")
    monkeypatch.setattr(pipeline, "make_preview", boom)
    assert pipeline.try_preview(tmp_path / "s.mp4", tmp_path / "c.wav", {}, {}, d, tmp_path, False, False) is False
    assert not list(d.glob("*.mp4")) and not list(d.glob("*.m4a"))


def sine_wav(path, sec, sr):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate={sr}:duration={sec}", "-c:a", "pcm_s16le", path], check=True)
    return path


def probe_duration(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 path], capture_output=True, text=True, check=True).stdout.strip())


@needs_ffmpeg
def test_make_cut_preview_audio_matches_keeps(tmp_path):
    from fractions import Fraction
    wav = sine_wav(tmp_path / "a.wav", sec=4.0, sr=48000)
    fps = Fraction(30)
    keeps = [(0, 30), (45, 48), (90, 120)]                 # 1초, 3프레임, 1초
    out = tmp_path / "sub" / "audio_cut.x.m4a"
    idx = pipeline.make_cut_preview_audio(wav, keeps, fps, out, tmp_path)
    assert idx["keeps"] == [[0.0, 1.0], [1.5, 1.6], [3.0, 4.0]]
    assert abs(idx["duration"] - 2.1) < 1e-3                # cut_audio 샘플 수 기준
    assert probe_duration(out) == pytest.approx(2.1, abs=0.05)
    assert idx["frags"] and idx["codec"] == "mp4a.40.2"
    assert json.loads(out.with_suffix(".index.json").read_text(encoding="utf-8")) == idx
    assert not list(out.parent.glob("*.part.*"))


# ---- --preview-cut-audio 는 전사·분석 오디오를 새로 만들지 않는다 -------------------------------

def _no_whisper(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Whisper 를 불러오면 안 된다")
    monkeypatch.setattr(pipeline, "load_whisper", boom)


def _cut_audio_setup(tmp_path, *, audio=True, transcript="ok"):
    """작은 합성 영상 + (선택) 분석 오디오 캐시 + 전사 캐시(ok | stale | None) + 컷 목록. argv 를 돌려준다."""
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", src], check=True)
    out, work_root = tmp_path / "out", tmp_path / "work"
    out.mkdir()
    work = work_root / "s"
    work.mkdir(parents=True)
    info = pipeline.ffprobe(src)
    _, _, audio_key = pipeline.prepare_audio(src, work, info, "none", True, False)
    if not audio:
        (work / "audio.meta.json").unlink()
    tr = {"language": "ko", "duration": 2.0, "model": "large-v3-turbo",
          "cache_key": {"audio": audio_key, "model": "large-v3-turbo", "compute_type": "int8",
                        "hotwords": pipeline.load_fillers(pipeline.DEFAULT_FILLERS).get("prompt", "")},
          "segments": [{"id": 0, "start": 0.1, "end": 1.9, "text": "안녕",
                        "words": [{"word": " 안녕", "start": 0.1, "end": 1.9, "prob": 0.9}]}]}
    if transcript == "stale":
        tr["cache_key"]["compute_type"] = "int8_float16"
    tr_path = out / "s.transcript.json"
    if transcript:
        tr_path.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
    cuts = out / "s.cuts.json"
    cuts.write_text(json.dumps({"fps": "30/1", "transcript_sha1": pipeline.transcript_sha1(tr),
                                "cuts": [{"start": 0.5, "end": 1.0, "word": "어", "reason": "filler",
                                          "context": "", "seg": 0}]}), encoding="utf-8")
    m4a = tmp_path / "preview" / "audio_cut.k.m4a"
    argv = ["pipeline.py", str(src), "--apply-cuts", str(cuts), "--preview-cut-audio", str(m4a),
            "--out", str(out), "--work-root", str(work_root), "--denoise", "none"]
    return argv, tr_path, m4a, work


@needs_ffmpeg
@pytest.mark.parametrize("transcript", ["stale", None])
def test_preview_cut_audio_never_transcribes(tmp_path, monkeypatch, transcript):
    argv, tr_path, m4a, _ = _cut_audio_setup(tmp_path, transcript=transcript)
    before = tr_path.read_bytes() if transcript else None
    _no_whisper(monkeypatch)
    monkeypatch.setattr(pipeline.sys, "argv", argv)
    with pytest.raises(SystemExit) as e:
        pipeline.main()
    assert e.value.code == "오류: 미리보기 소리: 전사 캐시가 없거나 맞지 않습니다"
    assert (tr_path.read_bytes() if tr_path.exists() else None) == before
    assert not m4a.exists()


@needs_ffmpeg
def test_preview_cut_audio_needs_analysis_audio_cache(tmp_path, monkeypatch):
    argv, tr_path, m4a, work = _cut_audio_setup(tmp_path, audio=False)
    wav = (work / "clean.wav").stat().st_mtime_ns
    _no_whisper(monkeypatch)
    monkeypatch.setattr(pipeline.sys, "argv", argv)
    with pytest.raises(SystemExit) as e:
        pipeline.main()
    assert e.value.code == "오류: 미리보기 소리: 분석 오디오 캐시가 없거나 맞지 않습니다"
    assert (work / "clean.wav").stat().st_mtime_ns == wav and not (work / "audio.meta.json").exists()
    assert not m4a.exists()


@needs_ffmpeg
def test_preview_cut_audio_uses_caches(tmp_path, monkeypatch):
    argv, tr_path, m4a, _ = _cut_audio_setup(tmp_path)
    before = tr_path.read_bytes()
    _no_whisper(monkeypatch)
    monkeypatch.setattr(pipeline.sys, "argv", argv + ["--force"])   # --force 여도 캐시만 쓴다
    pipeline.main()
    assert m4a.is_file() and m4a.with_suffix(".index.json").is_file()
    assert tr_path.read_bytes() == before


@needs_ffmpeg
def test_make_preview_removes_old_work_preview_files(tmp_path):
    src, wav = synth(tmp_path, video=False)
    work = tmp_path / "work"
    work.mkdir()
    old = ["preview_video.mp4", "preview_video.part.mp4", "preview_video.meta.json", "preview.meta.json"]
    for n in old + ["clean.wav"]:
        (work / n).write_bytes(b"x")
    make(tmp_path, src, wav)
    assert [n for n in old if (work / n).exists()] == []
    assert (work / "clean.wav").exists()


def test_make_cut_preview_audio_removes_leftover_wav_first(tmp_path, monkeypatch):
    from fractions import Fraction
    left = tmp_path / "preview_cut.wav"
    left.write_bytes(b"old")
    seen = []

    def fake_cut_audio(src, dst, keeps, fps):
        seen.append(Path(dst).exists())
        raise RuntimeError("stop")
    monkeypatch.setattr(pipeline, "cut_audio", fake_cut_audio)
    with pytest.raises(RuntimeError):
        pipeline.make_cut_preview_audio(tmp_path / "a.wav", [(0, 30)], Fraction(30), tmp_path / "o.m4a", tmp_path)
    assert seen == [False] and not left.exists()
