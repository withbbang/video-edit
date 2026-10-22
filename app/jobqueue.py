"""작업 대기열. 작업 스레드 하나가 분석·렌더를 차례로 돌리고, 저장(렌더)은 대기 중인 분석보다 앞에 선다.

재판정(rejudge)은 대기열을 거치지 않고 따로 짧은 스레드에서 바로 돈다 — 전사가 캐시돼 있어 몇 초면
끝나므로 다른 영상의 긴 분석 뒤에서 기다리게 하지 않는다.

execute(job, kind, on_start) -> RunResult
    kind 는 "analyze" | "render". 실행을 시작하면 on_start(handle) 을 한 번 부르고, handle.cancel() 로
    멈출 수 있어야 한다 (runner.run(..., on_start=on_start) 를 그대로 넘기면 된다). 취소되면
    RunResult(False, "cancelled") 를 돌려준다.
"""
import logging
import shutil
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Protocol

from . import paths, runner
from .runner import RunResult
from .store import Job, Status, Store

ANALYZE = "analyze"
RENDER = "render"
CANCELLED = "cancelled"

log = logging.getLogger(__name__)


class Cancellable(Protocol):
    def cancel(self) -> None: ...


Execute = Callable[[Job, str, Callable[[Cancellable], None]], RunResult]


def _out_dir(job: Job) -> Path:
    return paths.job_dir(Path(job.src)) / "out"


