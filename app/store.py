"""작업 목록과 설정을 JSON 으로 저장/복원."""
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .options import JobOptions


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
    prev_cuts: str | None = None  # 직전 판정 cuts.json 경로 (diff 기준)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["options"] = self.options.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        d["options"] = JobOptions.from_dict(d.get("options") or {})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


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
            settings = {**_default_settings(), **data.get("settings", {})}
            jobs = [Job.from_dict(d) for d in data.get("jobs", [])]
        except (OSError, ValueError, TypeError, AttributeError):
            return store
        store.settings = settings
        for job in jobs:
            if job.status in (Status.PROCESSING, Status.SAVING):
                job.status, job.progress = Status.WAITING, 0.0
            if not Path(job.src).exists():
                job.status = Status.MISSING
        store.jobs = jobs
        return store
