"""배경 잡음 세기 --denoise-level 0~100: 0 = 끔, 50 = 지금의 rnnoise(mix 0.85), 51~100 은 mix 를 올리고 afftdn 을 더한다.
결과·미리보기 오디오에만 쓰고 분석 오디오(clean.wav·asr.wav)는 그대로. 미리보기는 소리만 바꿔 끼운다."""
import re
import shutil
import subprocess

import pytest

import pipeline

M = "models/sh.rnnn"


def mix(chain: str) -> float:
    m = re.search(r"arnndn=m=[^:]+:mix=([0-9.]+)", chain)
    return float(m.group(1)) if m else 0.0


def nr(chain: str) -> float:
    m = re.search(r"afftdn=nf=-25:nr=([0-9.]+)", chain)
    return float(m.group(1)) if m else 0.0


def test_level_50_matches_rnnoise():
    assert pipeline.audio_filter_chain("rnnoise", True, M, 0, denoise_level=50) == \
        pipeline.audio_filter_chain("rnnoise", True, M, 0)
    # level 이 있으면 --denoise 문자열은 무시
    assert pipeline.audio_filter_chain("none", False, M, 0.2, denoise_level=50) == \
        pipeline.audio_filter_chain("rnnoise", False, M, 0.2)


def test_level_0_no_denoise():
    chain = pipeline.audio_filter_chain("rnnoise", True, M, 0, denoise_level=0)
    assert "arnndn" not in chain and "afftdn" not in chain
    assert chain == pipeline.audio_filter_chain("none", True, M, 0)
    assert pipeline.denoise_filters(0, M) == []


def test_level_increases_strength():
    c = {n: pipeline.audio_filter_chain("rnnoise", True, M, 0, denoise_level=n) for n in range(0, 101, 10)}
    assert mix(c[30]) < mix(c[50])
    assert "afftdn" in c[80] and "afftdn" not in c[50]
    strength = [(mix(c[n]), nr(c[n])) for n in range(0, 101, 10)]
    assert all(a < b for a, b in zip(strength, strength[1:]))   # 10 단위로 늘 세진다
    assert mix(c[100]) == 1.0


def test_parser_denoise_level():
    p = pipeline.build_parser()
    assert p.parse_args(["x.mp4"]).denoise_level is None
    assert p.parse_args(["x.mp4", "--denoise-level", "80"]).denoise_level == 80
    with pytest.raises(SystemExit):
        p.parse_args(["x.mp4", "--denoise-level", "101"])


def test_render_audio_note_level():
    a = pipeline.build_parser().parse_args(["x.mp4"])
    assert pipeline.render_audio_note(a) == ""
    a = pipeline.build_parser().parse_args(["x.mp4", "--denoise-level", "70"])
    assert "세기 70" in pipeline.render_audio_note(a)


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")


def synth_video(path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path], check=True)


@needs_ffmpeg
def test_preview_audio_swap_reuses_video(tmp_path):
    src = tmp_path / "s.mp4"
    synth_video(src)
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "job" / "preview.mp4"
    info = pipeline.ffprobe(src)

    wav_a, key_a = pipeline.render_audio_for(src, work, info, "none", False, False, denoise_level=0)
    pipeline.make_preview(src, wav_a, key_a, info, out, work, False, False)
    video = work / "preview_video.mp4"
    assert video.exists() and out.exists()
    v1, p1 = video.stat(), out.stat()

    # 같은 소리로 다시 → 아무것도 안 씀
    pipeline.make_preview(src, wav_a, key_a, info, out, work, False, False)
    assert out.stat().st_ino == p1.st_ino and out.stat().st_mtime_ns == p1.st_mtime_ns

    wav_b, key_b = pipeline.render_audio_for(src, work, info, "none", False, False, denoise_level=30)
    assert key_b != key_a
    pipeline.make_preview(src, wav_b, key_b, info, out, work, False, False)
    v2, p2 = video.stat(), out.stat()
    assert (v2.st_ino, v2.st_mtime_ns) == (v1.st_ino, v1.st_mtime_ns)   # 영상은 다시 인코딩하지 않음
    assert (p2.st_ino, p2.st_mtime_ns) != (p1.st_ino, p1.st_mtime_ns)   # 미리보기는 새로 씀
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                            "-of", "csv=p=0", out], capture_output=True, text=True, check=True).stdout.split()
    assert sorted(probe) == ["audio", "video"]


@needs_ffmpeg
def test_render_audio_same_as_analysis_reuses_clean(tmp_path):
    """세기 50 + 같은 음량 설정이면 분석용 clean.wav 와 필터가 같아 그대로 쓴다 (따로 만들지 않음)."""
    src = tmp_path / "s.mp4"
    synth_video(src)
    work = tmp_path / "work"
    work.mkdir()
    info = pipeline.ffprobe(src)
    clean, _, key = pipeline.prepare_audio(src, work, info, "rnnoise", True, False)
    wav, k = pipeline.render_audio_for(src, work, info, "rnnoise", True, False, denoise_level=50,
                                       analysis=(clean, key))
    assert wav == clean and k == key and not (work / "render.wav").exists()
    wav, k = pipeline.render_audio_for(src, work, info, "rnnoise", True, False, denoise_level=80,
                                       analysis=(clean, key))
    assert wav == work / "render.wav" and "afftdn" in k["filter"]
