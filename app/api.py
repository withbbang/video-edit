"""화면(JS) ↔ 파이썬 다리. pywebview 의 js_api 로 넘기는 Api 와, 대기열이 쓰는 execute 를 만든다.

화면이 부르는 메서드는 모두 JSON 으로 바꿀 수 있는 값만 돌려준다. 창으로 보내는 이벤트는 push(event, data):
  "job"     Job 화면용 dict (job_view) — 대기열의 on_change 에 make_on_change(push) 를 넘긴다
  "models"  {"done", "total"} 진행 · {"ready": True} 완료 · {"error": True} 실패
  "update"  {"error": "..."} 설치 파일 받기/열기 실패 · {"opened": True} 열기 성공

저장(렌더)은 영상 캐시 폴더의 media/ 에 만든 뒤, 성공했을 때만 저장 폴더로 옮긴다 (publish_media) —
멈추거나 실패한 저장이 저장 폴더에 잘린 파일을 남기거나 전에 저장한 파일을 지우지 않게.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import math
import logging
import os
import re
import secrets
import shutil
import struct
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable, Protocol

from . import __version__, bgm, diff, models, paths, preview, runner, sequence, subtitles, thumbs, updates
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
    "burn": ("off", "on"),
    "quality": ("low", "auto", "high"),
}
PROGRESS_INTERVAL = 0.2  # 작업·모델 진행 이벤트는 초당 5번까지
HISTORY_MAX = 100        # 영상마다 되돌릴 수 있는 편집 수

Push = Callable[[str, dict], None]


class Dialogs(Protocol):
    def open_files(self) -> list[str] | None: ...
    def choose_folder(self) -> str | None: ...
    def open_image(self) -> str | None: ...
    def open_video(self) -> str | None: ...
    def open_audio(self) -> str | None: ...


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
        cmd, weights = runner.analyze_command(job, model, device, save_sequence(job, lock)), ANALYZE_WEIGHTS
    elif kind == RENDER:
        save_dir = Path(save_dir or store.settings["save_dir"])
        save_dir.mkdir(parents=True, exist_ok=True)
        media = media_dir(job)
        shutil.rmtree(media, ignore_errors=True)   # 지난번에 멈춘 저장의 찌꺼기
        media.mkdir(parents=True, exist_ok=True)
        tr = _read_json(jd / "out" / f"{Path(job.src).stem}.transcript.json")
        with lock or contextlib.nullcontext():
            settle_stable(job, tr, store)   # 화면이 아직 자막을 안 읽었어도 옛 전사 키 수정을 쓰지 않게
        cmd, weights = runner.render_command(job, model, device, media, save_cuts(job, lock), save_subs_edits(job, lock),
                                         save_overlays(job, lock), save_sequence(job, lock),
                                         save_bgms(job, lock)), RENDER_WEIGHTS
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


def settle_stable(job: Job, tr: dict | None, store: Store) -> bool:
    """넣기·빼기 뒤 다시 판정이 끝났으면(처리 중이 아니고 디스크 전사가 지금 조각 목록 것) stable_edits 를
    새 전사 키로 풀어 넣고 edits_sha1 을 새 지문(수정이 없으면 None)으로 바꾼다. 풀었으면 True. queue.lock 안에서."""
    if (job.stable_edits is None or tr is None or job.status == Status.PROCESSING
            or not _tr_matches_pieces(job.pieces, tr)):
        return False
    st = job.stable_edits
    job.word_edits, job.line_times, job.word_styles = (sequence.from_stable(st.get(k) or {}, tr) for k in STABLE_KEYS)
    job.stable_edits = None
    has = job.word_edits or job.line_times or job.word_styles
    job.edits_sha1 = subtitles.transcript_sha1(tr) if has else None
    store.save()
    return True


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
    path.write_text(json.dumps(_effective_cuts(cur, restored, ops, tr), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def _effective_cuts(cur: dict | None, restored: list, ops: list, transcript: dict | None) -> dict | None:
    """되살린 곳·구간 작업을 반영한 컷 (저장·미리보기·요약·BGM 이 같은 목록을 쓴다). 반영할 게 없으면 cur 그대로."""
    if cur is None or not (restored or ops):
        return cur
    return diff.effective_cuts(cur, restored, ops, transcript)


def bgm_context_of(effective: dict | None, transcript: dict | None, duration: float) -> tuple[list, list, float]:
    """effective 컷·전사 → (남길 구간 keeps, 결과 초 말 구간 speech, fps)."""
    fps = float(subtitles._fps(effective or {}))
    cuts = [(c["start"], c["end"]) for c in (effective or {}).get("cuts", [])]
    keeps = bgm.keeps_from_cuts(cuts, duration, fps)
    words = [w for seg in (transcript or {}).get("segments", []) for w in seg.get("words") or []]
    return keeps, bgm.speech_spans(words, keeps), fps


def _bgm_inputs(src: Path, restored: list, ops: list, summary: dict | None) -> tuple:
    """파일에서 읽은 effective 컷·전사·길이로 BGM 계산용 (keeps, speech, fps) 를 만든다."""
    out = paths.job_dir(src) / "out"
    cur = _read_json(out / f"{src.stem}.cuts.json")
    tr = _read_json(out / f"{src.stem}.transcript.json")
    try:
        duration = float((tr or {}).get("duration") or (summary or {}).get("orig_sec") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return bgm_context_of(_effective_cuts(cur, restored, ops, tr), tr, duration)


def save_bgms(job: Job, lock=None, context=None) -> Path | None:
    """저장용 음원 목록 <이름>.bgm.json (파이프라인 --bgm). 없으면 낡은 파일을 지우고 None.
    음량 점은 손대지 않은(points None) 항목만 지금 컷·말로 자동 곡선을 풀어 넣는다.
    context=(keeps, speech, fps) 를 주면 그걸 쓰고, 아니면 지금 컷·전사에서 계산한다."""
    with lock or contextlib.nullcontext():
        src, items = Path(job.src), copy.deepcopy(job.bgms)
        restored, ops, summary = [list(r) for r in job.restored], [dict(o) for o in job.range_ops], job.summary
    jd = paths.job_dir(src)
    path = jd / "out" / f"{src.stem}.bgm.json"
    if not items:
        path.unlink(missing_ok=True)
        return None
    _, speech, _ = context or _bgm_inputs(src, restored, ops, summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [{"path": str(jd / "bgm" / b["file"]), "at": b["at"], "in": b["in"], "out": b["out"], "gain": b["gain"],
             "points": bgm.resolve_points(b, speech)} for b in items]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def save_subs_edits(job: Job, lock=None) -> Path | None:
    """저장용 자막 수정 파일 <이름>.subs_edits.json. 어절·줄 시간 수정·글자 꾸밈이 없으면 낡은 파일을 지우고 None.
    job 필드는 lock(queue.lock) 을 쥐고 복사한 뒤 락 밖에서 쓴다."""
    with lock or contextlib.nullcontext():
        src, sha = Path(job.src), job.edits_sha1
        words, lines = dict(job.word_edits), {k: list(v) for k, v in job.line_times.items()}
        styles = copy.deepcopy(job.word_styles)
    path = paths.job_dir(src) / "out" / f"{src.stem}.subs_edits.json"
    if not (words or lines or styles) or not sha:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"transcript_sha1": sha, "words": words, "lines": lines}
    if styles:
        data["styles"] = styles
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


PNG_SIG = b"\x89PNG\r\n\x1a\n"
OVERLAY_SPAN = 5.0       # 새 이미지가 나오는 기본 초
OVERLAY_W = 0.25         # 새 이미지 너비 (영상 너비 대비)
OVERLAY_MIN_W = 0.02
PRESET_NAME_MAX = 40
BGM_POINTS_MAX = 500     # 음원 하나의 음량 점 최대 수
_BGM_FILE = re.compile(r"[0-9a-f]{16}\.m4a")   # 작업·보관함 폴더 안 음원 파일 이름


def png_size(path: Path) -> tuple[int, int] | None:
    """PNG 머리(IHDR)의 (너비, 높이). PNG 가 아니거나 못 읽으면 None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != PNG_SIG or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h) if w and h else None


def store_png(src: Path, folder: Path) -> str:
    """PNG 를 folder/<sha1 앞 16자>.png 로 복사(이미 있으면 그대로)하고 그 이름을 돌려준다."""
    data = Path(src).read_bytes()
    name = hashlib.sha1(data).hexdigest()[:16] + ".png"
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / name
    if not dst.is_file():
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dst)
    return name


