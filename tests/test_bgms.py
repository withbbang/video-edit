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


# ---- Api: 불러오기·고치기·자동 조절·보관함 ----------------------------------------------
import math
import re
import shutil
import subprocess

import pytest

from app import bgm
from app.api import save_bgms
from app.store import Status
from tests.test_api import FakeDialogs

pytestmark_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 필요")


class AudioDialogs(FakeDialogs):
    def __init__(self, audio=None):
        super().__init__()
        self.audio = audio

    def open_audio(self):
        return self.audio


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def tone(tmp_path, name="노래.mp3", sec=3):
    p = tmp_path / name
    _ff("-f", "lavfi", "-i", f"sine=frequency=440:duration={sec}", str(p))
    return p


def api_with_audio(tmp_path, audio, status=Status.REVIEW, monkeypatch=None):
    a, store, job, push = _setup(tmp_path, status)
    a._dialogs = AudioDialogs(str(audio) if audio else None)
    if monkeypatch is not None:
        monkeypatch.setenv("YTEDIT_DATA_DIR", str(tmp_path / "data"))
    return a, store, job


@pytestmark_ffmpeg
def test_add_bgm_imports_and_defaults(tmp_path):
    src = tone(tmp_path)
    a, store, job = api_with_audio(tmp_path, src)
    r = a.add_bgm(job.id, 2.0)
    [b] = r["bgms"]
    assert (b["name"], b["in"], b["points"] is not None, b["touched"]) == ("노래", 0.0, True, False)
    assert math.isclose(b["out"], b["dur"]) and 2.5 < b["dur"] < 3.5
    assert job.bgms[0]["points"] is None and -30 <= b["gain"] <= 12
    assert re.fullmatch(r"[0-9a-f]{16}\.m4a", b["file"]) and b["url"].endswith(f"/{job.id}/bgm/{b['file']}")
    assert 0 <= b["at"] <= 20 and r["history"] == {"undo": True, "redo": False}
    src.unlink()                                                  # 원본을 지워도 복사본으로 렌더
    data = json.loads(save_bgms(job).read_text(encoding="utf-8"))
    assert Path(data[0]["path"]).is_file() and Path(data[0]["path"]).name == b["file"]
    a._dialogs.audio = str(tone(tmp_path, "둘.mp3", 2))
    b2 = a.add_bgm(job.id, 999.0)["bgms"][1]                      # 결과 길이 밖은 끝으로
    assert b2["at"] <= 20.0 and b2["at"] >= b["at"]
    assert a.add_bgm(job.id, float("nan")) is None


@pytestmark_ffmpeg
def test_add_bgm_rejects_video_and_garbage(tmp_path):
    vid = tmp_path / "v.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=d=1:s=64x64", "-f", "lavfi", "-i", "sine=d=1", "-shortest", str(vid))
    txt = tmp_path / "가짜.mp3"
    txt.write_text("not audio")
    a, store, job = api_with_audio(tmp_path, vid)
    assert a.add_bgm(job.id, 0.0) is None
    a._dialogs.audio = str(txt)
    assert a.add_bgm(job.id, 0.0) is None
    a._dialogs.audio = None
    assert a.add_bgm(job.id, 0.0) is None and job.bgms == []
    a._dialogs.audio = str(tone(tmp_path))
    job.status = Status.PROCESSING
    assert a.add_bgm(job.id, 0.0) is None


