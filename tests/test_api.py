import json
import threading
import time
from pathlib import Path

import pytest

from app import api as api_mod
from app import models, paths
from app.api import Api, make_execute, pipeline_command
from app.jobqueue import ANALYZE, RENDER
from app.models import DownloadError
from app.runner import RunResult
from app.store import Status, Store

MANIFEST = {"models": [{"key": "whisper", "revision": "r1", "files": []},
                       {"key": "ctc", "revision": "r2", "files": []}]}


class FakeQueue:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []

    def start_all(self):
        self.calls.append(("start_all",))

    def request_save(self, job_id):
        self.calls.append(("request_save", job_id))

    def rejudge(self, job_id):
        self.calls.append(("rejudge", job_id))

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))

    def set_progress(self, job_id, value):
        self.calls.append(("set_progress", job_id, value))


class FakeDialogs:
    def __init__(self, files=None, folder=None):
        self.files, self.folder = files or [], folder

    def open_files(self):
        return self.files

    def choose_folder(self):
        return self.folder


class Pushes(list):
    def __call__(self, event, data):
        self.append((event, data))

    def of(self, event):
        return [d for e, d in self if e == event]


def make(tmp_path, dialogs=None):
    store = Store(tmp_path / "state.json")
    store.settings["save_dir"] = str(tmp_path / "saved")
    q, push = FakeQueue(), Pushes()
    a = Api(store, q, push, dialogs or FakeDialogs(), manifest=MANIFEST, data_root=tmp_path / "data",
            preview_base="http://127.0.0.1:9999/jobs")
    return a, store, q, push


def video(tmp_path, name="a.mp4"):
    p = tmp_path / name
    p.write_bytes(b"x")
    return p


def add_job(tmp_path, a, store, name="a.mp4", status=Status.WAITING):
    p = video(tmp_path, name)
    a.add_files([str(p)])
    job = store.jobs[-1]
    job.status = status
    return job


def write_out(job, name, data):
    out = paths.job_dir(Path(job.src)) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(data), encoding="utf-8")
    return out / name


# ---- 목록 --------------------------------------------------------------------

def test_add_files_filters_and_dedupes(tmp_path):
    a, store, q, _ = make(tmp_path)
    v = video(tmp_path, "A.MOV")
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    folder = tmp_path / "dir.mp4"
    folder.mkdir()
    jobs = a.add_files([str(v), str(txt), str(v), str(tmp_path / "gone.mp4"), str(folder)])
    assert [j["name"] for j in jobs] == ["A.MOV"]
    jobs = a.add_files([str(v)])
    assert len(jobs) == 1
    assert set(jobs[0]) >= {"id", "name", "status", "progress", "error", "summary"}
    assert Store.load(store.path).jobs[0].src == str(v)  # 저장됨
    json.dumps(jobs)


def test_add_files_relative_path_made_absolute(tmp_path, monkeypatch):
    a, store, *_ = make(tmp_path)
    video(tmp_path, "b.wav")
    monkeypatch.chdir(tmp_path)
    a.add_files(["b.wav"])
    assert store.jobs[0].src == str(tmp_path / "b.wav")


def test_pick_files_uses_dialog(tmp_path):
    v = video(tmp_path, "c.mkv")
    a, store, *_ = make(tmp_path, FakeDialogs(files=[str(v)]))
    assert [j["name"] for j in a.pick_files()] == ["c.mkv"]


def test_pick_files_cancelled(tmp_path):
    a, store, *_ = make(tmp_path, FakeDialogs(files=None))
    assert a.pick_files() == []


