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
