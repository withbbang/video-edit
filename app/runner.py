"""파이프라인을 하위 프로세스로 실행하고 진행률·오류를 읽는다."""
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths
from .options import analysis_args, render_args
from .paths import ModelPaths
from .progress import Tracker
from .store import Job

IS_WIN = sys.platform == "win32"


@dataclass
class RunResult:
    ok: bool
    error: str = ""


def pipeline_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--pipeline"]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" / "pipeline.py")]


def _tail(model: ModelPaths, device: str) -> list[str]:
    args = ["--model", model.whisper, "--ctc-model", model.ctc, "--device", device]
    fillers = paths.data_dir() / "fillers.json"
    if fillers.is_file():
        args += ["--fillers", str(fillers)]
    return args


def analyze_command(job: Job, model: ModelPaths, device: str) -> list[str]:
    jd = paths.job_dir(Path(job.src))
    return (pipeline_cmd() + [job.src, "--review", "--progress-json",
                              "--out", str(jd / "out"), "--work-root", str(jd / "work"),
                              "--preview", str(jd / "preview.mp4")]
            + _tail(model, device) + analysis_args(job.options))


def render_command(job: Job, model: ModelPaths, device: str, media_dir: Path,
                   cuts_path: Path | None = None) -> list[str]:
    """media_dir 은 결과 영상·SRT 를 쓸 임시 폴더 (성공하면 api 가 저장 폴더로 옮긴다).
    cuts_path 는 되살린 곳을 뺀 컷 목록 (없으면 판정 결과 그대로)."""
    src = Path(job.src)
    jd = paths.job_dir(src)
    cuts_path = cuts_path or jd / "out" / f"{src.stem}.cuts.json"
    return (pipeline_cmd() + [job.src, "--apply-cuts", str(cuts_path),
                              "--progress-json", "--out", str(jd / "out"),
                              "--work-root", str(jd / "work"), "--media-out", str(media_dir)]
            + _tail(model, device) + render_args(job.options))


def build_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    bin_dir = paths.ffmpeg_bin_dir()
    if bin_dir:
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["HF_HUB_OFFLINE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


class Running:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        if self.proc.poll() is not None:
            return
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def run(cmd: list[str], weights: dict, on_progress: Callable[[float], None],
        env: dict | None = None, on_start: Callable[[Running], None] | None = None) -> RunResult:
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace",
                                env=build_env(env), **kw)
    except OSError as e:
        return RunResult(False, f"처리 프로그램을 시작하지 못했습니다: {e}")
    handle = Running(proc)
    if on_start:
        on_start(handle)
    tracker = Tracker(weights)
    lines: list[str] = []
    last = -1.0
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        p = tracker.feed(line)
        if p is not None:
            if p != last:
                last = p
                on_progress(p)
        else:
            lines.append(line)
            del lines[:-200]
    code = proc.wait()
    if handle.cancelled:
        return RunResult(False, "cancelled")
    if code == 0:
        return RunResult(True)
    if code < 0 or code > 255:
        return RunResult(False, f"처리 프로그램이 비정상 종료했습니다 (코드 {code})")
    errs = [l for l in lines if l.startswith("오류:")]
    return RunResult(False, errs[-1] if errs else (lines[-1] if lines else f"처리 프로그램이 실패했습니다 (코드 {code})"))


def read_summary(job: Job) -> dict:
    src = Path(job.src)
    out = paths.job_dir(src) / "out"
    cuts = json.loads((out / f"{src.stem}.cuts.json").read_text(encoding="utf-8")).get("cuts", [])
    removed = sum(float(c["end"]) - float(c["start"]) for c in cuts)
    orig = 0.0
    try:
        tr = json.loads((out / f"{src.stem}.transcript.json").read_text(encoding="utf-8"))
        orig = float(tr.get("duration") or 0)
    except (OSError, ValueError):
        pass
    return {"orig_sec": orig, "final_sec": max(orig - removed, 0.0), "cuts": len(cuts), "removed_sec": removed}
