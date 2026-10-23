"""화면(JS) ↔ 파이썬 다리. pywebview 의 js_api 로 넘기는 Api 와, 대기열이 쓰는 execute 를 만든다.

화면이 부르는 메서드는 모두 JSON 으로 바꿀 수 있는 값만 돌려준다. 창으로 보내는 이벤트는 push(event, data):
  "job"     Job 화면용 dict (job_view) — 대기열의 on_change 에 make_on_change(push) 를 넘긴다
  "models"  {"done", "total"} 진행 · {"ready": True} 완료 · {"error": True} 실패
  "update"  {"error": "..."} 설치 파일 받기/열기 실패 · {"opened": True} 열기 성공

저장(렌더)은 영상 캐시 폴더의 media/ 에 만든 뒤, 성공했을 때만 저장 폴더로 옮긴다 (publish_media) —
멈추거나 실패한 저장이 저장 폴더에 잘린 파일을 남기거나 전에 저장한 파일을 지우지 않게.
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import __version__, diff, models, paths, preview, runner, subtitles, updates
from .jobqueue import ANALYZE, RENDER
from .options import FAST_KEYS, LEVEL_KEYS, JobOptions, parse_level
from .progress import ANALYZE_WEIGHTS, RENDER_WEIGHTS
from .runner import RunResult
from .store import Job, Status, Store

log = logging.getLogger(__name__)

MEDIA_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m4a", ".mp3", ".wav"}
ALLOWED = {
    "filler": ("keep", "remove"),
    "habit": ("keep", "some", "all"),
    "silence": range(101),   # 0~100 슬라이더 (parse_level 로 자름)
    "denoise": range(101),
    "loudnorm": ("keep", "match"),
    "subs": ("omit", "embed"),
    "quality": ("low", "auto", "high"),
}
PROGRESS_INTERVAL = 0.2  # 작업·모델 진행 이벤트는 초당 5번까지
HISTORY_MAX = 100        # 영상마다 되돌릴 수 있는 편집 수

Push = Callable[[str, dict], None]


class Dialogs(Protocol):
    def open_files(self) -> list[str] | None: ...
    def choose_folder(self) -> str | None: ...


def _union(cuts: list[dict]) -> list[list[float]]:
    """컷 구간을 겹치거나 맞닿은 것끼리 합친다 (요약의 컷 수·잘린 초)."""
    merged: list[list[float]] = []
    for a, b in sorted((float(c["start"]), float(c["end"])) for c in cuts):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def _preview_url(url: str, preview: Path) -> str | None:
    """미리보기 주소 끝에 파일 수정 시각을 붙인다 — 다시 만들면 주소가 바뀌어 재생기가 새로 불러온다. 없으면 None."""
    try:
        return f"{url}?v={preview.stat().st_mtime_ns}" if preview.is_file() else None
    except OSError:
        return None


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
def media_dir(job: Job) -> Path:
    """저장(렌더) 결과를 먼저 만드는 임시 폴더."""
    return paths.job_dir(Path(job.src)) / "media"


def publish_media(media: Path, save_dir: Path) -> list[Path]:
    """media 폴더의 결과 파일을 저장 폴더로 옮긴다. 모두 저장 폴더 안 숨김 임시 이름(.<이름>.part)으로 먼저
    옮기고(다른 볼륨이면 복사) 다 옮긴 뒤에 최종 이름으로 바꾼다. 도중에 실패하면 임시 파일을 지우고 예외를 올린다
    (같은 이름의 이전 파일은 그대로 남는다)."""
    save_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for f in sorted(p for p in Path(media).iterdir() if p.is_file()):
            tmp = save_dir / f".{f.name}.part"
            tmp.unlink(missing_ok=True)
            staged.append((tmp, save_dir / f.name))
            shutil.move(str(f), str(tmp))
        for tmp, final in staged:
            os.replace(tmp, final)
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    return [final for _, final in staged]


def pipeline_command(job: Job, kind: str, store: Store, manifest: dict, data_root: Path,
                     save_dir: Path | None = None, lock=None) -> tuple[list[str], dict, dict]:
    """(명령, 진행 가중치, 더할 환경변수). 필요한 폴더를 만든다. 렌더는 media_dir(job) 에 결과를 쓴다
    (저장 폴더로 옮기는 건 make_execute)."""
    model = models.paths(manifest, data_root)
    device = models.device(store.settings, manifest)
    jd = paths.job_dir(Path(job.src))
    if kind == ANALYZE:
        (jd / "out").mkdir(parents=True, exist_ok=True)
        (jd / "work").mkdir(parents=True, exist_ok=True)
        cmd, weights = runner.analyze_command(job, model, device), ANALYZE_WEIGHTS
    elif kind == RENDER:
        save_dir = Path(save_dir or store.settings["save_dir"])
        save_dir.mkdir(parents=True, exist_ok=True)
        media = media_dir(job)
        shutil.rmtree(media, ignore_errors=True)   # 지난번에 멈춘 저장의 찌꺼기
        media.mkdir(parents=True, exist_ok=True)
        cmd, weights = runner.render_command(job, model, device, media, save_cuts(job, lock), save_subs_edits(job, lock)), RENDER_WEIGHTS
    else:
        raise ValueError(f"알 수 없는 작업 종류: {kind}")
    return cmd, weights, models.pipeline_env(manifest, data_root, store.settings)


def _silence_min_sec() -> float:
    """앱의 파이프라인이 쓰는 fillers.json(사용자 폴더에 있으면 그것, 없으면 기본) 의 silence_min_sec. 못 읽으면 0.3."""
    try:
        v = float(subtitles.load_cfg().get("silence_min_sec", 0.3))
        return v if v > 0 else 0.3
    except Exception:
        return 0.3


def save_cuts(job: Job, lock=None) -> Path | None:
    """저장용 컷 목록: 판정 결과에 되살리기·구간 작업을 반영한 <이름>.save.cuts.json. 반영할 게 없거나 못 읽으면 None(원본 그대로).
    job 필드는 lock(queue.lock) 을 쥐고 복사한 뒤 락 밖에서 쓴다."""
    with lock or contextlib.nullcontext():
        src = Path(job.src)
        restored = [list(r) for r in job.restored]
        ops = [dict(o) for o in job.range_ops]
    out = paths.job_dir(src) / "out"
    cur = _read_json(out / f"{src.stem}.cuts.json")
    if (not restored and not ops) or cur is None:
        return None
    tr = _read_json(out / f"{src.stem}.transcript.json")
    path = out / f"{src.stem}.save.cuts.json"
    path.write_text(json.dumps(diff.effective_cuts(cur, restored, ops, tr), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def save_subs_edits(job: Job, lock=None) -> Path | None:
    """저장용 자막 수정 파일 <이름>.subs_edits.json. 어절·줄 시간 수정이 없으면 낡은 파일을 지우고 None.
    job 필드는 lock(queue.lock) 을 쥐고 복사한 뒤 락 밖에서 쓴다."""
    with lock or contextlib.nullcontext():
        src, sha = Path(job.src), job.edits_sha1
        words, lines = dict(job.word_edits), {k: list(v) for k, v in job.line_times.items()}
    path = paths.job_dir(src) / "out" / f"{src.stem}.subs_edits.json"
    if not (words or lines) or not sha:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"transcript_sha1": sha, "words": words, "lines": lines}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


CUT_AUDIO_WAIT = 10.0   # 분석·저장 전에 그 영상의 미리보기 소리 만들기가 멈추길 기다리는 최대 초


def make_execute(store: Store, manifest: dict, data_root: Path, queue_getter: Callable[[], object],
                 cut_audio_getter: Callable[[], "preview.CutAudio | None"] | None = None):
    """Queue(execute=...) 에 넘긴다. queue_getter 는 Queue 를 만든 뒤에 돌려줄 수 있게 함수로 받는다.
    진행률은 queue.set_progress 로 넘기고(그게 on_change → push("job") 로 이어진다) 작업마다 초당 5번까지만.
    cut_audio_getter 가 주는 CutAudio 의 그 영상 작업은 실행 전에 멈추고(같은 영상 잠금을 쥐고 있으면 파이프라인이
    "이미 처리 중" 으로 멈춘다) 끝나면 다시 받는다."""
    def execute(job: Job, kind: str, on_start) -> RunResult:
        cut_audio = cut_audio_getter() if cut_audio_getter else None
        if cut_audio is None:
            return _execute(job, kind, on_start)
        if not cut_audio.cancel(job.id, CUT_AUDIO_WAIT):
            log.warning("미리보기 소리 만들기를 %s초 안에 멈추지 못했습니다: %s", CUT_AUDIO_WAIT, job.id)
        try:
            return _execute(job, kind, on_start)
        finally:
            cut_audio.release(job.id)

    def _execute(job: Job, kind: str, on_start) -> RunResult:
        save_dir = Path(store.settings["save_dir"]) if kind == RENDER else None
        cmd, weights, env = pipeline_command(job, kind, store, manifest, data_root, save_dir, queue_getter().lock)
        throttle = _Throttle()

        def on_progress(p: float) -> None:
            if throttle.ready():
                queue_getter().set_progress(job.id, p)

        if kind != RENDER:
            return runner.run(cmd, weights, on_progress, env=env, on_start=on_start)
        try:
            result = runner.run(cmd, weights, on_progress, env=env, on_start=on_start)
            if result.ok:
                try:
                    publish_media(media_dir(job), save_dir)
                except OSError as e:
                    log.exception("저장 폴더로 옮기기 실패: %s", job.id)
                    result = RunResult(False, f"저장 폴더에 파일을 옮기지 못했습니다: {e}")
            return result
        finally:
            shutil.rmtree(media_dir(job), ignore_errors=True)
    return execute


# ---------------------------------------------------------------------------
# 화면 API
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, store: Store, queue, push: Push, dialogs: Dialogs, *,
                 manifest: dict, data_root: Path, preview_base: str,
                 cut_audio_run: Callable[[list[str]], RunResult] | None = None):
        self._store = store
        self._queue = queue
        self._push = push
        self._dialogs = dialogs
        self._manifest = manifest
        self._data_root = Path(data_root)
        self._preview_base = preview_base.rstrip("/")
        self._bg = threading.Lock()                 # 내려받기 스레드 시작 판정
        self._download_thread: threading.Thread | None = None
        self._update_thread: threading.Thread | None = None
        self._update: dict | None = None            # 마지막으로 찾은 새 버전
        # 편집 되돌리기: 영상 id → (undo, redo) 편집 전 상태 묶음 목록 (메모리에만, 앱을 끄면 사라짐)
        self._history: dict[str, tuple[list, list]] = {}
        # 컷을 적용한 미리보기 소리 (cut_audio_run 은 테스트가 가짜를 넣는다)
        self._cut_audio = preview.CutAudio(cut_audio_run or self._run_cut_audio,
                                           lambda job_id: self._push("preview", {"id": job_id}))

    # ---- 내부 ----------------------------------------------------------------

    def _run_cut_audio(self, cmd: list[str]) -> RunResult:
        with self._queue.lock:
            settings = copy.deepcopy(self._store.settings)
        env = models.pipeline_env(self._manifest, self._data_root, settings)
        return runner.run(cmd, {"preview": 1.0}, lambda p: None, env=env, on_start=self._cut_audio.started)

    def shutdown(self) -> None:
        """창을 닫을 때: 미리보기 소리를 만들던 프로세스를 멈춘다."""
        self._cut_audio.shutdown()

    def _job(self, job_id: str) -> Job | None:
        return self._store.get(job_id)

    def _save(self) -> None:
        with self._queue.lock:
            self._store.save()

    # ---- 편집 되돌리기 ---------------------------------------------------------
    # 결과 편집(잘린 곳 되살리기, 구간 작업, 자막 고치기)만 되돌린다. 옵션 변경은 다시 판정이라 넣지 않는다.

    @staticmethod
    def _snap(job: Job) -> tuple:
        return copy.deepcopy((job.restored, job.range_ops, job.word_edits, job.line_times, job.edits_sha1))

    @staticmethod
    def _put(job: Job, snap: tuple) -> None:
        job.restored, job.range_ops, job.word_edits, job.line_times, job.edits_sha1 = copy.deepcopy(snap)

    def _record(self, job: Job, before: tuple) -> None:
        """편집 뒤에 부른다 (queue.lock 안). 실제로 바뀌었으면 편집 전 상태를 쌓고 다시 실행 목록을 비운다."""
        if self._snap(job) == before:
            return
        undo, redo = self._history.setdefault(job.id, ([], []))
        undo.append(before)
        del undo[:-HISTORY_MAX]
        redo.clear()

    def _hist(self, job_id: str) -> dict:
        undo, redo = self._history.get(job_id, ([], []))
        return {"undo": bool(undo), "redo": bool(redo)}

    def undo(self, job_id: str) -> dict | None:
        """마지막 편집을 되돌린다. → {"history": {"undo", "redo"}}, 되돌릴 게 없거나 확인 대기·저장됨이 아니면 None."""
        return self._step(job_id, forward=False)

    def redo(self, job_id: str) -> dict | None:
        """되돌린 편집을 다시 한다."""
        return self._step(job_id, forward=True)

    def _step(self, job_id: str, forward: bool) -> dict | None:
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            undo, redo = self._history.get(job_id, ([], []))
            src, dst = (redo, undo) if forward else (undo, redo)
            if not src:
                return None
            dst.append(self._snap(job))
            self._put(job, src.pop())
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view, hist = job_view(job), self._hist(job_id)
        self._push("job", view)
        return {"history": hist}

    # ---- 목록 ----------------------------------------------------------------

    def list_jobs(self) -> list[dict]:
        """원본 없음 영상의 원본이 다시 생겼으면 그 전 상태로 되돌려 돌려준다."""
        with self._queue.lock:
            if self._store.refresh_missing(mark_new=False):
                self._store.save()
            return [job_view(j) for j in self._store.jobs]

    def add_files(self, paths_: list[str]) -> list[dict]:
        added = False
        with self._queue.lock:
            for raw in paths_ or []:
                p = Path(os.path.abspath(str(raw)))
                if p.suffix.lower() not in MEDIA_EXTS or not p.is_file():
                    continue
                if self._store.add(p):
                    added = True
            if added:
                self._store.save()
        return self.list_jobs()

    def pick_files(self) -> list[dict]:
        files = self._dialogs.open_files()
        return self.add_files(list(files)) if files else self.list_jobs()

    def remove_job(self, job_id: str) -> list[dict]:
        """대기 중이면 빼고, 처리·저장 중이면 멈추고 뺀다 (확인은 화면이 먼저 받는다)."""
        self._queue.cancel(job_id)
        with self._queue.lock:
            self._history.pop(job_id, None)
        return self.list_jobs()

    def start(self) -> list[dict]:
        self._queue.start_all()
        return self.list_jobs()

    def save(self, job_id: str) -> dict | None:
        # 알림(evaluate_js)은 락을 놓은 뒤에 보낸다 — 맥에선 메인 스레드를 기다리므로 락을 쥐고 보내면 교착 위험.
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            before = job.status
            self._queue.request_save(job_id, notify=False)
            changed = job.status != before
            view = job_view(job)
        if changed:
            self._push("job", view)
        return view

    # ---- 옵션 ----------------------------------------------------------------

    def get_options(self, job_id: str) -> dict | None:
        with self._queue.lock:
            job = self._job(job_id)
            return job.options.to_dict() if job else None

    def set_option(self, job_id: str, key: str, value) -> dict | None:
        """{"options": 지금 옵션, "accepted": 반영 여부}. 처리·저장 중엔 바꾸지 않는다 (화면이 막는다).
        없는 영상이면 None, 잘못된 key·value 는 ValueError. silence·denoise 는 0~100 숫자(범위 밖은 자름)."""
        if key in LEVEL_KEYS:
            value = parse_level(value)
        elif key not in ALLOWED or not isinstance(value, str) or value not in ALLOWED[key]:
            raise ValueError(f"잘못된 옵션: {key}={value!r}")
        rejudge = changed_status = False
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            if job.status in (Status.PROCESSING, Status.SAVING):
                return {"options": job.options.to_dict(), "accepted": False}
            if getattr(job.options, key) != value:
                setattr(job.options, key, value)
                if key in FAST_KEYS:
                    rejudge = job.status in (Status.REVIEW, Status.SAVED)
                elif job.status == Status.SAVED:
                    job.status = Status.REVIEW
                    changed_status = True
                self._store.save()
            opts = job.options.to_dict()
            view = job_view(job)
        if rejudge:
            self._queue.rejudge(job_id)   # 상태 변화는 대기열이 알린다
        elif changed_status:
            self._push("job", view)
        return {"options": opts, "accepted": True}

    def set_restored(self, job_id: str, start: float, end: float, restored: bool) -> dict | None:
        """잘린 곳 [start, end] 를 되살리거나(True) 다시 자른다(False). 처리 없이 바로 반영되고 저장 때 쓰인다.
        확인 대기·저장됨 상태에서만. → {"restored": [[시작, 끝], ...]}, 안 되면 None.
        저장됨이었으면 저장한 파일과 달라지므로 확인 대기로 돌린다."""
        start, end = float(start), float(end)
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED) or end <= start:
                return None
            before = self._snap(job)
            cut = {"start": start, "end": end}
            others = [r for r in job.restored if not diff.is_restored(cut, [r])]
            job.restored = others + ([[start, end]] if restored else [])
            self._record(job, before)
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view, out = job_view(job), [list(r) for r in job.restored]
        self._push("job", view)
        return {"restored": out}

    def _change_ops(self, job_id: str, change: Callable[[Job], bool]) -> dict | None:
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            before = self._snap(job)
            if not change(job):
                return None
            self._record(job, before)
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view, out = job_view(job), [dict(o) for o in job.range_ops]
        self._push("job", view)
        return {"range_ops": out}

    def _duration(self, job: Job) -> float:
        tr = _read_json(paths.job_dir(Path(job.src)) / "out" / f"{Path(job.src).stem}.transcript.json")
        try:
            return float((tr or {}).get("duration") or (job.summary or {}).get("orig_sec") or 0)
        except (TypeError, ValueError):
            return 0.0

    def add_range_op(self, job_id: str, op: str, start: float, end: float) -> dict | None:
        """원본 시간 [start, end] 를 직접 자르거나(cut) 되살린다(restore). 나중 작업이 이긴다.
        확인 대기·저장됨에서만, 구간이 0.05초 이상이고 영상 안일 때만. → {"range_ops": [...]}, 안 되면 None."""
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(start) and math.isfinite(end)):
            return None

        def change(job: Job) -> bool:
            if op not in ("cut", "restore") or start < 0 or end - start < 0.05 or end > self._duration(job):
                return False
            job.range_ops = [*job.range_ops, {"op": op, "start": start, "end": end}]
            return True
        return self._change_ops(job_id, change)

    def remove_range_op(self, job_id: str, index: int) -> dict | None:
        """index 번째 구간 작업을 지운다."""
        def change(job: Job) -> bool:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(job.range_ops):
                return False
            job.range_ops = [o for i, o in enumerate(job.range_ops) if i != index]
            return True
        return self._change_ops(job_id, change)

    # ---- 자막 고치기 ----------------------------------------------------------

    def _drop_stale_edits(self, job: Job, tr: dict) -> bool:
        """고친 자막을 만들 때의 받아쓰기 지문이 지금과 다르면 어절·줄 시간 수정을 버린다 (구간 작업은 시간 기준이라 남김).
        버렸으면 True. queue.lock 을 쥔 채 부른다."""
        if job.edits_sha1 is None or job.edits_sha1 == subtitles.transcript_sha1(tr):
            return False
        job.word_edits, job.line_times, job.edits_sha1 = {}, {}, None
        self._history.pop(job.id, None)   # 옛 받아쓰기 기준 수정은 되돌려도 맞지 않는다
        self._store.save()
        return True

    def _caption_state_ex(self, job_id: str):
        """(job, cuts, transcript, caps, edits_dropped) — 파일을 못 읽으면 None."""
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            src, restored = Path(job.src), [list(r) for r in job.restored]
            ops = [dict(o) for o in job.range_ops]
        out = paths.job_dir(src) / "out"
        cuts = _read_json(out / f"{src.stem}.cuts.json")
        tr = _read_json(out / f"{src.stem}.transcript.json")
        if cuts is None or tr is None:
            return None
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            dropped = self._drop_stale_edits(job, tr)
            edits, times = dict(job.word_edits), {k: list(v) for k, v in job.line_times.items()}
        cuts = diff.effective_cuts(cuts, restored, ops, tr)
        try:
            caps = subtitles.captions(cuts, tr, subtitles.load_cfg(), edits, times)
        except Exception:
            log.exception("자막 계산 실패")
            return None
        return job, cuts, tr, caps, dropped

    def _edit_subs(self, job_id: str, change: Callable[[Job, dict], bool]) -> dict | None:
        """자막 수정 공통: 확인 대기·저장됨에서만. change(job, tr) 가 False 면 거절. → {"captions": 새 자막 목록, "edits_dropped": 고치기 직전에 낡은 수정을 버렸는가}."""
        st = self._caption_state_ex(job_id)
        if st is None:
            return None
        _, _, tr, _, dropped = st
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            before = self._snap(job)
            if not change(job, tr):
                return None
            if job.edits_sha1 is None and (job.word_edits or job.line_times):
                job.edits_sha1 = subtitles.transcript_sha1(tr)
            elif not (job.word_edits or job.line_times):
                job.edits_sha1 = None
            self._record(job, before)
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view = job_view(job)
        self._push("job", view)
        st = self._caption_state_ex(job_id)
        if st is None:
            return None
        return {"captions": st[3], "edits_dropped": dropped or st[4], "history": self._hist(job_id)}

    def get_captions(self, job_id: str) -> dict | None:
        """{"captions": 목록, "edits_dropped": 받아쓰기가 바뀌어 고친 자막을 버린 그 한 번만 True}."""
        st = self._caption_state_ex(job_id)
        if st is None:
            return None
        return {"captions": st[3], "edits_dropped": st[4], "history": self._hist(job_id)}

    def set_word(self, job_id: str, key: str, text: str | None) -> dict | None:
        """어절 글자를 고친다. text 가 None 이거나 원래 글자와 같으면 원래대로."""
        if not isinstance(key, str) or not (text is None or isinstance(text, str)):
            return None

        def change(job: Job, tr: dict) -> bool:
            orig = subtitles.transcript_words(tr).get(key)
            if orig is None:
                return False
            if text is None or text.strip() == orig:
                job.word_edits.pop(key, None)
            else:
                job.word_edits[key] = text.strip()
            return True
        return self._edit_subs(job_id, change)

    def replace_text(self, job_id: str, find: str, repl: str, apply: bool) -> dict | None:
        """찾아 바꾸기. apply=False 면 몇 곳인지만 알려 준다. → {"count": 곳, "keys": [바뀔 어절 키]}"""
        st = self._caption_state_ex(job_id)
        if st is None or not isinstance(find, str) or not isinstance(repl, str):
            return None
        plan, count = subtitles.plan_replace(st[3], find, repl)
        result = {"count": count, "keys": list(plan), "edits_dropped": st[4]}
        if apply and plan:
            def change(job: Job, tr: dict) -> bool:
                words = subtitles.transcript_words(tr)
                for k, v in plan.items():
                    if v.strip() == words.get(k):
                        job.word_edits.pop(k, None)
                    else:
                        job.word_edits[k] = v.strip()
                return True
            edited = self._edit_subs(job_id, change)
            if edited is None:
                return None
            result.update(captions=edited["captions"], edits_dropped=edited["edits_dropped"] or st[4],
                          history=edited["history"])
        return result

    def set_line_time(self, job_id: str, key: str, start: float | None, end: float | None) -> dict | None:
        """자막 줄의 시작·끝(원본 초)을 맞춘다. 둘 다 None 이면 원래대로. 앞 줄 끝~다음 줄 시작,
        0~영상 길이 안으로 자르고 길이는 0.3초 이상."""
        import math
        st = self._caption_state_ex(job_id)
        if st is None:
            return None
        _, _, tr, caps, was_dropped = st
        line = next((c for c in caps if c["key"] == key), None)
        if line is None:
            return None
        remove = start is None and end is None
        new = None
        if not remove:
            try:
                s = float(line["start"] if start is None else start)
                e = float(line["end"] if end is None else end)
            except (TypeError, ValueError):
                return None
            if not (math.isfinite(s) and math.isfinite(e)):
                return None
            lo, hi = subtitles.line_bounds(caps, key)
            try:
                dur = float(tr.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            lo = max(lo, 0.0)
            if dur > 0:
                hi = min(hi, dur)
            s = min(max(s, lo), hi)
            e = min(max(e, lo), hi)
            if e - s < subtitles.MIN_LINE_SEC:
                e = s + subtitles.MIN_LINE_SEC
                if e > hi:
                    e = hi
                    s = max(lo, e - subtitles.MIN_LINE_SEC)
            new = [s, e]

        in_line = set(line.get("keys") or [w["key"] for w in line["words"]]) | {key}

        def change(job: Job, _tr: dict) -> bool:
            # 이 줄에 든 저장 key 는 모두 지운다 (다시 판정 뒤 줄 가운데 어절에 붙은 것까지) — 원래대로가 실제로
            # 되돌리고, 새 시간은 두 번째 key 를 더하지 않고 바꾼다
            for k in [k for k in job.line_times if k in in_line]:
                del job.line_times[k]
            if new is not None:
                job.line_times[key] = new
            return True
        r = self._edit_subs(job_id, change)
        if r is not None:
            r["edits_dropped"] = r["edits_dropped"] or was_dropped
        return r

    # ---- 결과 ----------------------------------------------------------------

    def get_result(self, job_id: str) -> dict | None:
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            src, summary, status = Path(job.src), job.summary, job.status
            restored = [list(r) for r in job.restored]
            ops = [dict(o) for o in job.range_ops]
            save_dir = Path(self._store.settings["save_dir"])
            snap = copy.deepcopy(job)
            settings = copy.deepcopy(self._store.settings)
        out = paths.job_dir(src) / "out"
        cur = _read_json(out / f"{src.stem}.cuts.json")
        effective = cur
        transcript = _read_json(out / f"{src.stem}.transcript.json")
        dropped = False
        if transcript is not None:
            with self._queue.lock:
                job = self._job(job_id)
                dropped = job is not None and self._drop_stale_edits(job, transcript)
        duration = 0.0
        try:
            duration = float((transcript or {}).get("duration") or (summary or {}).get("orig_sec") or 0)
        except (TypeError, ValueError):
            pass
        items = diff.cut_items(cur, restored, ops, transcript) if cur else []
        if (restored or ops) and cur is not None:
            # 되살린 곳은 미리보기에서 건너뛰지 않고 요약에서도 뺀다 (저장도 같은 목록으로 한다)
            effective = diff.effective_cuts(cur, restored, ops, transcript)
            kept = effective["cuts"]
            merged = _union(kept)
            removed = sum(b - a for a, b in merged)
            orig = float((summary or {}).get("orig_sec") or duration)
            summary = {**(summary or {}), "cuts": len(merged), "removed_sec": removed,
                       "final_sec": max(orig - removed, 0.0), "restored": sum(1 for c in cur["cuts"] if diff.is_restored(c, restored))}
            cur = {**cur, "cuts": kept}
        if summary is not None or cur is not None:
            # 말 없는 구간 슬라이더 설명의 {곳}곳 · −{초}초 (요약과 같은 컷 목록에서, 겹치면 합쳐서)
            sil = _union([c for c in (cur or {}).get("cuts", []) if diff._is_silence(c)])
            summary = {**(summary or {}), "silence_cuts": len(sil), "silence_sec": sum(b - a for a, b in sil)}
        if summary is not None:
            # 슬라이더 설명의 "N초 이상" — 앱의 파이프라인이 쓰는 fillers.json 의 silence_min_sec
            summary = {**summary, "silence_min_sec": _silence_min_sec()}
        result = {"summary": summary, "cuts": diff.cut_ranges(cur) if cur else [], "cut_items": items,
                  "range_ops": ops, "history": self._hist(job_id),
                  "duration": duration}
        result.update(self._preview(snap, status, effective, settings))
        if dropped:
            result["edits_dropped"] = True
        if status == Status.SAVED:
            names = [f"{src.stem}_clean.mp4", f"{src.stem}_clean.m4a", f"{src.stem}_clean.srt"]
            result["saved_paths"] = [str(save_dir / n) for n in names if (save_dir / n).is_file()]
        return result

    def _preview(self, job: Job, status: str, cuts: dict | None, settings: dict) -> dict:
        """get_result 의 "preview"(새 미리보기 폴더) 와 "preview_url"(옛 preview.mp4 만 있는 작업).
        미리보기를 못 만들었으면(분석은 성공) 둘 다 None — 화면은 재생기 대신 안내를 보여 준다.
        job 은 락 밖에서 쓰는 사본, cuts 는 되살림·구간 작업을 반영한 컷 목록."""
        jd = paths.job_dir(Path(job.src))
        pdir, old = jd / "preview", jd / "preview.mp4"
        base = f"{self._preview_base}/{job.id}"
        if not (pdir / "audio_full.index.json").is_file():
            return {"preview": None, "preview_url": _preview_url(f"{base}/preview.mp4", old)}
        old.unlink(missing_ok=True)   # 예전 방식 미리보기는 이제 안 쓴다

        def url(name: str) -> str | None:
            return _preview_url(f"{base}/preview/{name}", pdir / name)

        cut_audio = cut_index = None
        pending = False
        if cuts is not None:
            key = preview.cut_key(cuts.get("cuts", []), job.options)
            target = pdir / f"audio_cut.{key}.m4a"
            if status in (Status.REVIEW, Status.SAVED):
                stem = Path(job.src).stem
                cuts_path = jd / "out" / f"{stem}.preview.cuts.json"
                cmd = runner.preview_audio_command(job, models.paths(self._manifest, self._data_root),
                                                   models.device(settings, self._manifest), cuts_path, target)

                def prepare() -> None:   # 만들 때만 쓴다 (작업 스레드에서 실행 바로 전에)
                    cuts_path.parent.mkdir(parents=True, exist_ok=True)
                    cuts_path.write_text(json.dumps(cuts, ensure_ascii=False, indent=1), encoding="utf-8")

                pending = self._cut_audio.request(job.id, key, cmd, target, prepare=prepare) == "pending"
            if preview.is_ready(target):
                cut_audio, cut_index = url(target.name), url(preview.index_of(target).name)
        return {"preview_url": None,
                "preview": {"video": url("video.mp4"), "video_index": url("video.index.json"),
                            "audio_full": url("audio_full.m4a"), "audio_full_index": url("audio_full.index.json"),
                            "cut_audio": cut_audio, "cut_index": cut_index, "pending": pending}}

    # ---- 설정 ----------------------------------------------------------------

    def get_settings(self) -> dict:
        with self._queue.lock:
            return {"save_dir": self._store.settings["save_dir"]}

    def set_save_dir(self) -> str:
        folder = self._dialogs.choose_folder()
        with self._queue.lock:
            if folder:
                self._store.settings["save_dir"] = os.path.abspath(str(folder))
                self._store.save()
            return self._store.settings["save_dir"]

    def get_version(self) -> str:
        return __version__

    # ---- 모델 ----------------------------------------------------------------

    def _downloading(self) -> bool:
        t = self._download_thread
        return bool(t and t.is_alive())

    def model_status(self) -> dict:
        """ready 가 False 면 화면이 덮개를 띄우고 download_models(→ models.prepare)를 부른다. 모델이 다 있어도
        윈도우에서 GPU 를 아직 안 정했거나 그 CUDA 폴더가 덜 갖춰졌으면 prepare 를 다시 돌려야 하므로 False."""
        missing = [m["key"] for m in models.status(self._manifest, self._data_root)]
        with self._queue.lock:
            gpu = models.gpu_undecided(self._store.settings, self._manifest, self._data_root)
        return {"ready": not missing and not gpu, "missing": missing, "downloading": self._downloading()}

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
        settings = dict(self._store.settings)

        def on_progress(done: int, total: int) -> None:
            if throttle.ready(force=done >= total):
                self._push("models", {"done": done, "total": total})

        try:
            models.prepare(self._manifest, self._data_root, settings, on_progress)
        except Exception:  # noqa: BLE001 — DownloadError 말고도 화면엔 다시 시도로 보여 준다
            log.exception("모델 준비 실패")
            self._push("models", {"error": True})
            return
        try:
            with self._queue.lock:
                self._store.settings["gpu"] = settings.get("gpu")
                self._store.save()
        except Exception:  # noqa: BLE001
            log.exception("설정 저장 실패")
        self._push("models", {"ready": True})

    # ---- 업데이트 ------------------------------------------------------------

    def check_update(self) -> dict | None:
        info = updates.check(__version__)
        self._update = info
        with self._queue.lock:
            dismissed = self._store.settings.get("dismissed_version")
        if not info or info["version"] == dismissed:
            return None
        return info

    def dismiss_update(self, version: str) -> None:
        with self._queue.lock:
            self._store.settings["dismissed_version"] = version
            self._store.save()

    def _busy(self) -> bool:
        with self._queue.lock:
            return any(j.status in (Status.PROCESSING, Status.SAVING) for j in self._store.jobs)

    def install_update(self, force: bool = False) -> dict:
        """마지막으로 찾은 새 버전 설치 파일을 받아 연다 (스레드). 실패는 push("update", {"error"}).
        처리·저장 중인 영상이 있으면 열지 않고 {"started": False, "confirm": True} — 화면이 물어본 뒤
        force=True 로 다시 부르면 설치 파일을 열기 직전에 대기열을 멈춘다 (멈춘 영상은 대기로 저장)."""
        if not force and self._busy():
            return {"started": False, "confirm": True}
        info = self._update or updates.check(__version__)
        if not info:
            return {"started": False}
        with self._bg:
            t = self._update_thread
            if t and t.is_alive():
                return {"started": False}
            self._update_thread = threading.Thread(target=self._install_update, args=(info["url"], force),
                                                   daemon=True, name="update-download")
            self._update_thread.start()
        return {"started": True}

    def _install_update(self, url: str, stop_queue: bool = False) -> None:
        try:
            path = updates.download(url, self._data_root / "updates")
            if stop_queue:
                self._queue.shutdown()
            updates.open_file(path)
        except Exception as e:  # noqa: BLE001
            log.exception("업데이트 설치 파일 받기 실패")
            self._push("update", {"error": str(e) or type(e).__name__})
            return
        self._push("update", {"opened": True})


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
