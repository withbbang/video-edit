"""화면(JS) ↔ 파이썬 다리. pywebview 의 js_api 로 넘기는 Api 와, 대기열이 쓰는 execute 를 만든다.

화면이 부르는 메서드는 모두 JSON 으로 바꿀 수 있는 값만 돌려준다. 창으로 보내는 이벤트는 push(event, data):
  "job"     Job 화면용 dict (job_view) — 대기열의 on_change 에 make_on_change(push) 를 넘긴다
  "models"  {"done", "total"} 진행 · {"ready": True} 완료 · {"error": True} 실패
  "update"  {"error": "..."} 설치 파일 받기/열기 실패 · {"opened": True} 열기 성공
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import __version__, diff, models, paths, runner, updates
from .jobqueue import ANALYZE, RENDER
from .options import FAST_KEYS, JobOptions
from .progress import ANALYZE_WEIGHTS, RENDER_WEIGHTS
from .runner import RunResult
from .store import Job, Status, Store

log = logging.getLogger(__name__)

MEDIA_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m4a", ".mp3", ".wav"}
ALLOWED = {
    "filler": ("keep", "remove"),
    "habit": ("keep", "some", "all"),
    "silence": ("keep", "remove"),
    "denoise": ("keep", "remove"),
    "loudnorm": ("keep", "match"),
    "subs": ("omit", "embed"),
    "quality": ("low", "auto", "high"),
}
PROGRESS_INTERVAL = 0.2  # 작업·모델 진행 이벤트는 초당 5번까지

Push = Callable[[str, dict], None]


class Dialogs(Protocol):
    def open_files(self) -> list[str] | None: ...
    def choose_folder(self) -> str | None: ...


def job_view(job: Job) -> dict:
    return {"id": job.id, "name": Path(job.src).name, "status": job.status,
            "progress": job.progress, "error": job.error, "summary": job.summary}


def make_on_change(push: Push) -> Callable[[Job], None]:
    """Queue(on_change=...) 에 넘긴다."""
    return lambda job: push("job", job_view(job))


class _Throttle:
    def __init__(self, interval: float = PROGRESS_INTERVAL):
        self.interval, self.last = interval, None

    def ready(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or self.last is None or now - self.last >= self.interval:
            self.last = now
            return True
        return False


# ---------------------------------------------------------------------------
# 실행 (Queue 의 execute)
# ---------------------------------------------------------------------------
def pipeline_command(job: Job, kind: str, store: Store, manifest: dict, data_root: Path
                     ) -> tuple[list[str], dict, dict]:
    """(명령, 진행 가중치, 더할 환경변수). 필요한 폴더를 만든다."""
    model = models.paths(manifest, data_root)
    device = models.device(store.settings, manifest)
    jd = paths.job_dir(Path(job.src))
    if kind == ANALYZE:
        (jd / "out").mkdir(parents=True, exist_ok=True)
        (jd / "work").mkdir(parents=True, exist_ok=True)
        cmd, weights = runner.analyze_command(job, model, device), ANALYZE_WEIGHTS
    elif kind == RENDER:
        save_dir = Path(store.settings["save_dir"])
        save_dir.mkdir(parents=True, exist_ok=True)
        cmd, weights = runner.render_command(job, model, device, save_dir), RENDER_WEIGHTS
    else:
        raise ValueError(f"알 수 없는 작업 종류: {kind}")
    if device == "cuda":
        cmd += ["--compute-type", models.CUDA_COMPUTE_TYPE]
    return cmd, weights, models.pipeline_env(manifest, data_root, store.settings)


def make_execute(store: Store, manifest: dict, data_root: Path, queue_getter: Callable[[], object]):
    """Queue(execute=...) 에 넘긴다. queue_getter 는 Queue 를 만든 뒤에 돌려줄 수 있게 함수로 받는다.
    진행률은 queue.set_progress 로 넘기고(그게 on_change → push("job") 로 이어진다) 작업마다 초당 5번까지만."""
    def execute(job: Job, kind: str, on_start) -> RunResult:
        cmd, weights, env = pipeline_command(job, kind, store, manifest, data_root)
        throttle = _Throttle()

        def on_progress(p: float) -> None:
            if throttle.ready():
                queue_getter().set_progress(job.id, p)

        return runner.run(cmd, weights, on_progress, env=env, on_start=on_start)
    return execute


# ---------------------------------------------------------------------------
# 화면 API
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, store: Store, queue, push: Push, dialogs: Dialogs, *,
                 manifest: dict, data_root: Path, preview_base: str):
        self.store = store
        self.queue = queue
        self.push = push
        self.dialogs = dialogs
        self.manifest = manifest
        self.data_root = Path(data_root)
        self.preview_base = preview_base.rstrip("/")
        self._bg = threading.Lock()                 # 내려받기 스레드 시작 판정
        self._download_thread: threading.Thread | None = None
        self._update_thread: threading.Thread | None = None
        self._update: dict | None = None            # 마지막으로 찾은 새 버전

    # ---- 내부 ----------------------------------------------------------------

    def _job(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError(f"없는 영상: {job_id}")
        return job

    def _save(self) -> None:
        with self.queue.lock:
            self.store.save()

    # ---- 목록 ----------------------------------------------------------------

    def list_jobs(self) -> list[dict]:
        with self.queue.lock:
            return [job_view(j) for j in self.store.jobs]

    def add_files(self, paths_: list[str]) -> list[dict]:
        added = False
        with self.queue.lock:
            for raw in paths_ or []:
                p = Path(os.path.abspath(str(raw)))
                if p.suffix.lower() not in MEDIA_EXTS or not p.is_file():
                    continue
                if self.store.add(p):
                    added = True
            if added:
                self.store.save()
        return self.list_jobs()

    def pick_files(self) -> list[dict]:
        files = self.dialogs.open_files()
        return self.add_files(list(files)) if files else self.list_jobs()

    def remove_job(self, job_id: str) -> list[dict]:
        """대기 중이면 빼고, 처리·저장 중이면 멈추고 뺀다 (확인은 화면이 먼저 받는다)."""
        self.queue.cancel(job_id)
        return self.list_jobs()

    def start(self) -> list[dict]:
        self.queue.start_all()
        return self.list_jobs()

    def save(self, job_id: str) -> dict:
        self.queue.request_save(job_id)
        with self.queue.lock:
            job = self._job(job_id)
            if job.status == Status.SAVING and job.prev_cuts:
                job.prev_cuts = None   # "달라진 곳" 은 저장 때 초기화 (스펙 3.3)
                self.store.save()
            return job_view(job)

    # ---- 옵션 ----------------------------------------------------------------

    def get_options(self, job_id: str) -> dict:
        with self.queue.lock:
            return self._job(job_id).options.to_dict()

    def set_option(self, job_id: str, key: str, value: str) -> dict:
        """{"options": 지금 옵션, "accepted": 반영 여부}. 처리·저장 중엔 바꾸지 않는다 (화면이 막는다)."""
        if key not in ALLOWED or value not in ALLOWED[key]:
            raise ValueError(f"잘못된 옵션: {key}={value!r}")
        rejudge = changed_status = False
        with self.queue.lock:
            job = self._job(job_id)
            if job.status in (Status.PROCESSING, Status.SAVING):
                return {"options": job.options.to_dict(), "accepted": False}
            if getattr(job.options, key) != value:
                setattr(job.options, key, value)
                if key in FAST_KEYS:
                    rejudge = job.status in (Status.REVIEW, Status.SAVED)
                elif job.status == Status.SAVED:
                    job.status = Status.REVIEW
                    changed_status = True
                self.store.save()
            opts = job.options.to_dict()
            view = job_view(job)
        if rejudge:
            self.queue.rejudge(job_id)   # 상태 변화는 대기열이 알린다
        elif changed_status:
            self.push("job", view)
        return {"options": opts, "accepted": True}

    # ---- 결과 ----------------------------------------------------------------

    def get_result(self, job_id: str) -> dict:
        with self.queue.lock:
            job = self._job(job_id)
            src, summary, prev, status = Path(job.src), job.summary, job.prev_cuts, job.status
            save_dir = Path(self.store.settings["save_dir"])
        out = paths.job_dir(src) / "out"
        cur = _read_json(out / f"{src.stem}.cuts.json")
        transcript = _read_json(out / f"{src.stem}.transcript.json")
        changes = None
        if prev and cur is not None:
            old = _read_json(Path(prev))
            if old is not None:
                changes = diff.compare(old, cur, transcript)
        duration = 0.0
        try:
            duration = float((transcript or {}).get("duration") or (summary or {}).get("orig_sec") or 0)
        except (TypeError, ValueError):
            pass
        result = {"summary": summary, "cuts": diff.cut_ranges(cur) if cur else [], "changes": changes,
                  "preview_url": f"{self.preview_base}/{job_id}/preview.mp4", "duration": duration}
        if status == Status.SAVED:
            names = [f"{src.stem}_clean.mp4", f"{src.stem}_clean.m4a", f"{src.stem}_clean.srt"]
            result["saved_paths"] = [str(save_dir / n) for n in names if (save_dir / n).is_file()]
        return result

    # ---- 설정 ----------------------------------------------------------------

    def get_settings(self) -> dict:
        with self.queue.lock:
            return {"save_dir": self.store.settings["save_dir"]}

    def set_save_dir(self) -> str:
        folder = self.dialogs.choose_folder()
        with self.queue.lock:
            if folder:
                self.store.settings["save_dir"] = os.path.abspath(str(folder))
                self.store.save()
            return self.store.settings["save_dir"]

    def get_version(self) -> str:
        return __version__

    # ---- 모델 ----------------------------------------------------------------

    def _downloading(self) -> bool:
        t = self._download_thread
        return bool(t and t.is_alive())

    def model_status(self) -> dict:
        missing = [m["key"] for m in models.status(self.manifest, self.data_root)]
        return {"ready": not missing, "missing": missing, "downloading": self._downloading()}

    def download_models(self) -> dict:
        """스레드에서 모델(과 윈도우 GPU)을 준비한다. 이미 받는 중이면 {"started": False}."""
        with self._bg:
            if self._downloading():
                return {"started": False}
            self._download_thread = threading.Thread(target=self._download_models, daemon=True,
                                                     name="models-download")
            self._download_thread.start()
        return {"started": True}

    def _download_models(self) -> None:
        throttle = _Throttle()
        settings = dict(self.store.settings)

        def on_progress(done: int, total: int) -> None:
            if throttle.ready(force=done >= total):
                self.push("models", {"done": done, "total": total})

        try:
            models.prepare(self.manifest, self.data_root, settings, on_progress)
        except Exception:  # noqa: BLE001 — DownloadError 말고도 화면엔 다시 시도로 보여 준다
            log.exception("모델 준비 실패")
            self.push("models", {"error": True})
            return
        try:
            with self.queue.lock:
                self.store.settings["gpu"] = settings.get("gpu")
                self.store.save()
        except Exception:  # noqa: BLE001
            log.exception("설정 저장 실패")
        self.push("models", {"ready": True})

    # ---- 업데이트 ------------------------------------------------------------

    def check_update(self) -> dict | None:
        info = updates.check(__version__)
        self._update = info
        with self.queue.lock:
            dismissed = self.store.settings.get("dismissed_version")
        if not info or info["version"] == dismissed:
            return None
        return info

    def dismiss_update(self, version: str) -> None:
        with self.queue.lock:
            self.store.settings["dismissed_version"] = version
            self.store.save()

    def install_update(self) -> dict:
        """마지막으로 찾은 새 버전 설치 파일을 받아 연다 (스레드). 실패는 push("update", {"error"})."""
        info = self._update or updates.check(__version__)
        if not info:
            return {"started": False}
        with self._bg:
            t = self._update_thread
            if t and t.is_alive():
                return {"started": False}
            self._update_thread = threading.Thread(target=self._install_update, args=(info["url"],),
                                                   daemon=True, name="update-download")
            self._update_thread.start()
        return {"started": True}

    def _install_update(self, url: str) -> None:
        try:
            path = updates.download(url, paths.data_dir() / "updates")
            updates.open_file(path)
        except Exception as e:  # noqa: BLE001
            log.exception("업데이트 설치 파일 받기 실패")
            self.push("update", {"error": str(e) or type(e).__name__})
            return
        self.push("update", {"opened": True})


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
