import pipeline


def test_defaults_unchanged():
    a = pipeline.build_parser().parse_args(["x.mp4"])
    assert a.work_root == pipeline.ROOT / "work"
    assert a.media_out is None and a.ctc_model == pipeline.CTC_MODEL


def test_new_paths_parse(tmp_path):
    a = pipeline.build_parser().parse_args(["x.mp4", "--work-root", str(tmp_path), "--media-out", str(tmp_path / "m"), "--ctc-model", "/m/ctc"])
    assert a.work_root == tmp_path and a.media_out == tmp_path / "m" and a.ctc_model == "/m/ctc"


def test_preview_cut_audio_requires_apply_cuts(monkeypatch):
    import pytest
    monkeypatch.setattr(pipeline.sys, "argv", ["pipeline.py", "x.mp4", "--preview-cut-audio", "a.m4a"])
    with pytest.raises(SystemExit) as e:
        pipeline.main()
    assert "--apply-cuts" in str(e.value)


def test_preview_cut_audio_rejects_review(monkeypatch):
    import pytest
    monkeypatch.setattr(pipeline.sys, "argv", ["pipeline.py", "x.mp4", "--apply-cuts", "c.json", "--review",
                                               "--preview-cut-audio", "a.m4a"])
    with pytest.raises(SystemExit):
        pipeline.main()
