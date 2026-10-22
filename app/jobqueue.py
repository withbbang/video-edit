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
        self._stopping = False                          # shutdown() 뒤엔 아무것도 새로 시작하지 않는다

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

    def request_save(self, job_id: str, notify: bool = True) -> None:
        """notify=False 면 on_change 를 부르지 않는다 — 락을 쥔 채 부르는 쪽이 락을 놓은 뒤 직접 알린다."""
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
        if notify:
            self._notify(job)

    def rejudge(self, job_id: str) -> None:
        with self.lock:
            job = self.store.get(job_id)
            if not job or self._stopping:
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

    def shutdown(self, timeout: float = 3.0) -> bool:
        """앱을 닫을 때. 대기열을 비우고 실행 중인 분석·렌더·재판정을 멈춘다. cancel() 과 달리 목록에서 빼지 않고
        멈춘 영상은 대기(waiting)로 되돌려 저장한다 (다음 실행 때 다시 처리). 모두 멈췄으면 True."""
        with self.lock:
            self._stopping = True
            self._items.clear()
            running = [jid for jid in ([self._current] if self._current else []) + list(self._rejudging)]
            handles = []
            for jid in running:
                self._cancel.add(jid)          # 아직 on_start 전이면 on_start 가 멈춘다
                if jid in self._rejudging:
                    self._rejudging[jid] = False
                if jid in self._handles:
                    handles.append(self._handles[jid])
            self._cv.notify_all()
        for h in handles:
            try:
                h.cancel()
            except Exception:  # noqa: BLE001
                log.exception("멈추기 실패")
        with self._cv:
            done = self._cv.wait_for(lambda: self._current is None and not self._rejudging, timeout)
            for jid in running:      # 시간 안에 안 끝난 것도 대기로 저장해 둔다
                job = self.store.get(jid)
                if job and job.status in (Status.PROCESSING, Status.SAVING):
                    job.status, job.progress = Status.WAITING, 0.0
            self._save()
        return done

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
        if self._stopping:
            self._items.clear()
            self._cv.notify_all()
            return
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
                # 결과 반영과 상태 정리를 한 번의 락 안에서 끝내고 나서 알린다 — 알림을 받은 UI 가
                # 곧바로 rejudge/cancel/request_save 를 불러도 정상 동작하도록.
                with self.lock:
                    self._finish_safe(job, kind, result)
                    self._release_worker(job_id)
                self._notify(job)
            except Exception:  # noqa: BLE001 — 다음 항목은 계속 돌린다
                log.exception("작업 처리 중 예외: %s %s", job_id, kind)
            finally:
                with self.lock:
                    if self._current == job_id:   # 안전망: 위에서 정리하지 못한 경우
                        self._fail_if_running(job, "처리 중 예기치 못한 오류가 났습니다")
                        self._release_worker(job_id)

    def _release_worker(self, job_id: str) -> None:
        """lock 안에서 부른다."""
        self._handles.pop(job_id, None)
        self._cancel.discard(job_id)
        if self._current == job_id:
            self._current = None
        self._cv.notify_all()

    def _release_rejudge(self, job_id: str) -> None:
        """lock 안에서 부른다."""
        self._rejudging.pop(job_id, None)
        self._handles.pop(job_id, None)
        self._cancel.discard(job_id)
        self._cv.notify_all()

    def _fail_if_running(self, job: Job, msg: str) -> None:
        """lock 안에서 부른다. 처리 중으로 남은 영상을 실패로 돌린다."""
        if job.status in (Status.PROCESSING, Status.SAVING) and self.store.get(job.id) is job:
            job.status, job.error = Status.FAILED, msg
            self._save()

    def _finish_safe(self, job: Job, kind: str, result: RunResult) -> None:
        """lock 안에서 부른다. _finish 가 터져도 영상을 처리 중으로 남기지 않는다."""
        try:
            self._finish(job, kind, result)
        except Exception as e:  # noqa: BLE001
            log.exception("결과 반영 실패: %s %s", job.id, kind)
            job.status, job.error = Status.FAILED, f"처리 결과를 정리하지 못했습니다: {e}"
            self._save()

    def _execute(self, job: Job, kind: str) -> RunResult:
        try:
            return self.execute(job, kind, self._on_start(job.id))
        except Exception as e:  # noqa: BLE001 — 작업 스레드는 죽지 않는다
            log.exception("실행 실패: %s %s", job.id, kind)
            return RunResult(False, f"처리 중 오류가 났습니다: {e}")

    def _finish(self, job: Job, kind: str, result: RunResult) -> None:
        """lock 안에서 부른다."""
        self._handles.pop(job.id, None)
        if self._stopping:
            self._cancel.discard(job.id)
            if job.status in (Status.PROCESSING, Status.SAVING):
                self._set(job, Status.WAITING, progress=0.0)
            return
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
        released = False
        try:
            while True:
                result = self._execute(job, ANALYZE)
                with self.lock:
                    again = (self._rejudging.get(job_id, False) and result.ok
                             and job_id not in self._cancel)
                    if again:
                        self._handles.pop(job_id, None)
                        self._rejudging[job_id] = False
                        try:
                            self._prepare_rejudge(job, copy_prev=False)
                        except Exception as e:  # noqa: BLE001
                            log.exception("재판정 준비 실패: %s", job_id)
                            job.status, job.error = Status.FAILED, f"다시 판정하지 못했습니다: {e}"
                            self._save()
                            again = False
                            self._release_rejudge(job_id)
                            released = True
                    else:
                        # 결과 반영과 상태 정리를 한 번의 락 안에서 끝내고 나서 알린다
                        self._finish_safe(job, ANALYZE, result)
                        self._release_rejudge(job_id)
                        released = True
                self._notify(job)
                if not again:
                    return
        except Exception:  # noqa: BLE001
            log.exception("재판정 처리 중 예외: %s", job_id)
        finally:
            with self.lock:
                # 안전망. 이미 정리했다면 알림을 받은 UI 가 새로 시작한 재판정일 수 있으니 건드리지 않는다
                if not released:
                    self._fail_if_running(job, "다시 판정하는 중 예기치 못한 오류가 났습니다")
                    self._release_rejudge(job_id)