def test_remove_start_save_delegate_to_queue(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    a.start()
    a.save(job.id)
    a.remove_job(job.id)
    assert q.calls == [("start_all",), ("request_save", job.id), ("cancel", job.id)]


def test_save_clears_changes_baseline(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.prev_cuts = "/x/prev.cuts.json"
    q.request_save = lambda jid: setattr(job, "status", Status.SAVING)
    a.save(job.id)
    assert job.prev_cuts is None


# ---- 옵션 --------------------------------------------------------------------

def test_set_fast_option_triggers_rejudge(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    r = a.set_option(job.id, "habit", "all")
    assert r["accepted"] is True and r["options"]["habit"] == "all"
    assert ("rejudge", job.id) in q.calls
    assert Store.load(store.path).jobs[0].options.habit == "all"


def test_set_fast_option_on_saved_rejudges(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.SAVED)
    a.set_option(job.id, "silence", "keep")
    assert ("rejudge", job.id) in q.calls


def test_set_render_option_does_not_rejudge(tmp_path):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    r = a.set_option(job.id, "quality", "high")
    assert r["options"]["quality"] == "high"
    assert not [c for c in q.calls if c[0] == "rejudge"]
    assert job.status == Status.REVIEW


def test_set_render_option_on_saved_goes_back_to_review(tmp_path):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.SAVED)
    a.set_option(job.id, "denoise", "keep")
    assert job.status == Status.REVIEW
    assert not [c for c in q.calls if c[0] == "rejudge"]
    assert push.of("job")[-1]["status"] == Status.REVIEW
    assert Store.load(store.path).jobs[0].status == Status.REVIEW


def test_set_fast_option_on_waiting_only_saves(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store)
    a.set_option(job.id, "filler", "keep")
    assert job.options.filler == "keep" and not q.calls


@pytest.mark.parametrize("status", [Status.PROCESSING, Status.SAVING])
def test_set_option_rejected_while_running(tmp_path, status):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=status)
    r = a.set_option(job.id, "habit", "all")
    assert r == {"options": job.options.to_dict(), "accepted": False}
    assert job.options.habit == "some" and not q.calls


@pytest.mark.parametrize("key,value", [("nope", "keep"), ("habit", "remove"), ("quality", 3)])
def test_set_option_validates(tmp_path, key, value):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store)
    with pytest.raises(ValueError):
        a.set_option(job.id, key, value)


def test_get_options_and_unknown_job(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store)
    assert a.get_options(job.id)["habit"] == "some"
    with pytest.raises(ValueError):
        a.get_options("missing")


# ---- 결과 --------------------------------------------------------------------

def test_get_result_without_prev(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 10.0, "final_sec": 9.0, "cuts": 2, "removed_sec": 1.0}
    write_out(job, "a.cuts.json", {"cuts": [{"start": 5, "end": 5.5}, {"start": 1, "end": 1.5}]})
    write_out(job, "a.transcript.json", {"duration": 10.0, "segments": []})
    r = a.get_result(job.id)
    assert r["cuts"] == [[1, 1.5], [5, 5.5]]
    assert r["changes"] is None
    assert r["duration"] == 10.0
    assert r["summary"] == job.summary
    assert r["preview_url"] == f"http://127.0.0.1:9999/jobs/{job.id}/preview.mp4"
    assert "saved_paths" not in r
    json.dumps(r)