def save_overlays(job: Job, lock=None) -> Path | None:
    """저장용 이미지 목록 <이름>.overlays.json (파이프라인 --overlays). 이미지가 없으면 낡은 파일을 지우고 None.
    job 필드는 lock(queue.lock) 을 쥐고 복사한 뒤 락 밖에서 쓴다."""
    with lock or contextlib.nullcontext():
        src, ovs = Path(job.src), copy.deepcopy(job.overlays)
    jd = paths.job_dir(src)
    path = jd / "out" / f"{src.stem}.overlays.json"
    if not ovs:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [{"path": str(jd / "overlays" / o["img"]), **{k: o[k] for k in ("x", "y", "w", "h", "start", "end") if k in o}}
            for o in ovs]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def save_sequence(job: Job, lock=None) -> Path | None:
    """이어 붙인 조각 목록 <이름>.sequence.json (파이프라인 --sequence). 조각이 없으면 낡은 파일을 지우고 None.
    [{id, clip, src(절대 경로), in, out}] — name 은 파이프라인에 필요 없어 뺀다."""
    with lock or contextlib.nullcontext():
        src, pieces = Path(job.src), copy.deepcopy(job.pieces)
    path = paths.job_dir(src) / "out" / f"{src.stem}.sequence.json"
    if not pieces:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [{"id": p["id"], "clip": p["clip"], "src": str(p["src"]), "in": p["in"], "out": p["out"]} for p in pieces]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


CLIP_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}   # 끼워 넣을 수 있는 영상
STABLE_KEYS = ("word_edits", "line_times", "word_styles")   # 조각을 넣고 뺄 때 전사가 바뀌어 키를 옮기는 자막 수정
EPS = 1e-6


