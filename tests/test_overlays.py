"""이미지 겹쳐 넣기: 영상별 이미지 목록(Job.overlays) 저장, 렌더로 넘기기, Api 편집, 저장한 로고."""
import json
from pathlib import Path

from app import models, paths
from app.api import pipeline_command
from app.jobqueue import RENDER
from app.store import Job, Store
from tests.test_api import MANIFEST, _job

OV = {"id": "0a1b2c3d", "img": "0123456789abcdef.png", "name": "로고", "aspect": 2.0,
      "x": 0.1, "y": 0.2, "w": 0.25, "start": 1.0, "end": 3.0}


def _render_cmd(tmp_path, monkeypatch, overlays):
    store = Store(tmp_path / "s.json")
    store.settings["save_dir"] = str(tmp_path / "saved")
    job = _job(tmp_path, store)
    job.overlays = overlays
    monkeypatch.setattr(models, "device", lambda settings, manifest: "auto")
    cmd, _, _ = pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data")
    return job, cmd


def test_job_overlays_roundtrip(tmp_path):
    store = Store(tmp_path / "s.json")
    job = _job(tmp_path, store)
    assert job.overlays == []
    job.overlays = [dict(OV)]
    store.save()
    assert Store.load(tmp_path / "s.json").jobs[0].overlays == [OV]
    old = Job.from_dict({"id": "x", "src": "/a.mp4"})   # 예전 작업: 키 없음
    assert old.overlays == []


def test_render_command_passes_overlays(tmp_path, monkeypatch):
    job, cmd = _render_cmd(tmp_path, monkeypatch, [dict(OV)])
    path = Path(cmd[cmd.index("--overlays") + 1])
    jd = paths.job_dir(Path(job.src))
    assert path == jd / "out" / "a.overlays.json"
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"path": str(jd / "overlays" / OV["img"]), "x": 0.1, "y": 0.2, "w": 0.25, "start": 1.0, "end": 3.0}]


def test_render_command_without_overlays(tmp_path, monkeypatch):
    job, _ = _render_cmd(tmp_path, monkeypatch, [dict(OV)])
    stale = paths.job_dir(Path(job.src)) / "out" / "a.overlays.json"
    assert stale.is_file()
    _, cmd = _render_cmd(tmp_path, monkeypatch, [])
    assert "--overlays" not in cmd and not stale.exists()
