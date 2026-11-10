"""음원(BGM) 넣기: 영상별 목록(Job.bgms) 저장, 렌더로 넘기기, get_result 의 화면용 값."""
import json
from pathlib import Path

from app import models, paths
from app.api import pipeline_command
from app.jobqueue import RENDER
from app.store import Job, Store
from tests.test_api import MANIFEST, _job
from tests.test_history import setup as _setup

B = {"id": "0a1b2c3d", "file": "0123456789abcdef.m4a", "name": "노래", "dur": 30.0,
     "gain": -3.0, "at": 0.0, "in": 0.0, "out": 10.0, "points": None}


def _render_cmd(tmp_path, monkeypatch, bgms):
    store = Store(tmp_path / "s.json")
    store.settings["save_dir"] = str(tmp_path / "saved")
    job = _job(tmp_path, store)
    job.bgms = bgms
    monkeypatch.setattr(models, "device", lambda settings, manifest: "auto")
    cmd, _, _ = pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data")
    return job, cmd


def test_job_bgms_roundtrip(tmp_path):
    store = Store(tmp_path / "s.json")
    job = _job(tmp_path, store)
    assert job.bgms == []
    job.bgms = [dict(B)]
    store.save()
    assert Store.load(tmp_path / "s.json").jobs[0].bgms == [B]
    assert Job.from_dict({"id": "x", "src": "/a.mp4"}).bgms == []   # 예전 작업: 키 없음


def test_render_command_passes_bgm(tmp_path, monkeypatch):
    touched = {**B, "id": "11111111", "at": 12.0, "points": [[0.0, -6.0], [10.0, -6.0]]}
    job, cmd = _render_cmd(tmp_path, monkeypatch, [dict(B), touched])
    path = Path(cmd[cmd.index("--bgm") + 1])
    jd = paths.job_dir(Path(job.src))
    assert path == jd / "out" / "a.bgm.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [d["path"] for d in data] == [str(jd / "bgm" / B["file"])] * 2
    assert {k: data[0][k] for k in ("at", "in", "out", "gain")} == {"at": 0.0, "in": 0.0, "out": 10.0, "gain": -3.0}
    assert data[0]["points"] and data[0]["points"][0] == [0.0, -40.0]     # 손대지 않은 것: 자동 곡선이 풀려 들어감
    assert data[1]["points"] == [[0.0, -6.0], [10.0, -6.0]]               # 손댄 것: 그대로


def test_render_command_without_bgm(tmp_path, monkeypatch):
    job, _ = _render_cmd(tmp_path, monkeypatch, [dict(B)])
    stale = paths.job_dir(Path(job.src)) / "out" / "a.bgm.json"
    assert stale.is_file()
    _, cmd = _render_cmd(tmp_path, monkeypatch, [])
    assert "--bgm" not in cmd and not stale.exists()


def test_untouched_follows_cuts_touched_stays(tmp_path):
    a, store, job, _ = _setup(tmp_path)
    pts = [[0.0, -6.0], [5.0, -6.0], [10.0, -6.0]]
    job.bgms = [dict(B), {**B, "id": "22222222", "points": pts}]
    r1 = a.get_result(job.id)
    assert r1["keeps"][0][0] == 0.0 and r1["out_total"] > 0
    assert [b["touched"] for b in r1["bgms"]] == [False, True]
    assert r1["bgms"][0]["url"].endswith(f"/{job.id}/bgm/{B['file']}")
    a.add_range_op(job.id, "cut", 3.2, 4.8)           # 말 구간(5.0 초 단어) 앞을 자름
    r2 = a.get_result(job.id)
    assert r2["out_total"] < r1["out_total"]
    assert r2["bgms"][0]["points"] != r1["bgms"][0]["points"]
    assert r2["bgms"][1]["points"] == r1["bgms"][1]["points"] == pts
    assert [b["at"] for b in r2["bgms"]] == [0.0, 0.0]
    assert r2["bgms"][0]["touched"] is False


def test_undo_restores_bgms(tmp_path):
    a, store, job, _ = _setup(tmp_path)
    job.bgms = [dict(B)]
    snap = a._snap(job)
    job.bgms = []
    a._put(job, snap)
    assert job.bgms == [B]