def test_bgm_import_loudness_gain_is_clamped(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg 필요")
    quiet = tmp_path / "q.wav"
    _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-af", "volume=-50dB", str(quiet))
    got = bgm.import_audio(quiet, tmp_path / "bgm")
    assert got["gain"] == 12.0
    again = bgm.import_audio(quiet, tmp_path / "bgm")             # 이미 있으면 다시 안 만듦
    assert again["file"] == got["file"]
    assert [p.name for p in (tmp_path / "bgm").iterdir()] == [got["file"]]


def _with_bgm(tmp_path):
    a, store, job, _ = _setup(tmp_path)
    job.bgms = [dict(B)]
    return a, store, job


def test_update_bgm_validates(tmp_path):
    a, store, job = _with_bgm(tmp_path)
    bid = B["id"]
    for bad in ({"out": 31.0}, {"in": 10.0}, {"in": 9.95}, {"in": 5.0, "out": 5.05}, {"zzz": 1.0},
                {"at": float("nan")}, {"at": -1.0}, {"in": -1.0}, {}, {"at": True}, {"points": []},
                {"points": [[1, 2, 3]]}, {"points": "x"}, {"points": [[float("nan"), 0]]},
                {"points": [[0, 0]] * 501}):
        assert a.update_bgm(job.id, bid, bad) is None, bad
    assert a.update_bgm(job.id, "없음", {"at": 1.0}) is None
    assert job.bgms == [B]
    r = a.update_bgm(job.id, bid, {"at": 2.0, "in": 1.0, "out": 9.0})
    assert (r["bgms"][0]["at"], r["bgms"][0]["in"], r["bgms"][0]["out"]) == (2.0, 1.0, 9.0)
    assert r["bgms"][0]["touched"] is False
    r = a.update_bgm(job.id, bid, {"points": [[5, 999], [0, -999], [40, 0]]})
    assert r["bgms"][0]["touched"] is True
    assert job.bgms[0]["points"] == [[0.0, bgm.DB_MIN], [5.0, bgm.DB_MAX], [30.0, 0.0]]


def test_reset_and_undo(tmp_path):
    a, store, job = _with_bgm(tmp_path)
    a.update_bgm(job.id, B["id"], {"points": [[0, -6], [5, -6]]})
    job.status = Status.SAVED
    assert a.reset_bgm(job.id, B["id"])["bgms"][0]["touched"] is False
    assert job.status == Status.REVIEW and job.bgms[0]["points"] is None
    assert a.reset_bgm(job.id, "없음") is None
    a.undo(job.id)
    assert job.bgms[0]["points"] == [[0.0, -6.0], [5.0, -6.0]]
    a.undo(job.id)
    assert job.bgms == [B]


def test_rename_and_delete_bgm(tmp_path):
    a, store, job = _with_bgm(tmp_path)
    assert a.rename_bgm(job.id, B["id"], "  ") is None and a.rename_bgm(job.id, B["id"], "가" * 41) is None
    assert a.rename_bgm(job.id, B["id"], " 새 ")["bgms"][0]["name"] == "새"
    assert a.delete_bgm(job.id, "없음") is None
    assert a.delete_bgm(job.id, B["id"])["bgms"] == [] and job.bgms == []
    assert a.undo(job.id) is not None and len(job.bgms) == 1


@pytestmark_ffmpeg
def test_bgm_presets_roundtrip(tmp_path, monkeypatch):
    a, store, job = api_with_audio(tmp_path, tone(tmp_path), monkeypatch=monkeypatch)
    bid = a.add_bgm(job.id, 0.0)["bgms"][0]["id"]
    assert a.save_bgm_preset(job.id, bid, "   ") is None
    assert a.save_bgm_preset(job.id, bid, "가" * 41) is None
    assert a.save_bgm_preset(job.id, "없음", "곡") is None
    [p] = a.save_bgm_preset(job.id, bid, " 잔잔한 곡 ")
    assert p["name"] == "잔잔한 곡" and p["url"].endswith(f"/bgm_presets/{p['file']}")
    assert (a._bgm_presets_dir() / p["file"]).is_file()
    a.save_bgm_preset(job.id, bid, "잔잔한 곡")                    # 같은 이름은 덮어씀
    assert len(a.list_bgm_presets()) == 1
    job.bgms = []
    r = a.insert_bgm_preset(job.id, p["id"], 4.0)
    [b] = r["bgms"]
    assert (b["name"], b["in"], b["out"], b["touched"]) == ("잔잔한 곡", 0.0, p["dur"], False)
    assert (paths.job_dir(Path(job.src)) / "bgm" / p["file"]).is_file()
    assert a.insert_bgm_preset(job.id, "없음", 0.0) is None
    a.save_bgm_preset(job.id, b["id"], "둘째")
    [x, y] = a.list_bgm_presets()
    assert a.rename_bgm_preset(x["id"], " ") is None
    assert a.rename_bgm_preset(x["id"], y["name"]) is None        # 다른 항목과 같은 이름은 안 됨
    assert a.rename_bgm_preset("없음", "위") is None
    assert a.rename_bgm_preset(x["id"], x["name"]) is not None    # 제 이름 그대로는 괜찮음
    assert [q["name"] for q in a.rename_bgm_preset(x["id"], " 위 ")] == ["위", y["name"]]
    a.delete_bgm_preset(x["id"])
    assert (a._bgm_presets_dir() / p["file"]).is_file()           # 같은 파일을 다른 항목이 씀
    assert a.delete_bgm_preset(y["id"]) == [] and not (a._bgm_presets_dir() / p["file"]).exists()