class Queue:
    def __init__(self, store: Store, execute: Execute, on_change: Callable[[Job], None],
                 read_summary: Callable[[Job], dict] = runner.read_summary):
        self.store = store
        self.execute = execute
        self.on_change = on_change
        self.read_summary = read_summary
        self.lock = threading.RLock()
        self._cv = threading.Condition(self.lock)
        self._items: deque[tuple[str, str]] = deque()   # (job_id, kind)
        self._current: str | None = None                # 작업 스레드가 돌리는 job_id
        self._rejudging: dict[str, bool] = {}           # job_id -> 후속 재판정 예약 여부
        self._handles: dict[str, Cancellable] = {}
        self._cancel: set[str] = set()
        self._worker: threading.Thread | None = None

    # ---- 공개 API ----------------------------------------------------------

    def start_all(self) -> None:
        """대기(waiting)·실패(failed) 영상을 목록 순서로 분석 대기열에 넣는다. 실패한 것은 대기로 되돌린다."""
        retried = []
        with self.lock:
            queued = {jid for jid, _ in self._items}
            for job in self.store.jobs:
                if job.id in queued or job.id == self._current or job.id in self._rejudging:
                    continue
                if job.status == Status.FAILED:
                    job.status, job.error, job.progress = Status.WAITING, "", 0.0
                    retried.append(job)
                if job.status == Status.WAITING:
                    self._items.append((job.id, ANALYZE))
            if retried:
                self._save()
            self._wake()
        for job in retried:
            self._notify(job)

    def request_save(self, job_id: str) -> None:
        with self.lock:
            job = self.store.get(job_id)
            if not job or job.status not in (Status.REVIEW, Status.SAVED) or job_id in self._rejudging:
                return
            pos = 0
            while pos < len(self._items) and self._items[pos][1] == RENDER:
                pos += 1
            self._items.insert(pos, (job_id, RENDER))
            self._set(job, Status.SAVING, progress=0.0, error="")
            self._wake()
        self._notify(job)

    def rejudge(self, job_id: str) -> None:
        with self.lock:
            job = self.store.get(job_id)
            if not job:
                return
            if job_id in self._rejudging:
                self._rejudging[job_id] = True   # 후속 한 번만; 옵션은 실행 시점 것을 읽음
                return
            if job.status not in (Status.REVIEW, Status.SAVED) or job_id == self._current:
                return
            self._rejudging[job_id] = False
            try:
                self._prepare_rejudge(job, copy_prev=True)
                threading.Thread(target=self._rejudge_loop, args=(job_id,), daemon=True,
                                 name=f"rejudge-{job_id}").start()
            except BaseException:
                self._rejudging.pop(job_id, None)
                self._cv.notify_all()
                raise
        self._notify(job)

    def cancel(self, job_id: str) -> None:
        handle = None
        with self.lock:
            job = self.store.get(job_id)
            if not job:
                return
            running = job_id == self._current or job_id in self._rejudging
            if running:
                self._cancel.add(job_id)
                if job_id in self._rejudging:
                    self._rejudging[job_id] = False
                handle = self._handles.get(job_id)
            else:
                self._items = deque(i for i in self._items if i[0] != job_id)
                self.store.remove(job_id)
                self._save()
                self._cv.notify_all()
        if running:
            if handle:
                handle.cancel()
        else:
            self._notify(job)

    def set_progress(self, job_id: str, value: float) -> None:
        """진행률만 갱신 (저장하지 않음). runner 의 on_progress 에 연결한다."""
        with self.lock:
            job = self.store.get(job_id)
            if not job:
                return
            job.progress = value
        self._notify(job)

    def join(self, timeout: float | None = None) -> bool:
        """대기열이 비고 실행 중인 작업·재판정이 모두 끝날 때까지 기다린다."""
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._items and self._current is None and not self._rejudging, timeout)

    # ---- 내부 --------------------------------------------------------------

    def _notify(self, job: Job) -> None:
        try:
            self.on_change(job)
        except Exception:  # noqa: BLE001 — UI 콜백 오류로 작업 스레드가 멈추면 안 된다
            log.exception("on_change 실패: %s", job.id)

    def _save(self) -> None:
        try:
            self.store.save()
        except Exception:  # noqa: BLE001 — 저장 실패로 상태 정리가 막히면 안 된다
            log.exception("작업 목록 저장 실패")

    def _wake(self) -> None:
        if self._items and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._work, daemon=True, name="jobqueue")
            self._worker.start()
        self._cv.notify_all()

    def _set(self, job: Job, status: str, **fields) -> None:
        job.status = status
        for k, v in fields.items():
            setattr(job, k, v)
        self._save()

    def _on_start(self, job_id: str) -> Callable[[Cancellable], None]:
        def on_start(handle: Cancellable) -> None:
            with self.lock:
                self._handles[job_id] = handle
                pending = job_id in self._cancel
            if pending:
                handle.cancel()
        return on_start

    def _work(self) -> None:
        while True:
            with self.lock:
                if not self._items:
                    self._worker = None
                    self._cv.notify_all()
                    return
                job_id, kind = self._items.popleft()
                job = self.store.get(job_id)
                if not job:
                    continue
                self._current = job_id
            try:
                if kind == ANALYZE:
                    with self.lock:
                        self._set(job, Status.PROCESSING, progress=0.0, error="")
                    self._notify(job)
                result = self._execute(job, kind)
                with self.lock:
                    self._finish(job, kind, result)
            except Exception:  # noqa: BLE001 — 다음 항목은 계속 돌린다
                log.exception("작업 처리 중 예외: %s %s", job_id, kind)
            finally:
                with self.lock:
                    self._handles.pop(job_id, None)
                    self._cancel.discard(job_id)
                    if self._current == job_id:
                        self._current = None
                    self._cv.notify_all()
            self._notify(job)

    def _execute(self, job: Job, kind: str) -> RunResult:
        try:
            return self.execute(job, kind, self._on_start(job.id))
        except Exception as e:  # noqa: BLE001 — 작업 스레드는 죽지 않는다
            log.exception("실행 실패: %s %s", job.id, kind)
            return RunResult(False, f"처리 중 오류가 났습니다: {e}")

    def _finish(self, job: Job, kind: str, result: RunResult) -> None:
        """lock 안에서 부른다."""
        self._handles.pop(job.id, None)
        if job.id in self._cancel or (not result.ok and result.error == CANCELLED):
            self._cancel.discard(job.id)
            self.store.remove(job.id)
            self._save()
            return
        if not result.ok:
            self._set(job, Status.FAILED, error=result.error or "처리에 실패했습니다")
        elif kind == RENDER:
            self._set(job, Status.SAVED, progress=1.0, error="")
        else:
            try:
                summary = self.read_summary(job)
            except Exception as e:  # noqa: BLE001
                self._set(job, Status.FAILED, error=f"분석 결과를 읽지 못했습니다: {e}")
                return
            self._set(job, Status.REVIEW, progress=1.0, error="", summary=summary)

    def _prepare_rejudge(self, job: Job, copy_prev: bool) -> None:
        """lock 안에서 부른다. 재판정 묶음의 첫 실행만 현재 cuts.json 을 prev.cuts.json 으로 복사한다
        (후속 실행은 사용자가 마지막으로 본 결과를 비교 기준으로 남기려고 덮어쓰지 않는다)."""
        if copy_prev:
            out = _out_dir(job)
            prev = out / "prev.cuts.json"
            try:
                shutil.copyfile(out / f"{Path(job.src).stem}.cuts.json", prev)
                job.prev_cuts = str(prev)
            except OSError:
                pass
        self._set(job, Status.PROCESSING, progress=0.0, error="")

    def _rejudge_loop(self, job_id: str) -> None:
        job = self.store.get(job_id)
        try:
            while True:
                result = self._execute(job, ANALYZE)
                with self.lock:
                    again = (self._rejudging.get(job_id, False) and result.ok
                             and job_id not in self._cancel)
                    if again:
                        self._handles.pop(job_id, None)
                        self._rejudging[job_id] = False
                        self._prepare_rejudge(job, copy_prev=False)
                    else:
                        self._finish(job, ANALYZE, result)
                self._notify(job)
                if not again:
                    return
        except Exception:  # noqa: BLE001
            log.exception("재판정 처리 중 예외: %s", job_id)
        finally:
            with self.lock:
                self._rejudging.pop(job_id, None)
                self._handles.pop(job_id, None)
                self._cancel.discard(job_id)
                self._cv.notify_all()
