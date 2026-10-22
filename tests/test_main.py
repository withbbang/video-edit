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


def _frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)


def test_dash_c_runs_code(monkeypatch, capsys, tmp_path):
    """묶은 앱에선 sys.executable 이 파이썬이 아니므로 pipeline.probe_whisper_windows 의
    [sys.executable, "-c", code] 가 창을 띄우지 않고 코드를 실행해야 한다."""
    _frozen(monkeypatch, tmp_path)
    monkeypatch.setattr("app.window.run", lambda: pytest.fail("창을 띄우면 안 됨"))
    entry.main(["x", "-c", "import sys; print('hi', __name__)"])
    assert capsys.readouterr().out == "hi __main__\n"


def test_dash_c_exit_code(monkeypatch, tmp_path):
    _frozen(monkeypatch, tmp_path)
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


def test_dash_c_ignored_when_not_frozen(monkeypatch):
    """개발 실행(python -m app)에선 -c 를 받지 않는다."""
    ran = []
    monkeypatch.setattr("app.window.run", lambda: ran.append(True))
    entry.main(["x", "-c", "raise SystemExit(5)"])
    assert ran == [True]


# ---- 파이프라인 하위 프로세스 출력은 항상 UTF-8 (final-review I3) ----

def test_pipeline_mode_writes_utf8_even_with_other_locale(monkeypatch):
    """묶은 윈도우 앱은 PYTHONIOENCODING 을 따르지 않아 파이프 출력이 cp949 가 될 수 있다.
    --pipeline 은 stdout/stderr 를 UTF-8 로 다시 맞춰 runner(UTF-8 로 읽음)가 `오류:` 줄을 그대로 받게 한다."""
    import io
    out_b, err_b = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out_b, encoding="cp949"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err_b, encoding="cp949"))
    fake = types.ModuleType("pipeline")

    def run():
        print("오류: 입력 파일이 없습니다: 강의 03.mp4")
        print("오류: 둘째 줄 ✓", file=sys.stderr)
    fake.main = run
    monkeypatch.setitem(sys.modules, "pipeline", fake)
    monkeypatch.setattr(sys, "argv", ["orig"])
    entry.main(["x", "--pipeline", "in.mp4"])
    sys.stdout.flush()
    sys.stderr.flush()
    assert out_b.getvalue().decode("utf-8") == "오류: 입력 파일이 없습니다: 강의 03.mp4\n"
    assert err_b.getvalue().decode("utf-8") == "오류: 둘째 줄 ✓\n"


def test_pipeline_mode_tolerates_missing_stdio(monkeypatch):
    """창 없는 실행 파일은 sys.stdout/stderr 가 None 일 수 있다."""
    fake = types.ModuleType("pipeline")
    fake.main = lambda: "ran"
    monkeypatch.setitem(sys.modules, "pipeline", fake)
    monkeypatch.setattr(sys, "argv", ["orig"])
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert entry.main(["x", "--pipeline", "in.mp4"]) == "ran"


def test_dash_c_writes_utf8(monkeypatch, tmp_path):
    import io
    _frozen(monkeypatch, tmp_path)
    buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp949"))
    entry.main(["x", "-c", "print('오류: 한글 ✓')"])
    sys.stdout.flush()
    assert buf.getvalue().decode("utf-8") == "오류: 한글 ✓\n"


def test_runner_reads_child_error_in_korean_with_cp949_env(tmp_path):
    """실제 하위 프로세스: 자식 환경이 cp949 를 가리켜도 `오류:` 줄이 깨지지 않고 실패 이유가 된다."""
    import os
    import subprocess
    from pathlib import Path
    from app import runner
    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    cmd = [sys.executable, "-m", "app", "--pipeline", str(tmp_path / "없는 영상.mp4"), "--review"]
    r = subprocess.run(cmd, cwd=root, env={**env, "PYTHONIOENCODING": "cp949"}, capture_output=True)
    assert "오류:" in r.stdout.decode("utf-8") + r.stderr.decode("utf-8")
    res = runner.run(cmd, {}, lambda p: None, env={"PYTHONIOENCODING": "cp949", "PYTHONPATH": str(root)})
    assert not res.ok and res.error.startswith("오류:") and "없는 영상.mp4" in res.error
