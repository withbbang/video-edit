"""앱 끝-끝 시험 (창 없이 실제 Api + Queue + make_execute + 파이프라인). 느리고 모델이 필요해 기본은 건너뛴다.

    YTEDIT_E2E_MODELS=<models 폴더가 든 데이터 폴더> .venv/bin/python -m pytest tests/test_e2e_app.py -s

input/test.mp4 를 한글·공백 경로로 복사해 추가 → 분석 → 미리보기(Range) → 말버릇 모두 → 저장까지 확인한다.
"""
import http.client
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODELS = os.environ.get("YTEDIT_E2E_MODELS")
pytestmark = pytest.mark.skipif(not MODELS, reason="느린 끝-끝 시험: YTEDIT_E2E_MODELS 를 주면 돈다")

TIMEOUT = 900


class FakeDialogs:
    def __init__(self):
        self.folder = None

    def open_files(self):
        return None

    def choose_folder(self):
        return self.folder


def _wait(api, job_id, statuses, timeout=TIMEOUT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = next(j for j in api.list_jobs() if j["id"] == job_id)
        if job["status"] in statuses:
            return job
        if job["status"] == "failed":
            pytest.fail(f"실패: {job['error']}")
        time.sleep(0.5)
    pytest.fail(f"{statuses} 를 기다리다 시간 초과")


def _range_get(url, rng):
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.request("GET", u.path, headers={"Range": rng})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


def test_app_end_to_end(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "models").symlink_to(Path(MODELS) / "models", target_is_directory=True)
    monkeypatch.setenv("YTEDIT_DATA_DIR", str(data))
    src_dir = tmp_path / "바탕 화면"
    src_dir.mkdir()
    src = src_dir / "강의 03 (최종).mp4"
    shutil.copyfile(ROOT / "input" / "test.mp4", src)
    save_dir = tmp_path / "save"

    from app import window
    events = []
    dialogs = FakeDialogs()
    base = window.serve_previews(data)
    _store, _queue, api = window.build(data, lambda e, d: events.append((e, d)), dialogs, base)
    assert api.model_status()["ready"], "모델이 없음 — YTEDIT_E2E_MODELS 확인"

    jobs = api.add_files([str(src)])
    assert len(jobs) == 1
    assert len(api.add_files([str(src)])) == 1          # 같은 파일은 하나
    job_id = jobs[0]["id"]
    assert jobs[0]["name"] == src.name and jobs[0]["status"] == "waiting"

    t0 = time.monotonic()
    api.start()
    job = _wait(api, job_id, {"review"})
    print(f"분석 {time.monotonic() - t0:.1f}s, 요약 {job['summary']}")
    assert any(e == "job" and d["status"] == "processing" for e, d in events)

    r = api.get_result(job_id)
    assert r["cuts"], "컷이 비었음"
    assert r["duration"] > 0 and r["changes"] is None
    assert r["preview_url"] == f"{base}/{job_id}/preview.mp4"
    resp, body = _range_get(r["preview_url"], "bytes=0-1023")
    assert resp.status == 206 and len(body) == 1024
    assert resp.getheader("Content-Range").startswith("bytes 0-1023/")

    res = api.set_option(job_id, "habit", "all")
    assert res["accepted"] and res["options"]["habit"] == "all"
    _wait(api, job_id, {"review"})
    r = api.get_result(job_id)
    assert r["changes"] is not None
    print("변화", json.dumps(r["changes"], ensure_ascii=False)[:400])

    dialogs.folder = str(save_dir)
    assert api.set_save_dir() == str(save_dir)
    assert api.save(job_id)["status"] == "saving"
    _wait(api, job_id, {"saved"})
    r = api.get_result(job_id)
    stem = src.stem
    names = sorted(p.name for p in save_dir.iterdir())
    assert names == [f"{stem}_clean.mp4", f"{stem}_clean.srt"]
    assert sorted(Path(p).name for p in r["saved_paths"]) == names
    assert api.get_result(job_id)["changes"] is None    # 저장하면 달라진 곳 초기화

    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,duration,r_frame_rate:stream_tags=encoder,language",
                          "-of", "json", str(save_dir / f"{stem}_clean.mp4")],
                         capture_output=True, text=True, check=True)
    streams = json.loads(out.stdout)["streams"]
    print("ffprobe", streams)
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    subs = [s for s in streams if s["codec_type"] == "subtitle"]
    num, den = (int(x) for x in video["r_frame_rate"].split("/"))
    assert abs(float(video["duration"]) - float(audio["duration"])) <= den / num + 1e-3
    assert video.get("tags", {}).get("encoder")
    assert subs and subs[0].get("tags", {}).get("language") == "kor"

    # 저장 도중 ✕ (final-review I1·I4): 저장 폴더엔 잘린 파일도 숨김 임시 파일도 남지 않고 전에 저장한 파일은 그대로,
    # 목록에서 빠진 영상의 캐시 폴더도 지운다
    from app import paths as app_paths
    before = {p.name: p.read_bytes() for p in save_dir.iterdir()}
    assert api.set_option(job_id, "quality", "high")["accepted"]
    assert api.save(job_id)["status"] == "saving"
    end = time.monotonic() + 60
    while time.monotonic() < end:
        job = next(j for j in api.list_jobs() if j["id"] == job_id)
        assert job["status"] == "saving", f"멈추기 전에 끝남: {job}"
        if job["progress"] >= 0.1:      # 렌더(ffmpeg)가 돌기 시작
            break
        time.sleep(0.05)
    api.remove_job(job_id)
    end = time.monotonic() + 30
    while any(j["id"] == job_id for j in api.list_jobs()) and time.monotonic() < end:
        time.sleep(0.1)
    assert not any(j["id"] == job_id for j in api.list_jobs())
    assert {p.name: p.read_bytes() for p in save_dir.iterdir()} == before
    assert not app_paths.job_dir(src).exists()
