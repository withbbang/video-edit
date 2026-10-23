"""컷을 적용한 미리보기 소리(audio_cut.<key>.m4a + index.json)를 백그라운드에서 하나씩 만든다.

get_result 가 request() 로 부르고, 만들어지면 on_ready(job_id) (api 가 push("preview", {"id"}) 로 잇는다).
실행은 run(cmd) 콜백 하나 — api 가 runner.run 을 감싸 넘기고, 시작한 프로세스는 started(handle) 로 알려 준다
(shutdown() 이 멈출 수 있게).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Callable

from .options import JobOptions
from .runner import RunResult

log = logging.getLogger(__name__)

BUSY_ERROR = "오류: 같은 영상을 이미 처리 중"   # 다른 실행이 잠금을 쥐고 있었다 → 실패로 기억하지 않는다


def cut_key(cuts: list[dict], options: JobOptions) -> str:
    """컷 시간·잡음·음량이 같으면 같은 소리 → 같은 key (sha1 앞 16자리)."""
    data = {"cuts": [[round(float(c["start"]), 6), round(float(c["end"]), 6)] for c in cuts],
            "denoise": options.denoise, "loudnorm": options.loudnorm}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def index_of(out: Path) -> Path:
    return Path(out).with_suffix(".index.json")


def is_ready(out: Path) -> bool:
    """index 는 파이프라인이 맨 마지막에 쓴다 — 둘 다 있으면 다 만들어진 것."""
    return Path(out).is_file() and index_of(out).is_file()


class CutAudio:
    def __init__(self, run: Callable[[list[str]], RunResult], on_ready: Callable[[str], None], keep: int = 3):
        self._run = run
        self._on_ready = on_ready
        self._keep = keep
        self._cv = threading.Condition()
        self._pending: dict[str, tuple] = {}     # job_id → (key, cmd, out, prepare). 같은 job 은 최신 요청만
        self._current: tuple[str, str] | None = None   # 지금 만드는 (job_id, key)
        self._running = None                     # 지금 도는 프로세스 (runner.Running)
        self._failed: set[tuple[str, str]] = set()
        self._held: dict[str, int] = {}           # cancel() 로 막은 job → 막은 횟수 (release 까지 새 요청을 받지 않는다)
        self._cancel_current = False             # 지금 도는 것을 cancel() 이 멈췄다 → 실패로 기억·알림 안 함
        self._thread: threading.Thread | None = None
        self._stopped = False

    def request(self, job_id: str, key: str, cmd: list[str], out: Path,
                prepare: Callable[[], None] | None = None) -> str:
        """"ready" | "pending" | "failed". prepare 는 실행 바로 전에 작업 스레드에서 부른다 (컷 목록 파일 쓰기)."""
        if is_ready(out):
            return "ready"
        with self._cv:
            if self._stopped or (job_id, key) in self._failed:
                return "failed"
            if job_id in self._held:
                return "failed"   # 분석·저장 중 — 기억하지 않으니 끝난 뒤 요청은 다시 만든다
            if self._current == (job_id, key):
                self._pending.pop(job_id, None)   # 도는 것이 최신이면 대기 중인 옛 요청은 필요 없다
                return "pending"
            self._pending[job_id] = (key, list(cmd), Path(out), prepare)
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, daemon=True, name="preview-cut-audio")
                self._thread.start()
            self._cv.notify_all()
        return "pending"

    def started(self, handle) -> None:
        """run 콜백이 프로세스를 띄우면 부른다. 이미 멈추는 중이면 바로 멈춘다."""
        with self._cv:
            self._running = handle
            stop = self._stopped or self._cancel_current
        if stop:
            handle.cancel()

    def cancel(self, job_id: str, timeout: float = 10.0) -> bool:
        """분석·저장 전에 부른다: 그 job 의 대기 요청을 버리고 돌고 있으면 멈춘 뒤 끝날 때까지(최대 timeout 초) 기다린다.
        release(job_id) 까지 그 job 의 새 요청은 받지 않는다 (같은 영상 잠금을 놓고 다투지 않게). 멈췄으면 True."""
        with self._cv:
            self._held[job_id] = self._held.get(job_id, 0) + 1
            self._pending.pop(job_id, None)
            handle = None
            if self._current is not None and self._current[0] == job_id:
                self._cancel_current = True
                handle = self._running
        if handle is not None:
            handle.cancel()
        with self._cv:
            return self._cv.wait_for(lambda: self._current is None or self._current[0] != job_id, timeout)

    def release(self, job_id: str) -> None:
        """cancel() 로 막은 job 을 다시 받는다."""
        with self._cv:
            n = self._held.get(job_id, 0) - 1
            if n > 0:
                self._held[job_id] = n
            else:
                self._held.pop(job_id, None)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """대기·실행 중인 것이 없어질 때까지 기다린다 (테스트용)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._pending and self._current is None, timeout)

    def shutdown(self) -> None:
        """대기 요청을 버리고 돌고 있는 프로세스를 멈춘다. 여러 번 불려도 된다."""
        with self._cv:
            self._stopped = True
            self._pending.clear()
            handle = self._running
            self._cv.notify_all()
        if handle is not None:
            handle.cancel()

    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._pending or self._stopped)
                if self._stopped:
                    return
                job_id = next(iter(self._pending))
                key, cmd, out, prepare = self._pending.pop(job_id)
                self._current = (job_id, key)
                self._cancel_current = False
            res = self._build(cmd, out, prepare)
            with self._cv:
                self._running = None
                stopped = self._stopped or self._cancel_current
                remember = (not stopped and not res.ok and res.error != "cancelled"
                            and not res.error.startswith(BUSY_ERROR))
                if remember:
                    self._failed.add((job_id, key))
            if not stopped:
                if res.ok:
                    self._prune(out)
                if res.ok or remember:
                    # 실패도 알린다 — 화면이 "만드는 중" 을 거둔다. 잠금 충돌은 알리지 않는다(다시 요청이 바로 또 부딪힘).
                    try:
                        self._on_ready(job_id)
                    except Exception:  # noqa: BLE001 — 알림 실패가 작업 스레드를 죽이지 않게
                        log.exception("미리보기 소리 알림 실패")
            with self._cv:
                self._current = None
                self._cancel_current = False
                self._cv.notify_all()
                if self._stopped:
                    return

    def _build(self, cmd: list[str], out: Path, prepare) -> RunResult:
        try:
            if prepare is not None:
                prepare()
            res = self._run(cmd)
        except Exception as e:  # noqa: BLE001
            log.exception("미리보기 소리 만들기 실패")
            return RunResult(False, str(e) or type(e).__name__)
        if res.ok and not is_ready(out):
            return RunResult(False, "미리보기 소리 파일이 없습니다")
        if not res.ok and res.error != "cancelled":
            log.warning("미리보기 소리 만들기 실패: %s", res.error)
        return res

    def _prune(self, out: Path) -> None:
        """같은 폴더의 audio_cut.* 를 최신 keep 개(key 단위, 방금 만든 것 포함)만 남긴다."""
        groups: dict[str, list[Path]] = {}
        for p in out.parent.glob("audio_cut.*"):
            groups.setdefault(p.name.split(".")[1], []).append(p)
        newest = out.name.split(".")[1]

        def mtime(k: str) -> int:
            try:
                return max(p.stat().st_mtime_ns for p in groups[k])
            except OSError:
                return 0

        order = sorted(groups, key=lambda k: (k == newest, mtime(k)), reverse=True)
        for k in order[self._keep:]:
            for p in groups[k]:
                try:
                    p.unlink()
                except OSError:
                    pass
