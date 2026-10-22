"""화면(JS) ↔ 파이썬 다리. pywebview 의 js_api 로 넘기는 Api 와, 대기열이 쓰는 execute 를 만든다.

화면이 부르는 메서드는 모두 JSON 으로 바꿀 수 있는 값만 돌려준다. 창으로 보내는 이벤트는 push(event, data):
  "job"     Job 화면용 dict (job_view) — 대기열의 on_change 에 make_on_change(push) 를 넘긴다
  "models"  {"done", "total"} 진행 · {"ready": True} 완료 · {"error": True} 실패
  "update"  {"error": "..."} 설치 파일 받기/열기 실패 · {"opened": True} 열기 성공

저장(렌더)은 영상 캐시 폴더의 media/ 에 만든 뒤, 성공했을 때만 저장 폴더로 옮긴다 (publish_media) —
멈추거나 실패한 저장이 저장 폴더에 잘린 파일을 남기거나 전에 저장한 파일을 지우지 않게.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import __version__, diff, models, paths, runner, subtitles, updates
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
                     save_dir: Path | None = None) -> tuple[list[str], dict, dict]:
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
        cmd, weights = runner.render_command(job, model, device, media, save_cuts(job)), RENDER_WEIGHTS
    else:
        raise ValueError(f"알 수 없는 작업 종류: {kind}")
    if device == "cuda":
        cmd += ["--compute-type", models.CUDA_COMPUTE_TYPE]
    return cmd, weights, models.pipeline_env(manifest, data_root, store.settings)


def save_cuts(job: Job) -> Path | None:
    """저장용 컷 목록: 판정 결과에서 되살린 곳만 뺀 <이름>.save.cuts.json. 되살린 곳이 없거나 못 읽으면 None(원본 그대로)."""
    src = Path(job.src)
    out = paths.job_dir(src) / "out"
    cur = _read_json(out / f"{src.stem}.cuts.json")
    if not job.restored or cur is None:
        return None
    path = out / f"{src.stem}.save.cuts.json"
    path.write_text(json.dumps(diff.without_restored(cur, job.restored), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def make_execute(store: Store, manifest: dict, data_root: Path, queue_getter: Callable[[], object]):
    """Queue(execute=...) 에 넘긴다. queue_getter 는 Queue 를 만든 뒤에 돌려줄 수 있게 함수로 받는다.
    진행률은 queue.set_progress 로 넘기고(그게 on_change → push("job") 로 이어진다) 작업마다 초당 5번까지만."""
    def execute(job: Job, kind: str, on_start) -> RunResult:
        save_dir = Path(store.settings["save_dir"]) if kind == RENDER else None
        cmd, weights, env = pipeline_command(job, kind, store, manifest, data_root, save_dir)
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
                 manifest: dict, data_root: Path, preview_base: str):
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

    # ---- 내부 ----------------------------------------------------------------

    def _job(self, job_id: str) -> Job | None:
        return self._store.get(job_id)

    def _save(self) -> None:
        with self._queue.lock:
            self._store.save()

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
        return self.list_jobs()

    def start(self) -> list[dict]:
        self._queue.start_all()
        return self.list_jobs()

    def save(self, job_id: str) -> dict | None:
        # 락을 쥔 채로 요청해야 작업 스레드가 렌더를 끝내기 전에 prev_cuts 를 지울 수 있다 (queue.lock 은 RLock).
        # 알림(evaluate_js)은 락을 놓은 뒤에 보낸다 — 맥에선 메인 스레드를 기다리므로 락을 쥐고 보내면 교착 위험.
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            before = job.status
            self._queue.request_save(job_id, notify=False)
            if job.status in (Status.SAVING, Status.SAVED) and job.prev_cuts:
                job.prev_cuts = None   # "달라진 곳" 은 저장 때 초기화 (스펙 3.3)
                self._store.save()
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

    def set_option(self, job_id: str, key: str, value: str) -> dict | None:
        """{"options": 지금 옵션, "accepted": 반영 여부}. 처리·저장 중엔 바꾸지 않는다 (화면이 막는다).
        없는 영상이면 None, 잘못된 key·value 는 ValueError."""
        if key not in ALLOWED or value not in ALLOWED[key]:
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
                else:
                    job.prev_cuts = None   # "달라진 곳" 은 다음 옵션 변경 때 초기화 (스펙 3.3)
                    if job.status == Status.SAVED:
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
            cut = {"start": start, "end": end}
            others = [r for r in job.restored if not diff.is_restored(cut, [r])]
            job.restored = others + ([[start, end]] if restored else [])
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view, out = job_view(job), [list(r) for r in job.restored]
        self._push("job", view)
        return {"restored": out}

    # ---- 자막 고치기 ----------------------------------------------------------

    def _caption_state(self, job_id: str):
        """(job, cuts, transcript, caps) — 파일을 못 읽으면 None."""
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            src, restored = Path(job.src), [list(r) for r in job.restored]
            edits, times = dict(job.word_edits), {k: list(v) for k, v in job.line_times.items()}
        out = paths.job_dir(src) / "out"
        cuts = _read_json(out / f"{src.stem}.cuts.json")
        tr = _read_json(out / f"{src.stem}.transcript.json")
        if cuts is None or tr is None:
            return None
        cuts = diff.without_restored(cuts, restored)   # TODO(Task 4): effective_cuts
        try:
            caps = subtitles.captions(cuts, tr, subtitles.load_cfg(), edits, times)
        except Exception:
            log.exception("자막 계산 실패")
            return None
        return job, cuts, tr, caps

    def _edit_subs(self, job_id: str, change: Callable[[Job, dict], bool]) -> list | None:
        """자막 수정 공통: 확인 대기·저장됨에서만. change(job, tr) 가 False 면 거절. → 새 자막 목록."""
        st = self._caption_state(job_id)
        if st is None:
            return None
        _, _, tr, _ = st
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            if not change(job, tr):
                return None
            if job.edits_sha1 is None and (job.word_edits or job.line_times):
                job.edits_sha1 = subtitles.transcript_sha1(tr)
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view = job_view(job)
        self._push("job", view)
        return self.get_captions(job_id)

    def get_captions(self, job_id: str) -> list | None:
        st = self._caption_state(job_id)
        return st[3] if st else None

    def set_word(self, job_id: str, key: str, text: str | None) -> list | None:
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
        st = self._caption_state(job_id)
        if st is None or not isinstance(find, str) or not isinstance(repl, str):
            return None
        plan, count = subtitles.plan_replace(st[3], find, repl)
        result = {"count": count, "keys": list(plan)}
        if apply and plan:
            def change(job: Job, tr: dict) -> bool:
                words = subtitles.transcript_words(tr)
                for k, v in plan.items():
                    if v.strip() == words.get(k):
                        job.word_edits.pop(k, None)
                    else:
                        job.word_edits[k] = v.strip()
                return True
            if self._edit_subs(job_id, change) is None:
                return None
        return result

    def set_line_time(self, job_id: str, key: str, start: float | None, end: float | None) -> list | None:
        """자막 줄의 시작·끝(원본 초)을 맞춘다. 둘 다 None 이면 원래대로. 앞 줄 끝~다음 줄 시작,
        0~영상 길이 안으로 자르고 길이는 0.3초 이상."""
        import math
        st = self._caption_state(job_id)
        if st is None:
            return None
        _, _, tr, caps = st
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

        def change(job: Job, _tr: dict) -> bool:
            if new is None:
                job.line_times.pop(key, None)
            else:
                job.line_times[key] = new
            return True
        return self._edit_subs(job_id, change)

    # ---- 결과 ----------------------------------------------------------------

    def get_result(self, job_id: str) -> dict | None:
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            src, summary, prev, status = Path(job.src), job.summary, job.prev_cuts, job.status
            restored = [list(r) for r in job.restored]
            save_dir = Path(self._store.settings["save_dir"])
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
        # 미리보기를 못 만들었으면(분석은 성공) None — 화면은 재생기 대신 안내를 보여 준다
        preview = paths.job_dir(src) / "preview.mp4"
        items = diff.cut_items(cur, restored) if cur else []
        if restored and cur is not None:
            # 되살린 곳은 미리보기에서 건너뛰지 않고 요약에서도 뺀다 (저장도 같은 목록으로 한다)
            kept = diff.without_restored(cur, restored)["cuts"]
            removed = sum(float(c["end"]) - float(c["start"]) for c in kept)
            orig = float((summary or {}).get("orig_sec") or duration)
            summary = {**(summary or {}), "cuts": len(kept), "removed_sec": removed,
                       "final_sec": max(orig - removed, 0.0), "restored": len(cur["cuts"]) - len(kept)}
            cur = {**cur, "cuts": kept}
        result = {"summary": summary, "cuts": diff.cut_ranges(cur) if cur else [], "cut_items": items,
                  "changes": changes,
                  "preview_url": f"{self._preview_base}/{job_id}/preview.mp4" if preview.is_file() else None,
                  "duration": duration}
        if status == Status.SAVED:
            names = [f"{src.stem}_clean.mp4", f"{src.stem}_clean.m4a", f"{src.stem}_clean.srt"]
            result["saved_paths"] = [str(save_dir / n) for n in names if (save_dir / n).is_file()]
        return result

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
