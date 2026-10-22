from app import paths


def test_job_dir_same_name_different_folder(tmp_path):
    assert paths.job_dir(tmp_path / "a" / "강의.mp4") != paths.job_dir(tmp_path / "b" / "강의.mp4")


def test_job_dir_unicode_path(tmp_path):
    p = tmp_path / "바탕 화면" / "강의 03 (최종).mov"
    assert paths.job_dir(p) == paths.job_dir(p)
    assert paths.job_dir(p).name.isascii()


def test_data_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("YTEDIT_DATA_DIR", str(tmp_path / "d"))
    assert paths.data_dir() == tmp_path / "d"
    assert (tmp_path / "d").is_dir()
    assert paths.job_dir(tmp_path / "x.mp4").parent == tmp_path / "d" / "jobs"


def test_ffmpeg_bin_dir_none_in_dev():
    assert paths.ffmpeg_bin_dir() is None
