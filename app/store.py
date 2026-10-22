"""작업 목록과 설정을 JSON 으로 저장/복원."""
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .options import JobOptions

log = logging.getLogger(__name__)
JOB_ID = re.compile(r"[0-9a-f]{12}")


class Status:
    WAITING = "waiting"
    PROCESSING = "processing"
    REVIEW = "review"
    SAVING = "saving"
    SAVED = "saved"
    FAILED = "failed"
    MISSING = "missing"


@dataclass
class Job:
    id: str
    src: str
    options: JobOptions = field(default_factory=JobOptions)
    status: str = Status.WAITING
    progress: float = 0.0
    error: str = ""
    summary: dict | None = None   # {"orig_sec", "final_sec", "cuts", "removed_sec"}
    before_missing: str | None = None  # 원본 없음이 되기 전 상태 (원본이 돌아오면 되돌린다)
    restored: list = field(default_factory=list)  # 사용자가 되살린 컷 [[시작, 끝], ...] (diff.is_restored)
    word_edits: dict = field(default_factory=dict)  # 고친 자막 어절 {"seg:i": 글자}
    line_times: dict = field(default_factory=dict)  # 직접 맞춘 자막 줄 시간 {"seg:i": [시작, 끝]} (원본 초)
    range_ops: list = field(default_factory=list)   # 직접 자르기·남기기 구간 작업
    edits_sha1: str | None = None                   # 자막을 고칠 때의 받아쓰기 지문

    def to_dict(self) -> dict:
        d = asdict(self)
        d["options"] = self.options.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        if not isinstance(d.get("id"), str) or not isinstance(d.get("src"), str) or not d["src"]:
            raise ValueError(f"id/src 가 없는 작업: {d!r}")
        d["options"] = JobOptions.from_dict(d.get("options") or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def _results_cached(self) -> bool:
        src = Path(self.src)
        return (paths.job_dir(src) / "out" / f"{src.stem}.cuts.json").is_file()


def _default_settings() -> dict:
    return {"save_dir": str(paths.default_save_dir()), "dismissed_version": None, "gpu": None}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.jobs: list[Job] = []
        self.settings: dict = _default_settings()

    def get(self, job_id: str) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, src: Path) -> Job | None:
        job_id = paths.job_dir(src).name
        if self.get(job_id):
            return None
        job = Job(id=job_id, src=os.path.abspath(src))
        self.jobs.append(job)
        return job

    def remove(self, job_id: str) -> None:
        self.jobs = [j for j in self.jobs if j.id != job_id]

    def refresh_missing(self, mark_new: bool = True, skip: set | frozenset = frozenset()) -> list[Job]:
        """원본이 다시 생긴 `원본 없음` 영상을 그 전 상태로 되돌리고, mark_new 면 원본이 사라진 영상을
        `원본 없음` 으로 바꾼다 (처리·저장 중이거나 skip 에 든 영상은 건드리지 않는다). 바뀐 영상 목록."""
        changed = []
        for job in self.jobs:
            if job.id in skip or job.status in (Status.PROCESSING, Status.SAVING):
                continue
            exists = Path(job.src).exists()
            if job.status == Status.MISSING and exists:
                status = job.before_missing or Status.WAITING
                if status in (Status.REVIEW, Status.SAVED) and not job._results_cached():
                    status = Status.WAITING
                job.status, job.before_missing, job.progress = status, None, (
                    1.0 if status in (Status.REVIEW, Status.SAVED) else 0.0)
                changed.append(job)
            elif mark_new and job.status != Status.MISSING and not exists:
                job.before_missing, job.status = job.status, Status.MISSING
                changed.append(job)
        return changed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"settings": self.settings, "jobs": [j.to_dict() for j in self.jobs]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> "Store":
        store = cls(path)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            settings = {**_default_settings(), **(data.get("settings") or {})}
            raw_jobs = data.get("jobs") or []
            if not isinstance(raw_jobs, list):
                raise TypeError("jobs 가 목록이 아님")
        except (OSError, ValueError, TypeError, AttributeError):
            return store
        store.settings = settings
        jobs, seen = [], set()
        for d in raw_jobs:   # 잘못된 항목 하나 때문에 목록 전체를 잃지 않게 하나씩 읽는다
            try:
                job = Job.from_dict(d)
            except (ValueError, TypeError, AttributeError):
                log.warning("작업 목록의 잘못된 항목을 건너뜀: %r", d)
                continue
            if job.id in seen:
                continue
            seen.add(job.id)
            if job.status in (Status.PROCESSING, Status.SAVING):
                job.status, job.progress = Status.WAITING, 0.0
            jobs.append(job)
        store.jobs = jobs
        store.refresh_missing()
        return store


def prune_job_dirs(store: Store, jobs_root: Path) -> list[Path]:
    """목록에 없는 영상의 캐시 폴더(<data>/jobs/<12자리 16진수>)를 지운다. 지운 폴더 목록."""
    root = Path(jobs_root)
    if not root.is_dir():
        return []
    keep = {j.id for j in store.jobs}
    removed = []
    for d in root.iterdir():
        if d.is_dir() and JOB_ID.fullmatch(d.name) and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed
