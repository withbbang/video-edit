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


# ---- Api: 넣기·고치기·지우기 ----------------------------------------------------
import math
import struct
import zlib

from app.store import Status
from tests.test_api import FakeDialogs
from tests.test_history import setup as _setup


def png_bytes(w, h):
    ihdr = struct.pack(">II", w, h) + bytes([8, 6, 0, 0, 0])
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))


class ImageDialogs(FakeDialogs):
    def __init__(self, image=None):
        super().__init__()
        self.image = image

    def open_image(self):
        return self.image


def api_with_image(tmp_path, image, status=Status.REVIEW):
    a, store, job, push = _setup(tmp_path, status)      # 영상 길이 20초
    a._dialogs = ImageDialogs(str(image) if image else None)
    return a, store, job


def logo(tmp_path, name="로고.png", w=200, h=100):
    p = tmp_path / name
    p.write_bytes(png_bytes(w, h))
    return p


def test_add_overlay_copies_png_and_sets_defaults(tmp_path):
    src = logo(tmp_path)
    a, store, job = api_with_image(tmp_path, src)
    r = a.add_overlay(job.id, 2.0, 16 / 9)
    [o] = r["overlays"]
    assert (o["name"], o["aspect"], o["w"], o["x"], o["start"], o["end"]) == ("로고", 2.0, 0.25, 0.375, 2.0, 7.0)
    assert math.isclose(o["y"], (1 - 0.25 * (16 / 9) / 2) / 2)
    assert o["url"].endswith(f"/{job.id}/overlays/{o['img']}") and len(o["img"]) == 20
    assert r["history"] == {"undo": True, "redo": False}
    src.unlink()                                                     # 원본을 지워도 복사본으로 렌더
    from app.api import save_overlays
    data = json.loads(save_overlays(job).read_text(encoding="utf-8"))
    assert Path(data[0]["path"]).read_bytes() == png_bytes(200, 100)
    a._dialogs.image = str(logo(tmp_path, "둘.png"))
    assert a.add_overlay(job.id, 18.0, 16 / 9)["overlays"][1]["end"] == 20.0   # 영상 끝까지만


def test_add_overlay_rejects_non_png_and_cancel(tmp_path):
    bad = tmp_path / "가짜.png"
    bad.write_text("텍스트")
    a, store, job = api_with_image(tmp_path, bad)
    assert a.add_overlay(job.id, 0.0, 1.0) is None and job.overlays == []
    a._dialogs.image = None                                          # 취소
    assert a.add_overlay(job.id, 0.0, 1.0) is None


def test_add_overlay_only_in_review_or_saved(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path), status=Status.WAITING)
    assert a.add_overlay(job.id, 0.0, 1.0) is None


def test_update_overlay_validates(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))
    oid = a.add_overlay(job.id, 0.0, 1.0)["overlays"][0]["id"]
    for patch in ({"end": 21.0}, {"w": 0}, {"w": 1.5}, {"img": "x.png"}, {"x": float("nan")}, {"start": 5.0, "end": 4.0},
                  {"x": True}, {}):
        assert a.update_overlay(job.id, oid, patch) is None, patch
    r = a.update_overlay(job.id, oid, {"x": 0.5, "y": 0.0, "start": 10.0, "end": 12.5})
    o = r["overlays"][0]
    assert (o["x"], o["y"], o["start"], o["end"]) == (0.5, 0.0, 10.0, 12.5)
    assert a.update_overlay(job.id, "없음", {"x": 0.1}) is None


def test_overlay_edits_undo_and_saved_to_review(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path), status=Status.SAVED)
    oid = a.add_overlay(job.id, 0.0, 1.0)["overlays"][0]["id"]
    assert job.status == Status.REVIEW
    a.update_overlay(job.id, oid, {"x": 0.5})
    a.undo(job.id)
    assert job.overlays[0]["x"] == 0.375
    a.undo(job.id)
    assert job.overlays == []
    a.redo(job.id)
    assert a.delete_overlay(job.id, oid)["overlays"] == []
    assert a.delete_overlay(job.id, oid) is None


def test_get_result_lists_overlays(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))
    a.add_overlay(job.id, 0.0, 1.0)
    [o] = a.get_result(job.id)["overlays"]
    assert o["url"].startswith("http://127.0.0.1:9999/jobs/")


