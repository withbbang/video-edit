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
