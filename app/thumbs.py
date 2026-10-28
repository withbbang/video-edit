"""타임라인 썸네일: 미리보기 영상(모든 프레임이 기준 프레임)에서 STEP 초 간격 썸네일을 PER_SHEET 장씩
타일 JPEG(시트)으로 만들어 jobs/<id>/preview/thumbs/ 에 캐시한다. thumbs.json 이 어떤 시트를 다 만들었는지 적는다.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Callable

from .runner import RunResult

log = logging.getLogger(__name__)

THUMB_VERSION = 1   # 시트 형식이 바뀌면 올린다 → 옛 시트는 지우고 다시 만든다
STEP = 1.0          # 썸네일 간격 (초)
TILE_H = 90
COLS = 10
ROWS = 10
PER_SHEET = COLS * ROWS


def video_sig(video: Path) -> dict:
    st = Path(video).stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def plan(duration: float, width: int, height: int) -> dict:
    """영상 길이·미리보기 크기 → 시트 배치. 칸 너비는 화면비를 따르고 짝수."""
    tile_w = max(2, round(TILE_H * width / height / 2) * 2)
    count = max(1, math.ceil(duration / STEP))
    return {"v": THUMB_VERSION, "step": STEP, "tile_w": tile_w, "tile_h": TILE_H, "cols": COLS, "rows": ROWS,
            "per_sheet": PER_SHEET, "count": count, "sheets": math.ceil(count / PER_SHEET)}


def sheet_name(i: int) -> str:
    return f"sheet_{i:04d}.jpg"


def _num(x: float) -> str:
    return f"{x:g}"


def sheet_cmd(video: Path, i: int, p: dict, out: Path) -> list[str]:
    """i 번째 시트: [i*per_sheet*step, +per_sheet*step) 를 step 간격으로 뽑아 cols×rows 타일 한 장.
    CPU 를 덜 쓰게 스레드 2개 (분석·저장과 같이 돈다)."""
    span = p["per_sheet"] * p["step"]
    vf = f"fps=1/{_num(p['step'])},scale={p['tile_w']}:{p['tile_h']},tile={p['cols']}x{p['rows']}"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2",
            "-ss", _num(i * span), "-t", _num(span), "-i", str(video),
            "-vf", vf, "-frames:v", "1", "-q:v", "6", str(out)]


def _write(d: Path, meta: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "thumbs.json.part"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, d / "thumbs.json")


def load_meta(d: Path, video: Path, p: dict) -> dict:
    """d/thumbs.json 을 읽는다. 형식·미리보기 영상·배치가 다르면 시트를 지우고 빈 메타를 새로 쓴다."""
    d = Path(d)
    want = {"video": video_sig(video), **p}
    try:
        meta = json.loads((d / "thumbs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict) and {k: meta.get(k) for k in want} == want and isinstance(meta.get("done"), list):
        return meta
    for f in d.glob("sheet_*.jpg"):
        f.unlink(missing_ok=True)
    meta = {**want, "done": []}
    _write(d, meta)
    return meta


def mark_done(d: Path, meta: dict, i: int) -> dict:
    meta = {**meta, "done": sorted(set(meta["done"]) | {i})}
    _write(Path(d), meta)
    return meta


class Thumbs:
    """열어 본 영상 하나의 시트를 백그라운드에서 번호 순서대로 하나씩 만든다 (preview.CutAudio 와 같은 멈춤 규칙).
    다른 영상을 요청하면 만들던 것을 멈추고 그쪽으로 넘어간다. 시트 하나가 되면 on_sheet(job_id)."""

    def __init__(self, run: Callable[[list[str]], RunResult], on_sheet: Callable[[str], None]):
        self._run = run
        self._on_sheet = on_sheet
        self._cv = threading.Condition()
        self._target: tuple[str, Path, Path, dict] | None = None   # (job_id, video, 폴더, plan)
        self._current: str | None = None          # 지금 시트를 만드는 job
        self._running = None                      # 지금 도는 프로세스 (runner.Running)
        self._cancel_current = False
        self._failed: set[tuple[str, int]] = set()   # 실패한 (job, 시트) — drop 까지 다시 하지 않는다
        self._held: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self._stopped = False

    def _missing(self, job_id: str, meta: dict) -> list[int]:
        done = set(meta["done"])
        return [i for i in range(meta["sheets"]) if i not in done and (job_id, i) not in self._failed]

    def request(self, job_id: str, video: Path, out_dir: Path, duration: float, size: tuple[int, int]) -> dict | None:
        """지금 메타를 돌려주고, 모자란 시트가 있으면 이 job 을 만들 대상으로 정한다. 미리보기 영상이 없으면 None."""
        video, out_dir = Path(video), Path(out_dir)
        if not video.is_file():
            return None
        p = plan(duration, *size)
        meta = load_meta(out_dir, video, p)
        with self._cv:
            if self._stopped or job_id in self._held or not self._missing(job_id, meta):
                return meta
            self._target = (job_id, video, out_dir, p)
            handle = None
            if self._current is not None and self._current != job_id:
                self._cancel_current = True
                handle = self._running
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, daemon=True, name="timeline-thumbs")
                self._thread.start()
            self._cv.notify_all()
        if handle is not None:
            handle.cancel()
        return meta

    def started(self, handle) -> None:
        with self._cv:
            self._running = handle
            stop = self._stopped or self._cancel_current
        if stop:
            handle.cancel()

    def _stop_job(self, job_id: str):
        """잠금 안에서: 그 job 을 대상에서 빼고, 돌고 있으면 멈출 handle 을 돌려준다."""
        if self._target is not None and self._target[0] == job_id:
            self._target = None
        if self._current == job_id:
            self._cancel_current = True
            return self._running
        return None

    def cancel(self, job_id: str, timeout: float = 10.0) -> bool:
        """분석 전에: 그 job 을 멈추고 끝날 때까지(최대 timeout 초) 기다린다. release 까지 새 요청은 만들지 않는다."""
        with self._cv:
            self._held[job_id] = self._held.get(job_id, 0) + 1
            handle = self._stop_job(job_id)
        if handle is not None:
            handle.cancel()
        with self._cv:
            return self._cv.wait_for(lambda: self._current != job_id, timeout)

    def release(self, job_id: str) -> None:
        with self._cv:
            n = self._held.get(job_id, 0) - 1
            if n > 0:
                self._held[job_id] = n
            else:
                self._held.pop(job_id, None)

    def drop(self, job_id: str) -> None:
        """영상을 목록에서 뺄 때: 멈추되 기다리지 않는다. 실패 기억도 지운다."""
        with self._cv:
            self._failed = {f for f in self._failed if f[0] != job_id}
            handle = self._stop_job(job_id)
        if handle is not None:
            handle.cancel()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """만들 것도 도는 것도 없어질 때까지 기다린다 (테스트용)."""
        with self._cv:
            return self._cv.wait_for(lambda: self._target is None and self._current is None, timeout)

    def shutdown(self) -> None:
        with self._cv:
            self._stopped = True
            self._target = None
            handle = self._running
            self._cv.notify_all()
        if handle is not None:
            handle.cancel()

    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._target is not None or self._stopped)
                if self._stopped:
                    return
                target = self._target
            job_id, video, d, p = target
            try:
                meta = load_meta(d, video, p)
            except OSError:
                log.exception("썸네일 메타를 읽지 못했습니다")
                meta = None
            with self._cv:
                if self._target is not target:
                    continue                      # 그 사이 대상이 바뀌었다
                missing = self._missing(job_id, meta) if meta is not None else []
                if not missing:
                    self._target = None
                    self._cv.notify_all()
                    continue
                i = missing[0]
                self._current = job_id
                self._cancel_current = False
            out = d / sheet_name(i)
            try:
                res = self._run(sheet_cmd(video, i, p, out))
            except Exception as e:  # noqa: BLE001 — 작업 스레드가 죽지 않게
                log.exception("썸네일 만들기 실패")
                res = RunResult(False, str(e) or type(e).__name__)
            ok = res.ok and out.is_file()
            with self._cv:
                self._running = None
                stopped = self._stopped or self._cancel_current
            if not stopped:
                if ok:
                    try:
                        mark_done(d, meta, i)
                    except OSError:
                        log.exception("썸네일 메타를 쓰지 못했습니다")
                        ok = False
                if not ok:
                    log.warning("썸네일 시트 %s 실패: %s", i, res.error)
            with self._cv:
                if not stopped and not ok:
                    self._failed.add((job_id, i))
                self._current = None
                self._cancel_current = False
                self._cv.notify_all()
            if not stopped and ok:
                try:
                    self._on_sheet(job_id)
                except Exception:  # noqa: BLE001
                    log.exception("썸네일 알림 실패")
