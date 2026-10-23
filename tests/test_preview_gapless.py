"""끊김 없는 미리보기: 기준 프레임 조각 영상 + 전체 소리 폴더."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pipeline  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


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
