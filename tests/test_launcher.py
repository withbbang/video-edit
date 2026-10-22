"""packaging/launcher.py: 묶은 앱에서 처리 안 된 예외는 traceback 을 stderr 에 쓰고 종료 코드 1.
창 없는 윈도우 실행 파일이 메시지 창을 띄워 숨은 하위 프로세스가 멈추지 않게 (probe_whisper_windows 가 기대하는 형식)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 묶은 앱처럼 보이게 (sys.frozen, _MEIPASS) 한 뒤 launcher 를 __main__ 으로 실행
PRELUDE = f"""
import runpy, sys, types
sys.frozen = True
sys._MEIPASS = {str(ROOT)!r}
sys.path.insert(0, {str(ROOT)!r})
"""


def _run(body: str, argv: list[str]) -> subprocess.CompletedProcess:
    code = PRELUDE + body + f"""
sys.argv = {["youtube-edit"] + argv!r}
runpy.run_path({str(ROOT / "packaging" / "launcher.py")!r}, run_name="__main__")
"""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT)


def test_dash_c_exception_exits_1_with_traceback():
    r = _run("", ["-c", "raise ValueError('boom')"])
    assert r.returncode == 1
    assert "Traceback" in r.stderr and "ValueError: boom" in r.stderr


def test_dash_c_sys_exit_kept():
    r = _run("", ["-c", "import sys; sys.exit(3)"])
    assert r.returncode == 3 and "Traceback" not in r.stderr


def test_pipeline_exception_exits_1_with_traceback():
    fake = """
fake = types.ModuleType("pipeline")
def _boom():
    raise RuntimeError("pipeline crashed")
fake.main = _boom
sys.modules["pipeline"] = fake
"""
    r = _run(fake, ["--pipeline", "in.mp4"])
    assert r.returncode == 1
    assert "Traceback" in r.stderr and "RuntimeError: pipeline crashed" in r.stderr
