"""작업 대기열. 작업 스레드 하나가 분석·렌더를 차례로 돌리고, 저장(렌더)은 대기 중인 분석보다 앞에 선다.

재판정(rejudge)은 대기열을 거치지 않고 따로 짧은 스레드에서 바로 돈다 — 전사가 캐시돼 있어 몇 초면
끝나므로 다른 영상의 긴 분석 뒤에서 기다리게 하지 않는다.

execute(job, kind, on_start) -> RunResult
    kind 는 "analyze" | "render". 실행을 시작하면 on_start(handle) 을 한 번 부르고, handle.cancel() 로
    멈출 수 있어야 한다 (runner.run(..., on_start=on_start) 를 그대로 넘기면 된다). 취소되면
    RunResult(False, "cancelled") 를 돌려준다.
"""
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
        with self.lock:
            queued = {jid for jid, _ in self._items}
            for job in self.store.jobs:
                if job.status == Status.WAITING and job.id not in queued and job.id != self._current:
                    self._items.append((job.id, ANALYZE))
            self._wake()

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
        self.on_change(job)

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
            self._prepare_rejudge(job)
            t = threading.Thread(target=self._rejudge_loop, args=(job_id,), daemon=True,
                                 name=f"rejudge-{job_id}")
            t.start()
        self.on_change(job)

    def cancel(self, job_id: str) -> None:
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
                self.store.save()
                self._cv.notify_all()
        if running:
            if handle:
                handle.cancel()
        else:
            self.on_change(job)

    def set_progress(self, job_id: str, value: float) -> None:
        """진행률만 갱신 (저장하지 않음). runner 의 on_progress 에 연결한다."""
        with self.lock:
            job = self.store.get(job_id)
            if not job:
                return
            job.progress = value
        self.on_change(job)

    def join(self, timeout: float | None = None) -> bool:
        """대기열이 비고 실행 중인 작업·재판정이 모두 끝날 때까지 기다린다."""
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._items and self._current is None and not self._rejudging, timeout)

    # ---- 내부 --------------------------------------------------------------

    def _wake(self) -> None:
        if self._items and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._work, daemon=True, name="jobqueue")
            self._worker.start()
        self._cv.notify_all()

    def _set(self, job: Job, status: str, **fields) -> None:
        job.status = status
        for k, v in fields.items():
            setattr(job, k, v)
        self.store.save()

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
                if kind == ANALYZE:
                    self._set(job, Status.PROCESSING, progress=0.0, error="")
            if kind == ANALYZE:
                self.on_change(job)
            self._run(job, kind)

    def _run(self, job: Job, kind: str) -> None:
        try:
            result = self.execute(job, kind, self._on_start(job.id))
        except Exception as e:  # noqa: BLE001 — 작업 스레드는 죽지 않는다
            result = RunResult(False, f"처리 중 오류가 났습니다: {e}")
        with self.lock:
            self._finish(job, kind, result)
            if self._current == job.id:
                self._current = None
            self._cv.notify_all()
        self.on_change(job)

    def _finish(self, job: Job, kind: str, result: RunResult) -> None:
        """lock 안에서 부른다."""
        self._handles.pop(job.id, None)
        if job.id in self._cancel or (not result.ok and result.error == CANCELLED):
            self._cancel.discard(job.id)
            self.store.remove(job.id)
            self.store.save()
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

    def _prepare_rejudge(self, job: Job) -> None:
        """lock 안에서 부른다. 현재 cuts.json 을 prev.cuts.json 으로 복사하고 처리 중으로 바꾼다."""
        out = _out_dir(job)
        cur = out / f"{Path(job.src).stem}.cuts.json"
        prev = out / "prev.cuts.json"
        try:
            shutil.copyfile(cur, prev)
            job.prev_cuts = str(prev)
        except OSError:
            pass
        self._set(job, Status.PROCESSING, progress=0.0, error="")

    def _rejudge_loop(self, job_id: str) -> None:
        job = self.store.get(job_id)
        while True:
            try:
                result = self.execute(job, ANALYZE, self._on_start(job_id))
            except Exception as e:  # noqa: BLE001
                result = RunResult(False, f"처리 중 오류가 났습니다: {e}")
            with self.lock:
                again = self._rejudging.get(job_id, False) and result.ok and job_id not in self._cancel
                if again:
                    self._handles.pop(job_id, None)
                    self._rejudging[job_id] = False
                    self._prepare_rejudge(job)
                else:
                    self._finish(job, ANALYZE, result)
                    self._rejudging.pop(job_id, None)
                    self._cv.notify_all()
            self.on_change(job)
            if not again:
                return