def test_presets_roundtrip(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))
    oid = a.add_overlay(job.id, 3.0, 1.0)["overlays"][0]["id"]
    a.update_overlay(job.id, oid, {"x": 0.7, "y": 0.05, "w": 0.2})
    assert a.save_preset(job.id, oid, "   ") is None
    assert a.save_preset(job.id, oid, "가" * 41) is None
    [p] = a.save_preset(job.id, oid, " 오른쪽 위 로고 ")
    assert (p["name"], p["x"], p["y"], p["w"], p["aspect"]) == ("오른쪽 위 로고", 0.7, 0.05, 0.2, 2.0)
    assert p["url"].endswith(f"/presets/{p['img']}")
    a.save_preset(job.id, oid, "오른쪽 위 로고")                      # 같은 이름은 덮어씀
    assert len(a.list_presets()) == 1
    job.overlays = []                                               # 다른 영상처럼: 이미지가 작업 폴더에 없어도 넣는다
    import shutil
    shutil.rmtree(paths.job_dir(Path(job.src)) / "overlays")
    r = a.insert_preset(job.id, p["id"])
    [o] = r["overlays"]
    assert (o["x"], o["y"], o["w"], o["start"], o["end"]) == (0.7, 0.05, 0.2, 0.0, 20.0)
    assert (paths.job_dir(Path(job.src)) / "overlays" / o["img"]).is_file()
    assert a.insert_preset(job.id, "없음") is None
    assert a.delete_preset(p["id"]) == [] and a.list_presets() == []


def test_rename_overlay(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))
    oid = a.add_overlay(job.id, 3.0, 1.0)["overlays"][0]["id"]
    assert a.rename_overlay(job.id, oid, "   ") is None
    assert a.rename_overlay(job.id, oid, "가" * 41) is None
    assert a.rename_overlay(job.id, "없음", "구독") is None
    r = a.rename_overlay(job.id, oid, " 구독 버튼 ")
    assert r["overlays"][0]["name"] == "구독 버튼" and job.overlays[0]["name"] == "구독 버튼"


def test_rename_preset(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))
    oid = a.add_overlay(job.id, 3.0, 1.0)["overlays"][0]["id"]
    a.save_preset(job.id, oid, "왼쪽")
    [p, q] = a.save_preset(job.id, oid, "오른쪽")
    assert a.rename_preset(p["id"], " ") is None
    assert a.rename_preset(p["id"], "오른쪽") is None                 # 다른 항목과 같은 이름은 안 됨 (저장 때 덮어쓰는 기준)
    assert a.rename_preset("없음", "위") is None
    items = a.rename_preset(p["id"], " 위 ")
    assert [x["name"] for x in items] == ["위", "오른쪽"] and items[0]["url"]
    assert [x["name"] for x in a.list_presets()] == ["위", "오른쪽"]
    assert a.rename_preset(p["id"], "위") is not None                 # 제 이름 그대로는 괜찮음


def test_add_tall_png_fits_inside_video(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path, "세로.png", 30, 100))
    o = a.add_overlay(job.id, 0.0, 16 / 9)["overlays"][0]
    h = o["w"] * (16 / 9) / o["aspect"]                 # 화면 높이 대비 이미지 높이
    assert h <= 1 + 1e-9 and o["y"] + h <= 1 + 1e-9 and o["x"] + o["w"] <= 1


def test_free_height_stored_and_validated(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))           # 200x100
    o = a.add_overlay(job.id, 0.0, 16 / 9)["overlays"][0]
    assert math.isclose(o["h"], 0.25 * (16 / 9) / 2)                  # 처음엔 그림 비율대로
    for bad in ({"h": 0}, {"h": 1.5}, {"h": float("inf")}):
        assert a.update_overlay(job.id, o["id"], bad) is None
    r = a.update_overlay(job.id, o["id"], {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.1})
    assert (r["overlays"][0]["w"], r["overlays"][0]["h"]) == (0.5, 0.1)
    from app.api import save_overlays
    assert json.loads(save_overlays(job).read_text(encoding="utf-8"))[0]["h"] == 0.1
    [p] = a.save_preset(job.id, o["id"], "납작 로고")
    assert p["h"] == 0.1
    assert a.insert_preset(job.id, p["id"])["overlays"][1]["h"] == 0.1


def test_old_overlay_without_height_still_saves(tmp_path):
    a, store, job = api_with_image(tmp_path, logo(tmp_path))
    job.overlays = [dict(OV)]                                           # 예전 항목: h 없음
    from app.api import save_overlays
    assert "h" not in json.loads(save_overlays(job).read_text(encoding="utf-8"))[0]
