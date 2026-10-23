from fractions import Fraction

import pipeline


def test_progress_line(capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "PROGRESS", True)
    pipeline.progress("transcribe", 0.5)
    assert capsys.readouterr().out.strip() == '@progress {"stage": "transcribe", "frac": 0.5}'


def test_progress_silent_by_default(capsys):
    pipeline.progress("audio", 1.0)
    assert capsys.readouterr().out == ""


def test_render_audio_defaults_same():
    a = pipeline.build_parser().parse_args(["x.mp4"])
    assert (a.render_denoise, a.render_loudnorm, a.vbitrate_scale, a.preview) == ("same", "same", 1.0, None)


def test_bitrate_scale():
    info = {"width": 1920, "height": 1080, "fps": Fraction(30), "high_bit": False}
    assert pipeline.auto_bitrate(info) == "8M"
    assert pipeline.auto_bitrate(info, scale=0.6) == "4M"   # 하한 4M
    assert pipeline.auto_bitrate(info, scale=1.5) == "12M"


def test_scale_applies_to_software_crf():
    info = {"width": 1920, "height": 1080, "fps": Fraction(30), "high_bit": False, "color": {}}
    def crf(scale, vbitrate="auto"):
        _, a = pipeline.video_encoder_args(info, False, " libx264 ", 18, vbitrate, "medium", scale)
        return a[a.index("-crf") + 1]
    assert (crf(0.6), crf(1.0), crf(1.5)) == ("22", "18", "14")
    assert crf(0.6, vbitrate="20M") == "18"   # 비트레이트를 직접 정하면 배율 무시


def test_preview_failure_does_not_fail_analysis(tmp_path, monkeypatch, capsys):
    """--preview 실패는 분석을 멈추지 않고 미리보기 파일만 지운다 (final-review M8)."""
    import subprocess
    out = tmp_path / "preview"
    out.mkdir()
    (out / "video.mp4").write_bytes(b"old")               # 옛 미리보기가 섞이지 않게 지운다
    (out / "audio_full.part.m4a").write_bytes(b"half")

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, ["ffmpeg"])
    monkeypatch.setattr(pipeline, "make_preview", boom)
    monkeypatch.setattr(pipeline, "PROGRESS", True)
    args = (tmp_path / "s.mp4", tmp_path / "c.wav", {}, {}, out, tmp_path, False, False)
    assert pipeline.try_preview(*args) is False
    assert not list(out.iterdir())
    text = capsys.readouterr().out
    assert "미리보기를 만들지 못했습니다" in text and '"stage": "preview"' in text.replace("'", '"')
    monkeypatch.setattr(pipeline, "make_preview", lambda *a, **k: None)
    assert pipeline.try_preview(*args) is True


def test_run_decodes_utf8_regardless_of_locale(monkeypatch):
    # 윈도우(cp949)에서 ffprobe 의 UTF-8 한글을 로캘로 읽으면 stdout 이 None → json.loads TypeError
    import subprocess
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    pipeline.run(["ffprobe", "영상.mp4"], quiet=True)
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