def probe_media(path: Path) -> tuple[float, bool] | None:
    """ffprobe 로 (길이 초, 영상 스트림이 있는가). 앨범 아트(attached_pic)는 영상으로 치지 않는다. 못 읽으면 None."""
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if runner.IS_WIN else {}
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:stream=codec_type:stream_disposition=attached_pic", "-of", "json",
                              str(path)], capture_output=True, text=True, env=runner.build_env(), timeout=30, **kw)
        data = json.loads(res.stdout or "{}")
        dur = float(data["format"]["duration"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    if not math.isfinite(dur) or dur <= 0:
        return None
    video = any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                for s in data.get("streams") or [])
    return dur, video


def store_clip(src: Path, folder: Path) -> Path:
    """영상을 folder/<sha1 앞 16자><확장자 소문자> 로 복사(이미 있으면 그대로)하고 그 경로를 돌려준다.
    sha1 은 조금씩 읽어 계산한다 (큰 영상도 메모리에 올리지 않게)."""
    h = hashlib.sha1()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / (h.hexdigest()[:16] + Path(src).suffix.lower())
    if not dst.is_file():   # 있으면 그대로 둔다 — 수정 시각(파일 키)이 바뀌지 않게
        tmp = dst.with_name(dst.name + ".part")
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
    return dst


def _grid(t: float, fps: Fraction, down: bool = False) -> float:
    """t 초를 fps 프레임 격자에 맞춘다 (down 이면 내림, 아니면 가장 가까운 프레임)."""
    n = float(t) * fps
    return float((math.floor(n + EPS) if down else round(n)) / fps)


def _piece_key(pieces: list) -> list:
    """다시 판정이 필요한 조각 목록의 모양 (이름은 빼고)."""
    return [(p.get("id"), p.get("clip"), str(p.get("src")), float(p.get("in", 0)), float(p.get("out", 0))) for p in pieces]


def _layout(tr: dict | None) -> list:
    """전사가 만들어진 조각 자리 [[id, 시작, 끝], ...] (이어 붙이지 않은 전사면 []). 화면은 이것과 길이가 바뀌면
    소리 줄·썸네일을 다시 읽는다 (넣기·빼기·실행 취소 뒤 asr.wav 가 바뀐다)."""
    out = []
    for p in (tr or {}).get("pieces") or []:
        try:
            out.append([p.get("id"), round(float(p["start"]), 6), round(float(p["end"]), 6)])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _merge_pieces(pieces: list) -> list:
    """이웃한 같은 영상·같은 파일 조각 중 in/out 이 이어진 것은 하나로 합친다 (앞 조각 id 를 남김)."""
    out: list[dict] = []
    for p in pieces:
        q = out[-1] if out else None
        if (q and q.get("clip") == p.get("clip") and str(q.get("src")) == str(p.get("src"))
                and abs(float(q["out"]) - float(p["in"])) <= EPS):
            out[-1] = {**q, "out": p["out"]}
        else:
            out.append(dict(p))
    return out


def _tr_matches_pieces(pieces: list, tr: dict | None) -> bool:
    """디스크의 전사가 지금 조각 목록으로 만든 것인가 (조각이 없으면 이어 붙이지 않은 전사)."""
    want = [p.get("id") for p in pieces if float(p["out"]) - float(p["in"]) > EPS]
    have = [p.get("id") for p in (tr or {}).get("pieces") or []]
    return want == have


WAVE_HOP = 0.01      # 파형 한 값의 길이 (초)
WAVE_FLOOR = -60.0   # 이보다 작은 소리는 0
CUT_AUDIO_WAIT = 10.0   # 분석·저장 전에 그 영상의 미리보기 소리·썸네일 만들기가 멈추길 기다리는 최대 초
THUMBS_STOP_WAIT = 3.0   # 영상을 뺄 때 썸네일 만들기가 멈추길 기다리는 최대 초


def make_execute(store: Store, manifest: dict, data_root: Path, queue_getter: Callable[[], object],
                 cut_audio_getter: Callable[[], "preview.CutAudio | None"] | None = None,
                 thumbs_getter: Callable[[], "thumbs.Thumbs | None"] | None = None):
    """Queue(execute=...) 에 넘긴다. queue_getter 는 Queue 를 만든 뒤에 돌려줄 수 있게 함수로 받는다.
    진행률은 queue.set_progress 로 넘기고(그게 on_change → push("job") 로 이어진다) 작업마다 초당 5번까지만.
    cut_audio_getter 가 주는 CutAudio 의 그 영상 작업은 실행 전에 멈추고(같은 영상 잠금을 쥐고 있으면 파이프라인이
    "이미 처리 중" 으로 멈춘다) 끝나면 다시 받는다. thumbs_getter 의 썸네일 일꾼은 분석(미리보기 영상을 다시 만듦) 때만 멈춘다."""
    def execute(job: Job, kind: str, on_start) -> RunResult:
        th = thumbs_getter() if thumbs_getter and kind == ANALYZE else None
        if th is None:
            return _execute_audio(job, kind, on_start)
        if not th.cancel(job.id, CUT_AUDIO_WAIT):
            log.warning("썸네일 만들기를 %s초 안에 멈추지 못했습니다: %s", CUT_AUDIO_WAIT, job.id)
        try:
            return _execute_audio(job, kind, on_start)
        finally:
            th.release(job.id)

    def _execute_audio(job: Job, kind: str, on_start) -> RunResult:
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
                 cut_audio_run: Callable[[list[str]], RunResult] | None = None,
                 thumbs_run: Callable[[list[str]], RunResult] | None = None):
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
        self._presets_lock = threading.Lock()       # 저장한 이미지 목록(presets.json) 읽고 쓰기
        # 컷을 적용한 미리보기 소리 (cut_audio_run 은 테스트가 가짜를 넣는다)
        self._cut_audio = preview.CutAudio(cut_audio_run or self._run_cut_audio,
                                           lambda job_id: self._push("preview", {"id": job_id}))
        # 타임라인 썸네일 시트 (열어 본 영상만, thumbs_run 은 테스트가 가짜를 넣는다)
        self._thumbs = thumbs.Thumbs(thumbs_run or self._run_thumbs, lambda job_id: self._push("thumbs", {"id": job_id}))

    # ---- 내부 ----------------------------------------------------------------

    def _run_cut_audio(self, cmd: list[str]) -> RunResult:
        with self._queue.lock:
            settings = copy.deepcopy(self._store.settings)
        env = models.pipeline_env(self._manifest, self._data_root, settings)
        return runner.run(cmd, {"preview": 1.0}, lambda p: None, env=env, on_start=self._cut_audio.started)

    def _run_thumbs(self, cmd: list[str]) -> RunResult:
        return runner.run(cmd, {"thumbs": 1.0}, lambda p: None, on_start=self._thumbs.started, low_priority=True)

    def _shutdown(self) -> None:
        """창을 닫을 때: 미리보기 소리·썸네일을 만들던 프로세스를 멈춘다."""
        self._cut_audio.shutdown()
        self._thumbs.shutdown()

    def _job(self, job_id: str) -> Job | None:
        return self._store.get(job_id)

    def _save(self) -> None:
        with self._queue.lock:
            self._store.save()

    # ---- 편집 되돌리기 ---------------------------------------------------------
    # 결과 편집(잘린 곳 되살리기, 구간 작업, 자막 고치기)만 되돌린다. 옵션 변경은 다시 판정이라 넣지 않는다.

    @staticmethod
    def _snap(job: Job) -> tuple:
        return copy.deepcopy((job.restored, job.range_ops, job.word_edits, job.line_times, job.word_styles, job.edits_sha1,
                              job.overlays, job.pieces, job.stable_edits, job.bgms))

    @staticmethod
    def _put(job: Job, snap: tuple) -> None:
        (job.restored, job.range_ops, job.word_edits, job.line_times, job.word_styles, job.edits_sha1,
         job.overlays, job.pieces, job.stable_edits, job.bgms) = copy.deepcopy(snap)

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
        """확인 대기·저장됨에서, 그리고 영상을 넣은 뒤 다시 판정이 실패한 상태(조각이 있는 FAILED)에서도 — 실패한
        넣기를 되돌릴 수 있게. 실패 상태에서 되돌리면 확인 대기로 돌리고 다시 판정한다."""
        with self._queue.lock:
            job = self._job(job_id)
            failed = job is not None and job.status == Status.FAILED and bool(job.pieces)
            if job is None or (job.status not in (Status.REVIEW, Status.SAVED) and not failed):
                return None
            undo, redo = self._history.get(job_id, ([], []))
            src, dst = (redo, undo) if forward else (undo, redo)
            if not src:
                return None
            dst.append(self._snap(job))
            shape = _piece_key(job.pieces)
            self._put(job, src.pop())
            # 영상을 넣거나 뺀 것을 되돌리면(또는 실패 상태면) 이어 붙인 전사를 다시 만든다
            rejudge = failed or _piece_key(job.pieces) != shape
            if job.status in (Status.SAVED, Status.FAILED):
                job.status, job.error = Status.REVIEW, ""
            self._store.save()
            view, hist = job_view(job), self._hist(job_id)
        self._push("job", view)
        if rejudge:
            self._queue.rejudge(job_id)
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
        # 썸네일 ffmpeg 가 미리보기 영상을 연 채로 작업 폴더를 지우지 않게 먼저 멈춘다 (윈도우는 열린 파일을 못 지움)
        self._thumbs.drop(job_id, THUMBS_STOP_WAIT)
        self._queue.cancel(job_id)
        self._cut_audio.drop(job_id)
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

    # ---- 이미지 겹쳐 넣기 ------------------------------------------------------
    # 항목 {id, img, name, aspect, x, y, w, start, end}: x·y·w 는 영상 화면 비율, start/end 는 원본 초.
    # 이미지는 작업 폴더 overlays/ 에 복사해 두고, 저장한 이미지는 <data_root>/overlay_presets/ 에 둔다.

    def _ov_views(self, job: Job) -> list[dict]:
        return [{**o, "url": f"{self._preview_base}/{job.id}/overlays/{o['img']}"} for o in job.overlays]

    def _change_overlays(self, job_id: str, change: Callable[[Job, float], bool]) -> dict | None:
        """이미지 편집 공통: 확인 대기·저장됨에서만. change(job, 영상 길이) 가 False 면 거절. → {"overlays", "history"}."""
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            before = self._snap(job)
            if not change(job, self._duration(job)):
                return None
            self._record(job, before)
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view, out = job_view(job), self._ov_views(job)
        self._push("job", view)
        return {"overlays": out, "history": self._hist(job_id)}

    def _job_overlay_dir(self, job_id: str) -> Path | None:
        with self._queue.lock:
            job = self._job(job_id)
            return paths.job_dir(Path(job.src)) / "overlays" if job else None

    def add_overlay(self, job_id: str, at: float, vid_aspect: float) -> dict | None:
        """PNG 를 골라 재생 위치 at(원본 초)부터 넣는다. vid_aspect = 영상 너비/높이 (화면 가운데에 놓으려고).
        취소·PNG 아님·편집할 수 없는 상태면 None."""
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (at, vid_aspect)) \
                or vid_aspect <= 0:
            return None
        folder = self._job_overlay_dir(job_id)
        if folder is None:
            return None
        picked = self._dialogs.open_image()
        size = png_size(Path(picked)) if picked else None
        if size is None:
            return None
        try:
            img = store_png(Path(picked), folder)
        except OSError:
            log.exception("이미지 복사 실패")
            return None
        aspect = size[0] / size[1]

        def change(job: Job, d: float) -> bool:
            start = min(max(float(at), 0.0), d)
            end = min(start + OVERLAY_SPAN, d)
            if end <= start:
                return False
            # 세로로 긴 그림은 화면 높이 안에 들어오게 줄인다
            w = max(OVERLAY_MIN_W, min(OVERLAY_W, aspect / vid_aspect))
            hr = w * vid_aspect / aspect   # 높이 (영상 높이 대비) — 처음엔 그림 비율대로, 손잡이로 따로 늘릴 수 있다
            y = max(0.0, (1 - hr) / 2)
            job.overlays = [*job.overlays, {"id": secrets.token_hex(4), "img": img, "name": Path(picked).stem,
                                            "aspect": aspect, "x": (1 - w) / 2, "y": y, "w": w, "h": hr,
                                            "start": start, "end": end}]
            return True
        return self._change_overlays(job_id, change)

    def update_overlay(self, job_id: str, oid: str, patch: dict) -> dict | None:
        """x·y·w·h·start·end 만 바꾼다. 0 <= start < end <= 영상 길이, OVERLAY_MIN_W <= w·h <= 1."""
        if not isinstance(patch, dict) or not patch or set(patch) - {"x", "y", "w", "h", "start", "end"}:
            return None
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in patch.values()):
            return None

        def change(job: Job, d: float) -> bool:
            i = next((i for i, o in enumerate(job.overlays) if o["id"] == oid), None)
            if i is None:
                return False
            o = {**job.overlays[i], **{k: float(v) for k, v in patch.items()}}
            if not (0 <= o["start"] < o["end"] <= d + 1e-6 and OVERLAY_MIN_W <= o["w"] <= 1
                    and (o.get("h") is None or OVERLAY_MIN_W <= o["h"] <= 1)
                    and -1 <= o["x"] <= 1 and -1 <= o["y"] <= 1):
                return False
            job.overlays = [*job.overlays[:i], o, *job.overlays[i + 1:]]
            return True
        return self._change_overlays(job_id, change)

    def rename_overlay(self, job_id: str, oid: str, name: str) -> dict | None:
        """이미지 이름을 바꾼다 (앞뒤 공백 제거, 1~PRESET_NAME_MAX 글자)."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None

        def change(job: Job, _d: float) -> bool:
            i = next((i for i, o in enumerate(job.overlays) if o["id"] == oid), None)
            if i is None:
                return False
            job.overlays = [*job.overlays[:i], {**job.overlays[i], "name": name}, *job.overlays[i + 1:]]
            return True
        return self._change_overlays(job_id, change)

    def delete_overlay(self, job_id: str, oid: str) -> dict | None:
        def change(job: Job, _d: float) -> bool:
            kept = [o for o in job.overlays if o["id"] != oid]
            if len(kept) == len(job.overlays):
                return False
            job.overlays = kept
            return True
        return self._change_overlays(job_id, change)

    # 저장한 이미지 (로고): 이미지 + 자리·크기. 다른 영상에 누르면 처음부터 끝까지 들어간다

    def _presets_dir(self) -> Path:
        return self._data_root / "overlay_presets"

    def _read_presets(self) -> list[dict]:
        data = _read_json(self._presets_dir() / "presets.json")
        return [p for p in data if isinstance(p, dict) and isinstance(p.get("id"), str)] if isinstance(data, list) else []

    def _write_presets(self, items: list[dict]) -> None:
        d = self._presets_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "presets.json.tmp"
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "presets.json")

    def _preset_views(self, items: list[dict]) -> list[dict]:
        return [{**p, "url": f"{self._preview_base}/presets/{p['img']}"} for p in items]

    def list_presets(self) -> list[dict]:
        with self._presets_lock:
            return self._preset_views(self._read_presets())

    def save_preset(self, job_id: str, oid: str, name: str) -> list[dict] | None:
        """고른 이미지를 이름 붙여 저장한다 (같은 이름이 있으면 덮어씀). → 저장한 이미지 목록, 안 되면 None."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None
        with self._queue.lock:
            job = self._job(job_id)
            o = next((dict(o) for o in job.overlays if o["id"] == oid), None) if job else None
            src = paths.job_dir(Path(job.src)) / "overlays" if job else None
        if o is None:
            return None
        try:
            img = store_png(src / o["img"], self._presets_dir())
        except OSError:
            log.exception("저장할 이미지 복사 실패")
            return None
        with self._presets_lock:
            items = self._read_presets()
            same = next((p for p in items if p.get("name") == name), None)   # 같은 이름은 덮어쓰되 id 는 그대로
            items = [p for p in items if p is not same]
            items.append({"id": same["id"] if same else secrets.token_hex(4), "name": name, "img": img,
                          **{k: o[k] for k in ("aspect", "x", "y", "w", "h") if k in o}})
            self._write_presets(items)
            return self._preset_views(items)

    def rename_preset(self, pid: str, name: str) -> list[dict] | None:
        """저장한 이미지 이름을 바꾼다. 다른 항목과 같은 이름은 안 된다 (저장할 때 같은 이름을 덮어쓰므로). → 목록, 안 되면 None."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None
        with self._presets_lock:
            items = self._read_presets()
            if not any(p["id"] == pid for p in items) or any(p["id"] != pid and p.get("name") == name for p in items):
                return None
            items = [{**p, "name": name} if p["id"] == pid else p for p in items]
            self._write_presets(items)
            return self._preset_views(items)

    def delete_preset(self, pid: str) -> list[dict]:
        """저장한 이미지를 지운다 (다른 항목이 같은 그림을 쓰지 않으면 그림 파일도)."""
        with self._presets_lock:
            items = self._read_presets()
            gone = [p for p in items if p["id"] == pid]
            items = [p for p in items if p["id"] != pid]
            self._write_presets(items)
            for p in gone:
                if not any(q.get("img") == p.get("img") for q in items):
                    (self._presets_dir() / str(p.get("img"))).unlink(missing_ok=True)
            return self._preset_views(items)

    def insert_preset(self, job_id: str, pid: str) -> dict | None:
        """저장한 이미지를 그 자리·크기로 영상 처음부터 끝까지 넣는다."""
        with self._presets_lock:
            p = next((dict(p) for p in self._read_presets() if p["id"] == pid), None)
        folder = self._job_overlay_dir(job_id)
        if p is None or folder is None:
            return None
        try:
            img = store_png(self._presets_dir() / p["img"], folder)
        except OSError:
            log.exception("저장한 이미지 복사 실패")
            return None

        def change(job: Job, d: float) -> bool:
            if d <= 0:
                return False
            job.overlays = [*job.overlays, {"id": secrets.token_hex(4), "img": img, "name": p["name"],
                                            **{k: float(p[k]) for k in ("aspect", "x", "y", "w", "h") if p.get(k) is not None},
                                            "start": 0.0, "end": d}]
            return True
        return self._change_overlays(job_id, change)

    # ---- 음원(BGM) -------------------------------------------------------------
    # 항목 {id, file, name, dur, gain, at, in, out, points}: at = 결과 초, in/out = 곡 초, points None = 자동 조절.
    # 음원은 작업 폴더 bgm/<16자>.m4a 로 바꿔 두고, 저장한 음원은 <data_root>/bgm_presets/ 에 둔다.

    def _change_bgms(self, job_id: str, change: Callable[[Job, float], bool],
                     ctx: tuple[list, list, float] | None = None) -> dict | None:
        """음원 편집 공통: 확인 대기·저장됨에서만. change(job, 결과 길이) 가 False 면 거절. → {"bgms", "history"}."""
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
        keeps, speech, _ = ctx or self._bgm_context(job)
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            before = self._snap(job)
            if not change(job, bgm.out_total(keeps)):
                return None
            self._record(job, before)
            if job.status == Status.SAVED:
                job.status = Status.REVIEW
            self._store.save()
            view, out = job_view(job), self._bgm_views(job, speech)
        self._push("job", view)
        return {"bgms": out, "history": self._hist(job_id)}

    def _job_bgm_dir(self, job_id: str) -> Path | None:
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            return paths.job_dir(Path(job.src)) / "bgm"

    @staticmethod
    def _bgm_at(at: float, out_total: float, fps) -> float:
        """결과 초 at 을 [0, out_total] 안으로 넣고 프레임 격자에 맞춘다."""
        t = min(max(float(at), 0.0), out_total)
        return min(max(_grid(t, Fraction(fps).limit_denominator(100000)), 0.0), out_total)

    def add_bgm(self, job_id: str, at: float) -> dict | None:
        """음원을 골라 결과 위치 at(초)에 넣는다. 취소·소리 파일 아님·편집할 수 없는 상태면 None."""
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
            return None
        folder = self._job_bgm_dir(job_id)
        if folder is None:
            return None
        picked = self._dialogs.open_audio()
        got = bgm.import_audio(Path(picked), folder) if picked else None
        if got is None:
            return None
        job = self._job(job_id)
        if job is None:
            return None
        ctx = self._bgm_context(job)
        place = self._bgm_at(at, bgm.out_total(ctx[0]), ctx[2])

        def change(job: Job, _d: float) -> bool:
            job.bgms = [*job.bgms, {"id": secrets.token_hex(4), "file": got["file"], "name": Path(picked).stem,
                                    "dur": got["dur"], "gain": got["gain"], "at": place, "in": 0.0, "out": got["dur"],
                                    "points": None}]
            return True
        return self._change_bgms(job_id, change, ctx)

    @staticmethod
    def _num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    def update_bgm(self, job_id: str, bid: str, patch: dict) -> dict | None:
        """at·in·out·points 만 바꾼다. at >= 0, 0 <= in < out <= dur, out - in >= MIN_LEN.
        points = [[곡 초, dB], …] — 곡 초는 [0, dur], dB 는 [DB_MIN, DB_MAX] 로 고정하고 시간순으로 저장 (최대 500개). 주면 손댄 것."""
        if not isinstance(patch, dict) or not patch or set(patch) - {"at", "in", "out", "points"}:
            return None
        if not all(self._num(v) for k, v in patch.items() if k != "points"):
            return None
        raw = patch.get("points")
        if "points" in patch:
            if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= BGM_POINTS_MAX:
                return None
            if not all(isinstance(p, (list, tuple)) and len(p) == 2 and all(self._num(x) for x in p) for p in raw):
                return None

        def change(job: Job, _d: float) -> bool:
            i = next((i for i, b in enumerate(job.bgms) if b["id"] == bid), None)
            if i is None:
                return False
            b = {**job.bgms[i], **{k: float(v) for k, v in patch.items() if k != "points"}}
            if not (b["at"] >= 0 and 0 <= b["in"] < b["out"] <= b["dur"] + EPS and b["out"] - b["in"] >= bgm.MIN_LEN - EPS):
                return False
            if raw is not None:
                b["points"] = sorted(([min(max(float(t), 0.0), b["dur"]), min(max(float(db), bgm.DB_MIN), bgm.DB_MAX)]
                                      for t, db in raw), key=lambda p: p[0])
            job.bgms = [*job.bgms[:i], b, *job.bgms[i + 1:]]
            return True
        return self._change_bgms(job_id, change)

    def reset_bgm(self, job_id: str, bid: str) -> dict | None:
        """음원 자동 조절: 손댄 음량 점을 버리고 말소리에 맞춰 자동으로 따라가게 한다."""
        def change(job: Job, _d: float) -> bool:
            i = next((i for i, b in enumerate(job.bgms) if b["id"] == bid), None)
            if i is None:
                return False
            job.bgms = [*job.bgms[:i], {**job.bgms[i], "points": None}, *job.bgms[i + 1:]]
            return True
        return self._change_bgms(job_id, change)

    def rename_bgm(self, job_id: str, bid: str, name: str) -> dict | None:
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None

        def change(job: Job, _d: float) -> bool:
            i = next((i for i, b in enumerate(job.bgms) if b["id"] == bid), None)
            if i is None:
                return False
            job.bgms = [*job.bgms[:i], {**job.bgms[i], "name": name}, *job.bgms[i + 1:]]
            return True
        return self._change_bgms(job_id, change)

    def delete_bgm(self, job_id: str, bid: str) -> dict | None:
        def change(job: Job, _d: float) -> bool:
            kept = [b for b in job.bgms if b["id"] != bid]
            if len(kept) == len(job.bgms):
                return False
            job.bgms = kept
            return True
        return self._change_bgms(job_id, change)

    # 저장한 음원: [{id, name, file, dur, gain}]. 다른 영상에 누르면 그 위치에 곡 처음부터 끝까지 들어간다

    def _bgm_presets_dir(self) -> Path:
        return self._data_root / "bgm_presets"

    def _read_bgm_presets(self) -> list[dict]:
        data = _read_json(self._bgm_presets_dir() / "presets.json")
        return [p for p in data if isinstance(p, dict) and isinstance(p.get("id"), str)
                and self._num(p.get("dur")) and p["dur"] > 0 and self._num(p.get("gain"))] if isinstance(data, list) else []

    def _write_bgm_presets(self, items: list[dict]) -> None:
        d = self._bgm_presets_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "presets.json.tmp"
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "presets.json")

    def _bgm_preset_views(self, items: list[dict]) -> list[dict]:
        return [{**p, "url": f"{self._preview_base}/bgm_presets/{p['file']}"} for p in items]

    @staticmethod
    def _copy_bgm_file(src: Path, folder: Path) -> None:
        """음원(이름이 곧 내용 해시)을 folder 로 복사. 이미 있으면 그대로 둔다."""
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / src.name
        if not dst.is_file():
            tmp = dst.with_name(dst.name + ".part")
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)

    def list_bgm_presets(self) -> list[dict]:
        with self._presets_lock:
            return self._bgm_preset_views(self._read_bgm_presets())

    def save_bgm_preset(self, job_id: str, bid: str, name: str) -> list[dict] | None:
        """고른 음원을 이름 붙여 저장한다 (같은 이름이 있으면 덮어씀). → 저장한 음원 목록, 안 되면 None."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None
        with self._queue.lock:
            job = self._job(job_id)
            b = next((dict(b) for b in job.bgms if b["id"] == bid), None) if job else None
            src = paths.job_dir(Path(job.src)) / "bgm" if job else None
        if b is None or not _BGM_FILE.fullmatch(str(b["file"])):
            return None
        try:
            self._copy_bgm_file(src / b["file"], self._bgm_presets_dir())
        except OSError:
            log.exception("저장할 음원 복사 실패")
            return None
        with self._presets_lock:
            items = self._read_bgm_presets()
            same = next((p for p in items if p.get("name") == name), None)   # 같은 이름은 덮어쓰되 id 는 그대로
            items = [p for p in items if p is not same]
            items.append({"id": same["id"] if same else secrets.token_hex(4), "name": name, "file": b["file"],
                          "dur": b["dur"], "gain": b["gain"]})
            self._write_bgm_presets(items)
            if same and same.get("file") != b["file"] and not any(q.get("file") == same.get("file") for q in items):
                (self._bgm_presets_dir() / str(same.get("file"))).unlink(missing_ok=True)
            return self._bgm_preset_views(items)

    def rename_bgm_preset(self, pid: str, name: str) -> list[dict] | None:
        """저장한 음원 이름을 바꾼다. 다른 항목과 같은 이름은 안 된다. → 목록, 안 되면 None."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None
        with self._presets_lock:
            items = self._read_bgm_presets()
            if not any(p["id"] == pid for p in items) or any(p["id"] != pid and p.get("name") == name for p in items):
                return None
            items = [{**p, "name": name} if p["id"] == pid else p for p in items]
            self._write_bgm_presets(items)
            return self._bgm_preset_views(items)

    def delete_bgm_preset(self, pid: str) -> list[dict]:
        """저장한 음원을 지운다 (다른 항목이 같은 파일을 쓰지 않으면 파일도)."""
        with self._presets_lock:
            items = self._read_bgm_presets()
            gone = [p for p in items if p["id"] == pid]
            items = [p for p in items if p["id"] != pid]
            self._write_bgm_presets(items)
            for p in gone:
                if _BGM_FILE.fullmatch(str(p.get("file"))) and not any(q.get("file") == p.get("file") for q in items):
                    (self._bgm_presets_dir() / p["file"]).unlink(missing_ok=True)
            return self._bgm_preset_views(items)

    def insert_bgm_preset(self, job_id: str, pid: str, at: float) -> dict | None:
        """저장한 음원을 결과 위치 at(초)에 곡 처음부터 끝까지 넣는다."""
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
            return None
        with self._presets_lock:
            p = next((dict(p) for p in self._read_bgm_presets() if p["id"] == pid), None)
        folder = self._job_bgm_dir(job_id)
        if p is None or folder is None or not _BGM_FILE.fullmatch(str(p.get("file"))):
            return None
        try:
            self._copy_bgm_file(self._bgm_presets_dir() / p["file"], folder)
        except OSError:
            log.exception("저장한 음원 복사 실패")
            return None
        job = self._job(job_id)
        if job is None:
            return None
        ctx = self._bgm_context(job)
        place = self._bgm_at(at, bgm.out_total(ctx[0]), ctx[2])

        def change(job: Job, _d: float) -> bool:
            job.bgms = [*job.bgms, {"id": secrets.token_hex(4), "file": p["file"], "name": p["name"], "dur": p["dur"],
                                    "gain": p["gain"], "at": place, "in": 0.0, "out": p["dur"], "points": None}]
            return True
        return self._change_bgms(job_id, change, ctx)

    # ---- 영상 끼워 넣기 --------------------------------------------------------
    # 조각 {id, clip, src, name, in, out}: 이어 붙인 시간 = 앞 조각들의 (out-in) 합. A(맨 처음 영상)는 clip "main".
    # 넣은 영상은 작업 폴더 clips/ 에 복사한다. 넣고 빼면 편집 시간을 바로 밀고/당기고, 자막 수정 키는
    # stable_edits(전사가 바뀌어도 변하지 않는 키)로 두었다가 다시 판정이 끝나면 새 전사 키로 푼다 (_settle_stable).

    def _clip_views(self, job: Job) -> list[dict]:
        """넣은 영상마다 한 줄 {clip, name, start(첫 조각 시작), end(마지막 조각 끝), dur(조각 길이 합),
        spans(조각마다 [시작, 끝] — 타임라인 띠)}. A 는 뺀다."""
        rows: dict[str, dict] = {}
        pos = 0.0
        for p in job.pieces:
            d = float(p["out"]) - float(p["in"])
            if p.get("clip") != "main":
                r = rows.get(p["clip"])
                if r is None:
                    rows[p["clip"]] = {"clip": p["clip"], "name": p.get("name") or "", "start": pos, "end": pos + d, "dur": d,
                                       "spans": [[pos, pos + d]]}
                else:
                    r["end"], r["dur"] = pos + d, r["dur"] + d
                    r["spans"].append([pos, pos + d])
            pos += d
        return list(rows.values())

    def list_clips(self, job_id: str) -> list[dict]:
        with self._queue.lock:
            job = self._job(job_id)
            return self._clip_views(job) if job else []

    def _clip_inputs(self, job_id: str):
        """(작업 폴더, cuts.json, 전사) — 락 밖에서 읽는다. 없으면 None."""
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            src = Path(job.src)
        out = paths.job_dir(src) / "out"
        cur, tr = _read_json(out / f"{src.stem}.cuts.json"), _read_json(out / f"{src.stem}.transcript.json")
        if cur is None or tr is None:
            return None
        return paths.job_dir(src), cur, tr

    def _can_restructure(self, job: Job | None, tr: dict) -> bool:
        """넣기·빼기를 해도 되는가 (queue.lock 안): 확인 대기·저장됨이고, 디스크의 전사가 지금 조각 목록 것이며,
        지난 넣기·빼기의 자막 키를 다 풀었고, 자막 수정이 그 전사 기준일 때."""
        if job is None or job.status not in (Status.REVIEW, Status.SAVED) or not _tr_matches_pieces(job.pieces, tr):
            return False
        self._settle_stable(job, tr)
        if job.stable_edits is not None:
            return False
        return job.edits_sha1 is None or job.edits_sha1 == subtitles.transcript_sha1(tr)

    def _restructure(self, job: Job, before: tuple, pieces: list, e: dict, stable: dict) -> dict:
        """넣기·빼기 공통 마무리 (queue.lock 안): 밀거나 당긴 편집 e 와 새 조각 목록, 자막 수정의 stable 키를 넣고
        실행 취소에 쌓는다. 실패 상태였으면 확인 대기로 돌린다 (다시 판정하려고). → {"clips", "history"}"""
        job.stable_edits = stable
        job.pieces = pieces
        job.restored, job.range_ops, job.overlays, job.line_times = e["restored"], e["range_ops"], e["overlays"], e["line_times"]
        self._record(job, before)
        if job.status in (Status.SAVED, Status.FAILED):
            job.status, job.error = Status.REVIEW, ""
        self._store.save()
        return {"clips": self._clip_views(job), "history": self._hist(job.id)}

    def add_clip(self, job_id: str, at: float) -> dict | None:
        """영상을 골라 이어 붙인 시간 at 에 끼워 넣는다. at 은 결과의 프레임 격자에 맞추고, 잘린 구간 안이면 그 구간 시작으로 당긴다.
        넣은 영상 길이는 프레임 격자로 내린다. 넣은 뒤 다시 판정한다. → {"clips", "history"}.
        취소·영상 아님·소리만 있는 파일(넣는 쪽이든 받는 쪽이든)·편집할 수 없는 상태면 None."""
        inputs = self._insert_inputs(job_id, at)
        if inputs is None:
            return None
        picked = self._dialogs.open_video()
        if not picked or Path(picked).suffix.lower() not in CLIP_EXTS:
            return None
        return self._insert_file(job_id, at, inputs, Path(picked), Path(picked).stem)

    def _insert_inputs(self, job_id: str, at):
        """넣기 전 확인: at 이 수이고, 넣을 수 있는 상태이며, 받는 쪽이 영상일 때 (작업 폴더, cuts.json, 전사). 아니면 None."""
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
            return None
        inputs = self._clip_inputs(job_id)
        if inputs is None:
            return None
        with self._queue.lock:
            job = self._job(job_id)
            if not self._can_restructure(job, inputs[2]):
                return None
            src = Path(job.src)
        a_probe = probe_media(src)
        if a_probe is None or not a_probe[1]:
            return None
        return inputs

    def _insert_file(self, job_id: str, at: float, inputs, picked: Path, name: str,
                     preset: dict | None = None) -> dict | None:
        """add_clip·insert_clip_preset 공통: picked 를 작업 폴더 clips/ 로 복사해 at 에 끼워 넣고 다시 판정한다.
        preset = {"dir": 보관함 항목 폴더, "stem": A 이름, "edits": 보관한 편집} 이면 분석 캐시를 그 복사본 서명으로 옮겨 오고
        (전사를 다시 하지 않게) 보관한 편집을 넣은 자리에 합친다 (이미지 파일도 작업 폴더 overlays/ 로)."""
        jd, cur, tr = inputs
        b_probe = probe_media(picked)
        if b_probe is None or not b_probe[1]:
            return None
        fps = subtitles._fps(cur)
        d = _grid(b_probe[0], fps, down=True)
        if d <= 0:
            return None
        try:
            clip_path = store_clip(picked, jd / "clips")
        except OSError:
            log.exception("영상 복사 실패")
            return None
        saved = None
        if preset is not None:
            saved = copy.deepcopy(preset.get("edits") or {})
            try:
                self._adopt_preset_cache(preset["dir"], jd / "work" / preset["stem"], clip_path)
                for o in saved.get("overlays") or []:
                    o["img"] = store_png(preset["dir"] / o["img"], jd / "overlays")
            except (OSError, KeyError, TypeError):
                log.exception("보관한 영상의 캐시·이미지 복사 실패")
                saved["overlays"] = []
        with self._queue.lock:
            job = self._job(job_id)
            if not self._can_restructure(job, tr):
                return None
            pieces = copy.deepcopy(job.pieces) or [{"id": secrets.token_hex(4), "clip": "main", "src": job.src,
                                                    "name": Path(job.src).stem, "in": 0.0, "out": _grid(self._duration(job), fps)}]
            total = sum(float(p["out"]) - float(p["in"]) for p in pieces)
            cuts = _union(diff.effective_cuts(cur, job.restored, job.range_ops, tr).get("cuts", []))
            t = sequence.snap_at(at, cuts, sequence.piece_bounds(pieces), fps, total)
            before = self._snap(job)
            e = sequence.shift_for_insert({k: getattr(job, k) for k in sequence.EDIT_KEYS}, t, d)
            new = {"id": secrets.token_hex(4), "clip": secrets.token_hex(4), "src": str(clip_path),
                   "name": name, "in": 0.0, "out": d}
            stable = {k: sequence.to_stable(e[k], tr) for k in STABLE_KEYS}
            pieces = sequence.split_piece(pieces, t, new)
            if saved:
                placed = sequence.place_clip(saved, sequence.clip_spans(pieces, new["clip"]), new["clip"])
                for o in placed["overlays"]:          # 같은 보관 영상을 두 번 넣어도 이미지 id 가 겹치지 않게
                    o["id"] = secrets.token_hex(4)
                for k in ("restored", "range_ops", "overlays"):
                    e[k] = [*e[k], *placed[k]]
                for k in STABLE_KEYS:                 # 자막 수정은 stable 키로 — 다시 판정 뒤 새 전사 키로 푼다
                    stable[k].update(placed[k])
            result = self._restructure(job, before, pieces, e, stable)
        self._queue.rejudge(job_id)   # 상태 변화는 대기열이 알린다
        return result

    # ---- 영상 보관함 ----------------------------------------------------------
    # <data_root>/clip_presets/presets.json: [{id, name, file, dur, edits}]. 영상은 <id>/<sha16><ext>,
    # 분석 캐시는 <id>/files/<fkey>/ (보관함 복사본 서명으로 옮긴 것), edits 는 sequence.extract_clip 결과(파일 안 초),
    # edits 의 이미지 파일도 <id>/ 에 둔다.

    def _clip_presets_dir(self) -> Path:
        return self._data_root / "clip_presets"

    def _read_clip_presets(self) -> list[dict]:
        data = _read_json(self._clip_presets_dir() / "presets.json")
        return [p for p in data if isinstance(p, dict) and isinstance(p.get("id"), str)
                and isinstance(p.get("file"), str)] if isinstance(data, list) else []

    def _write_clip_presets(self, items: list[dict]) -> None:
        d = self._clip_presets_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "presets.json.tmp"
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "presets.json")

    @staticmethod
    def _adopt_preset_cache(pdir: Path, work: Path, clip_path: Path) -> bool:
        """보관함 항목의 분석 캐시를 작업의 파일별 캐시(work/files/<fkey>/, work = 작업 폴더 work/<A 이름>)로 옮긴다
        (키는 작업 복사본 서명으로). 작업 쪽에 이미 캐시가 있으면 그대로 둔다 (같은 영상을 또 넣을 때)."""
        pl = subtitles.pipeline_module()
        pfile = (pdir / clip_path.name).resolve()   # 파일 키는 파이프라인(load_sequence)처럼 resolve 한 경로로
        clip_path = clip_path.resolve()
        if not pfile.is_file():
            return False
        dst = pl.file_work(work, pl.seq.file_key(clip_path))
        if (dst / "audio.meta.json").exists():
            return False
        return pl.rebase_file_cache(pdir / "files" / pl.seq.file_key(pfile), dst, clip_path)

    def list_clip_presets(self) -> list[dict]:
        with self._presets_lock:
            return self._read_clip_presets()

    def save_clip_preset(self, job_id: str, clip: str, name: str) -> list[dict] | None:
        """넣은 영상을 그 안의 편집(자막 수정·직접 자르기·되살리기·그 안의 이미지)과 함께 보관함에 저장한다.
        같은 파일이 이미 보관함에 있으면 그 항목의 편집·이름을 바꾼다. 다른 항목과 같은 이름은 안 된다.
        분석 캐시가 있으면 보관함 복사본 서명으로 옮겨 같이 둔다. 편집할 수 있는 상태(다시 판정이 끝나고 자막 키를 다 푼 뒤)에서만.
        → 보관함 목록, 안 되면 None."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX or not isinstance(clip, str) or clip == "main":
            return None
        inputs = self._clip_inputs(job_id)
        if inputs is None:
            return None
        jd, cur, tr = inputs
        with self._queue.lock:
            job = self._job(job_id)
            if not self._can_restructure(job, tr):
                return None
            spans = sequence.clip_spans(job.pieces, clip)
            if not spans:
                return None
            src = Path(next(p["src"] for p in job.pieces if p.get("clip") == clip))
            e = {k: copy.deepcopy(getattr(job, k)) for k in sequence.EDIT_KEYS}
            stable = {k: sequence.to_stable(getattr(job, k), tr) for k in STABLE_KEYS}
            work = jd / "work" / Path(job.src).stem
        saved = sequence.extract_clip(e, stable, spans, clip)
        probe = probe_media(src)
        if probe is None:
            return None
        dur = _grid(probe[0], subtitles._fps(cur), down=True)
        pl = subtitles.pipeline_module()
        with self._presets_lock:
            items = self._read_clip_presets()
            same = next((p for p in items if p["file"] == src.name), None)
            if any(p is not same and p.get("name") == name for p in items):
                return None
            pid = same["id"] if same else secrets.token_hex(4)
            pdir = self._clip_presets_dir() / pid
            pfile = pdir / src.name
            try:
                if not pfile.is_file():
                    pdir.mkdir(parents=True, exist_ok=True)
                    tmp = pdir / (src.name + ".part")
                    shutil.copy2(src, tmp)   # 수정 시각을 남긴다 (보관함 쪽 파일 키가 바뀌지 않게)
                    os.replace(tmp, pfile)
                rsrc, rpfile = src.resolve(), pfile.resolve()   # 파이프라인(load_sequence)과 같은 파일 키
                pl.rebase_file_cache(pl.file_work(work, pl.seq.file_key(rsrc)),
                                     pdir / "files" / pl.seq.file_key(rpfile), rpfile)
            except OSError:
                log.exception("보관할 영상 복사 실패")
                return None
            ovs = []
            for o in saved["overlays"]:
                try:
                    ovs.append({**o, "img": store_png(jd / "overlays" / o["img"], pdir)})
                except (OSError, KeyError):
                    log.exception("보관할 영상의 이미지 복사 실패")
            saved["overlays"] = ovs
            entry = {"id": pid, "name": name, "file": src.name, "dur": dur, "edits": saved}
            items = [entry if p is same else p for p in items] if same else [*items, entry]
            self._write_clip_presets(items)
            return items

    def rename_clip_preset(self, pid: str, name: str) -> list[dict] | None:
        """보관한 영상 이름을 바꾼다. 다른 항목과 같은 이름은 안 된다. → 목록, 안 되면 None."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX:
            return None
        with self._presets_lock:
            items = self._read_clip_presets()
            if not any(p["id"] == pid for p in items) or any(p["id"] != pid and p.get("name") == name for p in items):
                return None
            items = [{**p, "name": name} if p["id"] == pid else p for p in items]
            self._write_clip_presets(items)
            return items

    def delete_clip_preset(self, pid: str) -> list[dict]:
        """보관한 영상을 지운다 (영상·분석 캐시·이미지가 든 그 항목 폴더째)."""
        with self._presets_lock:
            items = self._read_clip_presets()
            gone = [p for p in items if p["id"] == pid]
            items = [p for p in items if p["id"] != pid]
            self._write_clip_presets(items)
            for p in gone:
                d = self._clip_presets_dir() / p["id"]
                if d.parent == self._clip_presets_dir():   # id 가 경로를 벗어나지 않게
                    shutil.rmtree(d, ignore_errors=True)
            return items

    def insert_clip_preset(self, job_id: str, pid: str, at: float) -> dict | None:
        """보관한 영상을 이어 붙인 시간 at 에 끼워 넣는다 (add_clip 과 같은 흐름). 분석 캐시를 작업 폴더로 옮겨 오므로
        다시 판정 때 전사를 다시 하지 않고, 보관한 편집이 넣은 자리에 따라온다. → {"clips", "history"}, 안 되면 None."""
        inputs = self._insert_inputs(job_id, at)
        if inputs is None:
            return None
        with self._presets_lock:
            p = next((copy.deepcopy(p) for p in self._read_clip_presets() if p["id"] == pid), None)
        if p is None:
            return None
        pdir = self._clip_presets_dir() / p["id"]
        pfile = pdir / p["file"]
        if pdir.parent != self._clip_presets_dir() or not pfile.is_file():
            return None
        with self._queue.lock:
            job = self._job(job_id)
            if job is None:
                return None
            stem = Path(job.src).stem
        return self._insert_file(job_id, at, inputs, pfile, str(p.get("name") or pfile.stem),
                                 {"dir": pdir, "stem": stem, "edits": p.get("edits")})

    def remove_clip(self, job_id: str, clip: str) -> dict | None:
        """넣은 영상(그 조각 모두)을 뺀다. 그 안의 편집은 사라지고 뒤쪽은 당긴다. A("main")는 뺄 수 없다.
        뺀 자리에서 이어지게 된 같은 파일 조각은 하나로 합친다. 뺀 뒤 다시 판정한다.
        넣은 뒤 다시 판정이 실패한 상태(조각이 있는 FAILED)에서도 뺄 수 있다 — 그땐 디스크 전사가 조각 목록과 맞지 않으므로
        아직 풀지 않은 stable_edits 를 그대로 이어 쓴다 (줄 시간만 당김)."""
        if not isinstance(clip, str) or clip == "main":
            return None
        inputs = self._clip_inputs(job_id)
        if inputs is None:
            return None
        _, _, tr = inputs
        with self._queue.lock:
            job = self._job(job_id)
            failed = job is not None and job.status == Status.FAILED and bool(job.pieces)
            if not failed and not self._can_restructure(job, tr):
                return None
            spans = sequence.clip_spans(job.pieces, clip)
            if not spans:
                return None
            before = self._snap(job)
            cut = [(a, b) for a, b, _ in spans]
            e = sequence.shift_for_remove({k: getattr(job, k) for k in sequence.EDIT_KEYS}, cut)
            if job.stable_edits is not None:   # 실패 상태: 지난 넣기·빼기의 stable 키를 그대로, 줄 시간만 당긴다
                st = job.stable_edits
                stable = {"word_edits": copy.deepcopy(st.get("word_edits") or {}),
                          "word_styles": copy.deepcopy(st.get("word_styles") or {}),
                          "line_times": sequence.shift_for_remove({"line_times": st.get("line_times") or {}}, cut)["line_times"]}
            else:
                stable = {k: sequence.to_stable(e[k], tr) for k in STABLE_KEYS}
            pieces = _merge_pieces([p for p in job.pieces if p.get("clip") != clip])
            result = self._restructure(job, before, pieces, e, stable)
        self._queue.rejudge(job_id)
        return result

    def move_clip(self, job_id: str, clip: str, at: float) -> dict | None:
        """넣은 영상 덩어리(갈라 넣은 영상 포함)를 이어 붙인 시간 at 으로 옮긴다. at 은 넣기와 같이 맞추고(영상 경계 우선),
        덩어리 안이면 거절(None), 제자리면 아무것도 바꾸지 않는다. 안 편집은 따라가고 바깥은 밀리며, 빠진 자리에서 이어진
        같은 파일 조각은 합친다. 옮긴 뒤 다시 판정한다. A("main")는 못 옮긴다. → {"clips", "history"}"""
        if not isinstance(clip, str) or clip == "main":
            return None
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
            return None
        inputs = self._clip_inputs(job_id)
        if inputs is None:
            return None
        _, cur, tr = inputs
        with self._queue.lock:
            job = self._job(job_id)
            if not self._can_restructure(job, tr):
                return None
            span = sequence.block_span(job.pieces, clip)
            if span is None:
                return None
            s, end = span
            fps = subtitles._fps(cur)
            total = sum(float(p["out"]) - float(p["in"]) for p in job.pieces)
            cuts = _union(diff.effective_cuts(cur, job.restored, job.range_ops, tr).get("cuts", []))
            t = sequence.snap_at(at, cuts, sequence.piece_bounds(job.pieces), fps, total)
            if s + EPS < t < end - EPS:
                return None
            if abs(t - s) <= EPS or abs(t - end) <= EPS:   # 제자리
                return {"clips": self._clip_views(job), "history": self._hist(job.id)}
            before = self._snap(job)
            pieces, t2 = sequence.move_block(copy.deepcopy(job.pieces), clip, t)
            e = sequence.shift_for_move({k: getattr(job, k) for k in sequence.EDIT_KEYS}, s, end, t2)
            stable = {k: sequence.to_stable(e[k], tr) for k in STABLE_KEYS}
            result = self._restructure(job, before, _merge_pieces(pieces), e, stable)
        self._queue.rejudge(job_id)
        return result

    def rename_clip(self, job_id: str, clip: str, name: str) -> dict | None:
        """넣은 영상 이름을 바꾼다 (앞뒤 공백 제거, 1~PRESET_NAME_MAX 글자). 다시 판정하지 않는다."""
        name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(name) <= PRESET_NAME_MAX or clip == "main":
            return None
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED) \
                    or not any(p.get("clip") == clip for p in job.pieces):
                return None
            before = self._snap(job)
            job.pieces = [{**p, "name": name} if p.get("clip") == clip else p for p in job.pieces]
            self._record(job, before)
            self._store.save()
            return {"clips": self._clip_views(job), "history": self._hist(job_id)}

    # ---- 자막 고치기 ----------------------------------------------------------

    def _settle_stable(self, job: Job, tr: dict) -> bool:
        return settle_stable(job, tr, self._store)

    def _drop_stale_edits(self, job: Job, tr: dict) -> bool:
        """고친 자막을 만들 때의 받아쓰기 지문이 지금과 다르면 어절·줄 시간 수정·글자 꾸밈을 버린다 (구간 작업은 시간 기준이라 남김).
        버렸으면 True. queue.lock 을 쥔 채 부른다. 넣기·빼기로 바뀐 전사면 버리지 않고 키를 옮긴다 (_settle_stable, 역사도 남김).
        처리 중이거나 디스크의 전사가 아직 지금 조각 목록 것이 아니면(실행 취소 직후 등) 아무것도 하지 않는다."""
        if job.status == Status.PROCESSING or not _tr_matches_pieces(job.pieces, tr):
            return False
        if self._settle_stable(job, tr):
            return False
        if job.edits_sha1 is None or job.edits_sha1 == subtitles.transcript_sha1(tr):
            return False
        job.word_edits, job.line_times, job.word_styles, job.edits_sha1 = {}, {}, {}, None
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
            styles = copy.deepcopy(job.word_styles)
        cuts = diff.effective_cuts(cuts, restored, ops, tr)
        try:
            caps = subtitles.captions(cuts, tr, subtitles.load_cfg(), edits, times, styles)
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
            has = job.word_edits or job.line_times or job.word_styles
            if job.edits_sha1 is None and has:
                job.edits_sha1 = subtitles.transcript_sha1(tr)
            elif not has:
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

    def set_line(self, job_id: str, key: str, text: str | None) -> dict | None:
        """자막 한 줄 글자를 통째로 고친다 (subtitles.plan_line 으로 바뀐 어절만). 글자 꾸밈은 고친 글자에 옮긴다.
        text 가 None 이면 그 줄 어절을 모두 원래 글자로."""
        if not isinstance(key, str) or not (text is None or (isinstance(text, str) and len(text) <= 2000)):
            return None
        st = self._caption_state_ex(job_id)
        if st is None:
            return None
        line = next((c for c in st[3] if c["key"] == key), None)
        if line is None:
            return None
        words = [{"key": w["key"], "text": w["text"]} for w in line["words"]]

        def change(job: Job, tr: dict) -> bool:
            origs = subtitles.transcript_words(tr)
            if text is None:
                for w in words:
                    job.word_edits.pop(w["key"], None)
                return True
            changes, restyled = subtitles.plan_line(words, text, job.word_styles)
            for k, v in changes.items():
                if v == origs.get(k):
                    job.word_edits.pop(k, None)
                else:
                    job.word_edits[k] = v
                if k in restyled:
                    if restyled[k]:
                        job.word_styles[k] = restyled[k]
                    else:
                        job.word_styles.pop(k, None)
            return True
        return self._edit_subs(job_id, change)

    def set_style(self, job_id: str, ranges: list, style: dict | None) -> dict | None:
        """고른 글자를 꾸민다. ranges = [[어절 키, 시작 글자, 끝 글자(빼고)], ...] (글자 = 유니코드 코드 포인트),
        style = {font?, size?, color?, bold?}: 준 항목만 바꾸고 값이 None 이면 그 항목을 지운다. style 이 None 이면 꾸밈을 모두 지운다."""
        pl = subtitles.pipeline_module()
        if not isinstance(ranges, list) or not ranges or len(ranges) > 2000:
            return None
        if style is not None:
            if not isinstance(style, dict) or not style or set(style) - set(subtitles.STYLE_KEYS):
                return None
            sets = pl.clean_style({k: v for k, v in style.items() if v is not None}) or {}
            # 기본값으로 되돌리는 값(size 1.0, bold False)은 지우기로 본다
            drops = {k for k in style if k not in sets}
        cleaned = []
        for r in ranges:
            if (not isinstance(r, (list, tuple)) or len(r) != 3 or not isinstance(r[0], str)
                    or not all(isinstance(x, int) and not isinstance(x, bool) for x in r[1:]) or not 0 <= r[1] < r[2] <= 1000):
                return None
            cleaned.append((r[0], r[1], r[2]))
        st = self._caption_state_ex(job_id)
        if st is None:
            return None
        texts = {w["key"]: w["text"] for c in st[3] for w in c["words"]}

        def change(job: Job, _tr: dict) -> bool:
            for key, a, b in cleaned:
                if key not in texts:
                    return False
                b = min(b, len(texts[key]))
                chars = list(job.word_styles.get(key) or [])
                chars += [None] * (b - len(chars))
                for i in range(a, b):
                    cur = dict(chars[i] or {})
                    if style is None:
                        cur = {}
                    else:
                        for k in drops:
                            cur.pop(k, None)
                        cur.update(sets)
                    chars[i] = cur or None
                while chars and chars[-1] is None:
                    chars.pop()
                if chars:
                    job.word_styles[key] = chars
                else:
                    job.word_styles.pop(key, None)
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
            effective = _effective_cuts(cur, restored, ops, transcript)
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
        ov_views = self._ov_views(snap)
        keeps, speech, _ = bgm_context_of(cur, transcript, duration)
        result = {"summary": summary, "cuts": diff.cut_ranges(cur) if cur else [], "cut_items": items,
                  "range_ops": ops, "history": self._hist(job_id),
                  "duration": duration, "fps": float(subtitles._fps(cur or {})),   # 화면의 프레임 단위 구간 다듬기
                  "overlays": ov_views, "layout": _layout(transcript),
                  "bgms": self._bgm_views(snap, speech), "keeps": keeps, "out_total": bgm.out_total(keeps)}
        result.update(self._preview(snap, status, effective, settings))
        if status in (Status.REVIEW, Status.SAVED):
            self._request_thumbs(snap, duration)
        if dropped:
            result["edits_dropped"] = True
        if status == Status.SAVED:
            names = [f"{src.stem}_clean.mp4", f"{src.stem}_clean.m4a", f"{src.stem}_clean.srt"]
            result["saved_paths"] = [str(save_dir / n) for n in names if (save_dir / n).is_file()]
        return result

    def _bgm_context(self, job: Job) -> tuple[list, list, float]:
        """지금 effective 컷·전사로 (keeps, speech, fps). get_result 와 같은 컷을 쓴다."""
        with self._queue.lock:
            src, summary = Path(job.src), job.summary
            restored, ops = [list(r) for r in job.restored], [dict(o) for o in job.range_ops]
        return _bgm_inputs(src, restored, ops, summary)

    def _bgm_views(self, job: Job, speech: list) -> list[dict]:
        return [{**b, "url": f"{self._preview_base}/{job.id}/bgm/{b['file']}",
                 "points": bgm.resolve_points(b, speech), "touched": b.get("points") is not None} for b in job.bgms]

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
            key = preview.cut_key(cuts.get("cuts", []), job.options, _piece_key(job.pieces) if job.pieces else None)
            target = pdir / f"audio_cut.{key}.m4a"
            if status in (Status.REVIEW, Status.SAVED):
                stem = Path(job.src).stem
                cuts_path = jd / "out" / f"{stem}.preview.cuts.json"
                cmd = runner.preview_audio_command(job, models.paths(self._manifest, self._data_root),
                                                   models.device(settings, self._manifest), cuts_path, target,
                                                   save_sequence(job))

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

    # ---- 타임라인 썸네일 ------------------------------------------------------

    @staticmethod
    def _preview_size(video: Path) -> tuple[int, int] | None:
        """미리보기 영상의 가로·세로. ffprobe 결과를 thumbs/size.json 에 영상 서명과 함께 남긴다."""
        cache = video.parent / "thumbs" / "size.json"
        try:
            sig = thumbs.video_sig(video)
        except OSError:
            return None
        c = _read_json(cache)
        if c and c.get("video") == sig and c.get("w") and c.get("h"):
            return int(c["w"]), int(c["h"])
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if runner.IS_WIN else {}
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=width,height", "-of", "csv=p=0", str(video)], capture_output=True, text=True,
                                 env=runner.build_env(), timeout=30, **kw).stdout.strip()
            w, h = (int(x) for x in out.split(",")[:2])
        except (OSError, ValueError, subprocess.SubprocessError):
            log.warning("미리보기 영상 크기를 읽지 못했습니다: %s", video)
            return None
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"video": sig, "w": w, "h": h}), encoding="utf-8")
        except OSError:
            pass
        return w, h

    def _thumbs_meta(self, job: Job, duration: float, request: bool) -> dict | None:
        video = paths.job_dir(Path(job.src)) / "preview" / "video.mp4"
        if duration <= 0 or not video.is_file():
            return None
        size = self._preview_size(video)
        if size is None:
            return None
        if request:
            return self._thumbs.request(job.id, video, video.parent / "thumbs", duration, size)
        try:
            return thumbs.load_meta(video.parent / "thumbs", video, thumbs.plan(duration, *size))
        except OSError:
            return None

    def _request_thumbs(self, job: Job, duration: float) -> None:
        """결과를 열어 본 영상의 썸네일 시트를 뒤에서 만들기 시작한다 (다른 영상 것은 멈춘다)."""
        try:
            self._thumbs_meta(job, duration, request=True)
        except OSError:
            log.exception("썸네일 요청 실패: %s", job.id)

    def get_thumbs(self, job_id: str) -> dict | None:
        """타임라인 썸네일 배치와 시트 주소 (아직 없는 시트는 None). 미리보기 영상이 없으면 None."""
        with self._queue.lock:
            job = self._job(job_id)
            snap = copy.deepcopy(job) if job else None
        if snap is None:
            return None
        meta = self._thumbs_meta(snap, self._duration(snap), request=False)
        if meta is None:
            return None
        d = paths.job_dir(Path(snap.src)) / "preview" / "thumbs"
        base = f"{self._preview_base}/{snap.id}/preview/thumbs"
        done = set(meta["done"])
        sheets = [_preview_url(f"{base}/{thumbs.sheet_name(i)}", d / thumbs.sheet_name(i)) if i in done else None
                  for i in range(meta["sheets"])]
        return {k: meta[k] for k in ("step", "tile_w", "tile_h", "cols", "rows", "per_sheet", "count")} | {"sheets": sheets}

    def get_waveform(self, job_id: str) -> dict | None:
        """타임라인 소리 줄: 분석용 오디오의 10ms 에너지를 dB 로 바꿔 [floor, 0] 을 0~255 한 바이트씩 (base64).
        preview/wave.bin 에 asr.wav 서명과 함께 캐시한다. 확인 대기·저장됨이 아니거나 asr.wav 가 없으면 None."""
        with self._queue.lock:
            job = self._job(job_id)
            if job is None or job.status not in (Status.REVIEW, Status.SAVED):
                return None
            src = Path(job.src)
        jd = paths.job_dir(src)
        wav = jd / "work" / src.stem / "asr.wav"
        bin_path, meta_path = jd / "preview" / "wave.bin", jd / "preview" / "wave.meta.json"
        try:
            st = wav.stat()
        except OSError:
            return None
        sig = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "floor": WAVE_FLOOR}
        data = None
        if _read_json(meta_path) == sig:
            try:
                data = bin_path.read_bytes()
            except OSError:
                data = None
        if data is None:
            import numpy as np
            try:
                rms, _hop = subtitles.pipeline_module().load_rms(wav, WAVE_HOP)
            except (OSError, EOFError, ValueError) as e:
                log.warning("파형을 만들지 못했습니다: %s", e)
                return None
            db = 20 * np.log10(np.maximum(np.asarray(rms, dtype=np.float64), 1e-6))
            data = np.round((np.clip(db, WAVE_FLOOR, 0.0) - WAVE_FLOOR) / -WAVE_FLOOR * 255).astype(np.uint8).tobytes()
            try:
                bin_path.parent.mkdir(parents=True, exist_ok=True)
                bin_path.write_bytes(data)
                meta_path.write_text(json.dumps(sig), encoding="utf-8")
            except OSError:
                pass
        return {"hop": WAVE_HOP, "floor": WAVE_FLOOR, "data": base64.b64encode(data).decode("ascii")}

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
