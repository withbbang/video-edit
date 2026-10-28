import json

import pytest
import sys
import time
from pathlib import Path

from app import paths, runner
from app.paths import ModelPaths
from app.progress import ANALYZE_WEIGHTS
from app.store import Job

FAKE = str(Path(__file__).parent / "fake_pipeline.py")


def fake(*lines, code=0, sleep=None):
    cmd = [sys.executable, FAKE, f"--code={code}"]
    if sleep:
        cmd.append(f"--sleep={sleep}")
    return cmd + list(lines)


def test_runner_progress_and_ok():
    seen = []
    lines = ['@progress {"stage":"audio","frac":1}', '@progress {"stage":"transcribe","frac":0.5}', "끝"]
    r = runner.run(fake(*lines), ANALYZE_WEIGHTS, seen.append)
    assert r.ok and seen == sorted(seen) and len(seen) == 2 and seen[-1] > seen[0]


def test_runner_failure_reports_last_error():
    r = runner.run(fake("오류: 남길 구간이 없습니다 (전부 컷 대상).", "뒷줄", code=1), ANALYZE_WEIGHTS, lambda f: None)
    assert not r.ok and r.error.startswith("오류: 남길 구간이 없습니다")


def test_runner_failure_last_line_without_marker():
    r = runner.run(fake("a", "b", code=1), ANALYZE_WEIGHTS, lambda f: None)
    assert not r.ok and r.error == "b"


def test_runner_crash_code():
    for code in ((3221225477,) if sys.platform == "win32" else (-11,)):  # POSIX 종료 코드는 8비트
        r = runner.run(fake(code=code), ANALYZE_WEIGHTS, lambda f: None)
        assert not r.ok and "비정상 종료" in r.error


def test_runner_cancel():
    box = []
    import threading
    res = []
    t = threading.Thread(target=lambda: res.append(
        runner.run(fake(sleep=30), ANALYZE_WEIGHTS, lambda f: None, on_start=box.append)))
    t.start()
    while not box:
        time.sleep(0.05)
    time.sleep(0.3)
    t0 = time.time()
    box[0].cancel()
    t.join(5)
    assert not t.is_alive() and time.time() - t0 < 2
    assert not res[0].ok and res[0].error == "cancelled"


def _job(tmp_path, name="강의 03 (최종).mov"):
    src = tmp_path / "바탕 화면" / name
    return Job(id="x", src=str(src))


def test_command_unicode_path(tmp_path):
    job = _job(tmp_path)
    cmd = runner.analyze_command(job, ModelPaths("/m/w", "/m/c"), "cpu")
    assert job.src in cmd and "--review" in cmd and "--preview" in cmd
    assert cmd[cmd.index("--preview") + 1] == str(paths.job_dir(Path(job.src)) / "preview")
    assert all(isinstance(c, str) for c in cmd)


def test_render_command(tmp_path):
    job = _job(tmp_path)
    jd = paths.job_dir(Path(job.src))
    cmd = runner.render_command(job, ModelPaths("/m/w", "/m/c"), "cpu", tmp_path / "save")
    assert "--review" not in cmd and "--preview" not in cmd
    assert cmd[cmd.index("--apply-cuts") + 1] == str(jd / "out" / "강의 03 (최종).cuts.json")
    assert cmd[cmd.index("--media-out") + 1] == str(tmp_path / "save")
    assert cmd[cmd.index("--out") + 1] == str(jd / "out")
    assert cmd[cmd.index("--denoise-level") + 1] == "50" and "--render-denoise" not in cmd and "--fillers" not in cmd


def test_fillers_used_when_present(tmp_path):
    (paths.data_dir() / "fillers.json").write_text("{}")
    cmd = runner.analyze_command(_job(tmp_path), ModelPaths("/m/w", "/m/c"), "cpu")
    assert cmd[cmd.index("--fillers") + 1] == str(paths.data_dir() / "fillers.json")


def test_env():
    env = runner.build_env()
    assert env["HF_HUB_OFFLINE"] == "1" and env["PYTHONIOENCODING"] == "utf-8"


def test_read_summary(tmp_path):
    job = _job(tmp_path)
    out = paths.job_dir(Path(job.src)) / "out"
    out.mkdir(parents=True)
    (out / "강의 03 (최종).cuts.json").write_text(json.dumps(
        {"cuts": [{"start": 1.0, "end": 2.5}, {"start": 10, "end": 11}]}))
    (out / "강의 03 (최종).transcript.json").write_text(json.dumps({"duration": 60.0}))
    s = runner.read_summary(job)
    assert s == {"orig_sec": 60.0, "final_sec": 57.5, "cuts": 2, "removed_sec": 2.5}


def test_render_command_passes_edits(tmp_path):
    job = Job(id="j", src=str(tmp_path / "a.mp4"))
    base = runner.render_command(job, ModelPaths("/m/w", "/m/c"), "cpu", tmp_path / "save")
    assert "--subs-edits" not in base
    cmd = runner.render_command(job, ModelPaths("/m/w", "/m/c"), "cpu", tmp_path / "save",
                                edits_path=tmp_path / "e.json")
    assert cmd[cmd.index("--subs-edits") + 1] == str(tmp_path / "e.json")


def test_runner_low_priority_runs():
    res = runner.run([sys.executable, "-c", "print(1)"], {"x": 1.0}, lambda p: None, low_priority=True)
    assert res.ok


@pytest.mark.skipif(runner.IS_WIN, reason="posix 전용")
def test_runner_low_priority_without_preexec_fn(monkeypatch):
    # 스레드가 여럿 도는 앱에서 preexec_fn 은 fork 한 자식을 멈추게 할 수 있다 → 띄운 뒤 우선순위를 낮춘다
    seen, nice = {}, []
    real_popen, real_set = runner.subprocess.Popen, runner.os.setpriority

    def popen(*a, **kw):
        seen.update(kw)
        return real_popen(*a, **kw)

    def setpriority(which, who, prio):
        nice.append((which, who, prio))
        return real_set(which, who, prio)

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.os, "setpriority", setpriority)
    res = runner.run([sys.executable, "-c", "print(1)"], {"x": 1.0}, lambda p: None, low_priority=True)
    assert res.ok and "preexec_fn" not in seen
    assert len(nice) == 1 and nice[0][0] == runner.os.PRIO_PROCESS and nice[0][2] == 10
    nice.clear()
    assert runner.run([sys.executable, "-c", "print(1)"], {"x": 1.0}, lambda p: None).ok and nice == []