def test_get_result_with_prev_compares(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    cut = {"start": 1.0, "end": 1.2, "reason": "filler", "word": "그", "words_idx": [[0, 0]]}
    write_out(job, "a.cuts.json", {"cuts": [cut]})
    job.prev_cuts = str(write_out(job, "prev.cuts.json", {"cuts": []}))
    write_out(job, "a.transcript.json", {"duration": 3.0, "segments": [
        {"words": [{"word": " 그", "start": 1.0, "end": 1.2}]}]})
    r = a.get_result(job.id)
    assert r["changes"]["rows"] == [{"word": "그", "kind": "added", "start": 1.0, "end": 1.2}]


def test_get_result_missing_files(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store)
    job.summary = {"orig_sec": 7.0}
    r = a.get_result(job.id)
    assert r["cuts"] == [] and r["changes"] is None and r["duration"] == 7.0


def test_get_result_saved_paths(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.SAVED)
    save = Path(store.settings["save_dir"])
    save.mkdir()
    (save / "a_clean.mp4").write_bytes(b"")
    (save / "a_clean.srt").write_text("")
    r = a.get_result(job.id)
    assert r["saved_paths"] == [str(save / "a_clean.mp4"), str(save / "a_clean.srt")]


# ---- 설정 --------------------------------------------------------------------

def test_settings_and_save_dir(tmp_path):
    new = tmp_path / "elsewhere"
    a, store, *_ = make(tmp_path, FakeDialogs(folder=str(new)))
    assert a.get_settings()["save_dir"] == str(tmp_path / "saved")
    assert a.set_save_dir() == str(new)
    assert Store.load(store.path).settings["save_dir"] == str(new)


def test_set_save_dir_cancelled(tmp_path):
    a, store, *_ = make(tmp_path, FakeDialogs(folder=None))
    assert a.set_save_dir() == str(tmp_path / "saved")


def test_version(tmp_path):
    from app import __version__
    a, *_ = make(tmp_path)
    assert a.get_version() == __version__


# ---- 모델 --------------------------------------------------------------------

def test_model_status(tmp_path):
    a, *_ = make(tmp_path)
    assert a.model_status() == {"ready": False, "missing": ["whisper", "ctc"], "downloading": False}
    d = tmp_path / "data" / "models" / "whisper" / "r1"
    d.mkdir(parents=True)
    (d / models.COMPLETE).write_text("r1")
    assert a.model_status()["missing"] == ["ctc"]


def test_download_models_success(tmp_path, monkeypatch):
    a, store, q, push = make(tmp_path)

    def fake_prepare(manifest, root, settings, on_progress):
        for i in range(50):
            on_progress(i, 100)
        on_progress(100, 100)
        settings["gpu"] = {"key": "k", "ok": False}
        return models.paths(manifest, root)

    monkeypatch.setattr(models, "prepare", fake_prepare)
    assert a.download_models() == {"started": True}
    a._download_thread.join(5)
    ev = push.of("models")
    assert ev[-1] == {"ready": True}
    progress = [e for e in ev if "done" in e]
    assert progress[0] == {"done": 0, "total": 100}
    assert progress[-1] == {"done": 100, "total": 100}
    assert len(progress) < 10  # 묶어서 보냄
    assert Store.load(store.path).settings["gpu"] == {"key": "k", "ok": False}


def test_download_models_error_and_no_double_start(tmp_path, monkeypatch):
    a, store, q, push = make(tmp_path)
    gate = threading.Event()

    def fake_prepare(manifest, root, settings, on_progress):
        assert gate.wait(5)
        raise DownloadError("net")

    monkeypatch.setattr(models, "prepare", fake_prepare)
    assert a.download_models() == {"started": True}
    assert a.download_models() == {"started": False}
    assert a.model_status()["downloading"] is True
    gate.set()
    a._download_thread.join(5)
    assert push.of("models")[-1] == {"error": True}
    assert a.model_status()["downloading"] is False


# ---- 업데이트 ----------------------------------------------------------------

INFO = {"version": "9.0.0", "notes": "n", "url": "https://x/y-9.0.0.dmg"}


def test_dismissed_update_hidden(tmp_path, monkeypatch):
    a, store, *_ = make(tmp_path)
    monkeypatch.setattr(api_mod.updates, "check", lambda current: dict(INFO))
    assert a.check_update() == INFO
    a.dismiss_update("9.0.0")
    assert Store.load(store.path).settings["dismissed_version"] == "9.0.0"
    assert a.check_update() is None


def test_install_update(tmp_path, monkeypatch):
    a, store, q, push = make(tmp_path)
    monkeypatch.setattr(api_mod.updates, "check", lambda current: dict(INFO))
    got = {}

    def fake_download(url, dest):
        got["url"], got["dest"] = url, dest
        return Path(dest) / "y.dmg"

    monkeypatch.setattr(api_mod.updates, "download", fake_download)
    monkeypatch.setattr(api_mod.updates, "open_file", lambda p: got.setdefault("opened", p))
    a.check_update()
    assert a.install_update() == {"started": True}
    a._update_thread.join(5)
    assert got["url"] == INFO["url"] and got["dest"] == paths.data_dir() / "updates"
    assert got["opened"] == paths.data_dir() / "updates" / "y.dmg"


def test_install_update_error_pushed(tmp_path, monkeypatch):
    a, store, q, push = make(tmp_path)
    monkeypatch.setattr(api_mod.updates, "check", lambda current: dict(INFO))

    def boom(url, dest):
        raise OSError("disk full")

    monkeypatch.setattr(api_mod.updates, "download", boom)
    a.check_update()
    a.install_update()
    a._update_thread.join(5)
    assert push.of("update")[-1] == {"error": "disk full"}


def test_install_update_without_check(tmp_path, monkeypatch):
    a, store, q, push = make(tmp_path)
    monkeypatch.setattr(api_mod.updates, "check", lambda current: None)
    assert a.install_update() == {"started": False}


# ---- 실행 --------------------------------------------------------------------

def _job(tmp_path, store):
    p = video(tmp_path)
    return store.add(p)


def test_pipeline_command_analyze_and_render(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.json")
    store.settings["save_dir"] = str(tmp_path / "saved")
    job = _job(tmp_path, store)
    monkeypatch.setattr(models, "device", lambda settings, manifest: "auto")
    cmd, weights, env = pipeline_command(job, ANALYZE, store, MANIFEST, tmp_path / "data")
    assert "--review" in cmd and "--compute-type" not in cmd
    assert cmd[cmd.index("--device") + 1] == "auto"
    assert cmd[cmd.index("--model") + 1] == str(tmp_path / "data" / "models" / "whisper" / "r1")
    assert weights is api_mod.ANALYZE_WEIGHTS
    jd = paths.job_dir(Path(job.src))
    assert (jd / "out").is_dir() and (jd / "work").is_dir()
    cmd, weights, env = pipeline_command(job, RENDER, store, MANIFEST, tmp_path / "data")
    assert cmd[cmd.index("--media-out") + 1] == str(tmp_path / "saved")
    assert (tmp_path / "saved").is_dir()
    assert weights is api_mod.RENDER_WEIGHTS


def test_pipeline_command_cuda_adds_compute_type(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.json")
    job = _job(tmp_path, store)
    monkeypatch.setattr(models, "device", lambda settings, manifest: "cuda")
    monkeypatch.setattr(models, "pipeline_env", lambda m, r, s: {"YTEDIT_CUDA_DIR": "/c"})
    for kind in (ANALYZE, RENDER):
        cmd, _, env = pipeline_command(job, kind, store, MANIFEST, tmp_path / "data")
        assert cmd[-2:] == ["--compute-type", models.CUDA_COMPUTE_TYPE]
        assert cmd[cmd.index("--device") + 1] == "cuda"
        assert env == {"YTEDIT_CUDA_DIR": "/c"}


def test_make_execute_runs_and_throttles_progress(tmp_path, monkeypatch):
    store = Store(tmp_path / "s.json")
    job = _job(tmp_path, store)
    q = FakeQueue()
    seen = {}

    def fake_run(cmd, weights, on_progress, env=None, on_start=None):
        seen.update(cmd=cmd, env=env, on_start=on_start)
        for i in range(100):
            on_progress(i / 100)
        return RunResult(True)

    monkeypatch.setattr(api_mod.runner, "run", fake_run)
    on_start = lambda h: None
    execute = make_execute(store, MANIFEST, tmp_path / "data", lambda: q)
    assert execute(job, ANALYZE, on_start) == RunResult(True)
    assert seen["on_start"] is on_start
    prog = [c for c in q.calls if c[0] == "set_progress"]
    assert 1 <= len(prog) <= 3
    assert prog[0] == ("set_progress", job.id, 0.0)
