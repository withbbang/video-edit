"""packaging/launcher.py: 묶은 앱에서 처리 안 된 예외는 traceback 을 stderr 에 쓰고 종료 코드 1.
창 없는 윈도우 실행 파일이 메시지 창을 띄워 숨은 하위 프로세스가 멈추지 않게 (probe_whisper_windows 가 기대하는 형식)."""
import os
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


def _run(body: str, argv: list[str], data_dir: Path | None = None) -> subprocess.CompletedProcess:
    code = PRELUDE + body + f"""
sys.argv = {["youtube-edit"] + argv!r}
runpy.run_path({str(ROOT / "packaging" / "launcher.py")!r}, run_name="__main__")
"""
    env = dict(os.environ)
    if data_dir is not None:
        env["YTEDIT_DATA_DIR"] = str(data_dir)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT,
                          env=env, encoding="utf-8")


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


# 창 모드에서 터지는 경우: app.window.run 을 예외를 던지는 가짜로 바꾼다
FAKE_WINDOW = """
import app
fake = types.ModuleType("app.window")
def _boom():
    raise RuntimeError("창 열기 실패")
fake.run = _boom
app.window = fake
sys.modules["app.window"] = fake
"""


def test_gui_crash_appends_to_crash_log(tmp_path):
    for _ in range(2):
        r = _run(FAKE_WINDOW, [], data_dir=tmp_path)
        assert r.returncode == 1 and "RuntimeError: 창 열기 실패" in r.stderr
    log = (tmp_path / "logs" / "crash.log").read_text(encoding="utf-8")
    assert log.count("Traceback") == 2 and log.count("RuntimeError: 창 열기 실패") == 2
    heads = [l for l in log.splitlines() if l.startswith("=== ")]
    assert len(heads) == 2 and all("youtube-edit" in h and h[4:8].isdigit() for h in heads)


def test_subprocess_modes_do_not_write_crash_log(tmp_path):
    r = _run("", ["-c", "raise ValueError('boom')"], data_dir=tmp_path)
    assert r.returncode == 1
    assert not (tmp_path / "logs" / "crash.log").exists()


def test_crash_log_failure_keeps_exit_code(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("폴더 자리에 파일", encoding="utf-8")  # logs 폴더를 만들 수 없게
    r = _run(FAKE_WINDOW, [], data_dir=tmp_path)
    assert r.returncode == 1 and "RuntimeError: 창 열기 실패" in r.stderr


def test_frozen_uses_bundled_ca_certs():
    """묶은 앱의 파이썬엔 CA 인증서 목록이 없어 HTTPS(모델·업데이트 받기)가 인증서 확인에서 실패한다.
    certifi 의 목록을 SSL_CERT_FILE 로 알려 준다 (사용자가 이미 정했으면 그대로)."""
    import certifi
    code = "import os, ssl; print(os.environ.get('SSL_CERT_FILE')); print(ssl.create_default_context().cert_store_stats()['x509_ca'])"
    old = os.environ.pop("SSL_CERT_FILE", None)
    try:
        r = _run("", ["-c", code])
    finally:
        if old is not None:
            os.environ["SSL_CERT_FILE"] = old
    lines = r.stdout.split()
    assert r.returncode == 0, r.stderr
    assert lines[0] == certifi.where() and int(lines[1]) > 0
    os.environ["SSL_CERT_FILE"] = "/custom.pem"
    try:
        r = _run("", ["-c", "import os; print(os.environ['SSL_CERT_FILE'])"])
    finally:
        os.environ.pop("SSL_CERT_FILE")
        if old is not None:
            os.environ["SSL_CERT_FILE"] = old
    assert r.stdout.strip() == "/custom.pem"
