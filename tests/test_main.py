import sys
import types

import pytest

from app import __main__ as entry


def test_pipeline_dispatch(monkeypatch):
    seen = {}
    fake = types.ModuleType("pipeline")
    fake.main = lambda: seen.setdefault("argv", list(sys.argv)) and "ran"
    monkeypatch.setitem(sys.modules, "pipeline", fake)
    monkeypatch.setattr(sys, "argv", ["orig"])
    entry.main(["/app/youtube-edit", "--pipeline", "in.mp4", "--review"])
    assert seen["argv"] == ["/app/youtube-edit", "in.mp4", "--review"]


def test_pipeline_dispatch_real_module(monkeypatch):
    """scripts/pipeline.py 를 bundle_dir()/scripts 로 찾아 main() 을 부른다 (--help 로 바로 끝냄)."""
    monkeypatch.delitem(sys.modules, "pipeline", raising=False)
    monkeypatch.setattr(sys, "argv", ["orig"])
    with pytest.raises(SystemExit) as e:
        entry.main(["x", "--pipeline", "--help"])
    assert e.value.code == 0


def test_probe_gpu_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("YTEDIT_DATA_DIR", str(tmp_path))
    got = {}

    def probe(root):
        got["root"] = root
        return 3
    monkeypatch.setattr("app.models.run_gpu_probe", probe)
    with pytest.raises(SystemExit) as e:
        entry.main(["x", "--probe-gpu"])
    assert e.value.code == 3
    assert got["root"] == tmp_path


def test_default_runs_window(monkeypatch):
    ran = []
    monkeypatch.setattr("app.window.run", lambda: ran.append(True))
    entry.main(["x"])
    assert ran == [True]


def test_dash_c_runs_code(monkeypatch, capsys):
    """묶은 앱에선 sys.executable 이 파이썬이 아니므로 pipeline.probe_whisper_windows 의
    [sys.executable, "-c", code] 가 창을 띄우지 않고 코드를 실행해야 한다."""
    monkeypatch.setattr("app.window.run", lambda: pytest.fail("창을 띄우면 안 됨"))
    entry.main(["x", "-c", "import sys; print('hi', __name__)"])
    assert capsys.readouterr().out == "hi __main__\n"


def test_dash_c_exit_code(monkeypatch):
    monkeypatch.setattr("app.window.run", lambda: pytest.fail("창을 띄우면 안 됨"))
    with pytest.raises(SystemExit) as e:
        entry.main(["x", "-c", "import sys; sys.exit(7)"])
    assert e.value.code == 7


def test_pipeline_puts_bundled_ffmpeg_first(monkeypatch, tmp_path):
    fake = types.ModuleType("pipeline")
    fake.main = lambda: None
    monkeypatch.setitem(sys.modules, "pipeline", fake)
    monkeypatch.setattr(sys, "argv", ["orig"])
    monkeypatch.setattr("app.paths.ffmpeg_bin_dir", lambda: tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")
    entry.main(["x", "--pipeline", "in.mp4"])
    import os
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    entry.main(["x", "--pipeline", "in.mp4"])  # 두 번 불려도 한 번만 붙인다
    assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path)) == 1


def test_pipeline_path_unchanged_when_not_frozen(monkeypatch):
    fake = types.ModuleType("pipeline")
    fake.main = lambda: None
    monkeypatch.setitem(sys.modules, "pipeline", fake)
    monkeypatch.setattr(sys, "argv", ["orig"])
    monkeypatch.setenv("PATH", "/usr/bin")
    entry.main(["x", "--pipeline", "in.mp4"])
    import os
    assert os.environ["PATH"] == "/usr/bin"
