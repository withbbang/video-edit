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

    def request_save(self, job_id, notify=True):
        self.calls.append(("request_save", job_id))

    def rejudge(self, job_id):
        self.calls.append(("rejudge", job_id))

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))

    def set_progress(self, job_id, value):
        self.calls.append(("set_progress", job_id, value))

    def shutdown(self, timeout=3.0):
        self.calls.append(("shutdown",))
        return True


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
    q.request_save = lambda jid, notify=True: setattr(job, "status", Status.SAVING)
    a.save(job.id)
    assert job.prev_cuts is None


def test_save_clears_baseline_even_if_render_already_finished(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.prev_cuts = "/x/prev.cuts.json"
    q.request_save = lambda jid, notify=True: setattr(job, "status", Status.SAVED)
    a.save(job.id)
    assert job.prev_cuts is None


def test_save_rejected_keeps_baseline(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.PROCESSING)
    job.prev_cuts = "/x/prev.cuts.json"
    a.save(job.id)
    assert job.prev_cuts == "/x/prev.cuts.json"


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
    a.set_option(job.id, "silence", 0)
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
    a.set_option(job.id, "loudnorm", "keep")
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


def test_set_option_slider_rejudges(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    r = a.set_option(job.id, "denoise", 80)
    assert r["accepted"] is True and r["options"]["denoise"] == 80
    assert ("rejudge", job.id) in q.calls
    assert Store.load(store.path).jobs[0].options.denoise == 80
    q.calls.clear()
    a.set_option(job.id, "denoise", "80")      # 같은 값(숫자 문자열) → 다시 판정 안 함
    assert not q.calls
    a.set_option(job.id, "silence", 70.0)
    assert job.options.silence == 70 and isinstance(job.options.silence, int)
    assert ("rejudge", job.id) in q.calls


def test_set_option_rejects_out_of_range(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    for bad in ("abc", "", None, True, "keep"):
        with pytest.raises(ValueError):
            a.set_option(job.id, "silence", bad)
    assert job.options.silence == 50 and not q.calls
    assert a.set_option(job.id, "silence", 150)["options"]["silence"] == 100
    assert a.set_option(job.id, "denoise", -1)["options"]["denoise"] == 0


def test_get_options_and_unknown_job(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store)
    assert a.get_options(job.id)["habit"] == "some"
    assert a.get_options("missing") is None


def test_unknown_job_is_noop(tmp_path):
    a, store, q, _ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    before = (job.to_dict(), store.path.read_text())
    assert a.get_options("missing") is None
    assert a.get_result("missing") is None
    assert a.set_option("missing", "habit", "all") is None
    assert a.save("missing") is None
    assert [j["id"] for j in a.remove_job("missing")] == [job.id]
    assert (job.to_dict(), store.path.read_text()) == before
    assert [c for c in q.calls if c[0] != "cancel"] == []
    with pytest.raises(ValueError):
        a.set_option("missing", "habit", "bogus")


def test_only_methods_are_public(tmp_path):
    import inspect
    a, *_ = make(tmp_path)
    public = [n for n in dir(a) if not n.startswith("_")]
    assert public and all(inspect.ismethod(getattr(a, n)) for n in public), public
    assert not [n for n in vars(a) if not n.startswith("_")]


# ---- 결과 --------------------------------------------------------------------

def test_get_result_without_prev(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 10.0, "final_sec": 9.0, "cuts": 2, "removed_sec": 1.0}
    write_out(job, "a.cuts.json", {"cuts": [{"start": 5, "end": 5.5}, {"start": 1, "end": 1.5}]})
    write_out(job, "a.transcript.json", {"duration": 10.0, "segments": []})
    preview = paths.job_dir(Path(job.src)) / "preview.mp4"
    preview.write_bytes(b"v")
    r = a.get_result(job.id)
    assert r["cuts"] == [[1, 1.5], [5, 5.5]]
    assert r["changes"] is None
    assert r["duration"] == 10.0
    assert r["summary"] == {**job.summary, "silence_cuts": 0, "silence_sec": 0.0, "silence_min_sec": 0.3}
    assert r["preview_url"] == f"http://127.0.0.1:9999/jobs/{job.id}/preview.mp4?v={preview.stat().st_mtime_ns}"
    assert "saved_paths" not in r
    json.dumps(r)


def test_get_result_without_preview_still_usable(tmp_path):
    """미리보기를 못 만들었어도 결과(컷·요약)는 쓸 수 있고 preview_url 만 None (final-review M8)."""
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 10.0, "final_sec": 9.5, "cuts": 1, "removed_sec": 0.5}
    write_out(job, "a.cuts.json", {"cuts": [{"start": 1, "end": 1.5}]})
    r = a.get_result(job.id)
    assert r["preview_url"] is None and r["cuts"] == [[1, 1.5]]
    assert r["summary"] == {**job.summary, "silence_cuts": 0, "silence_sec": 0.0, "silence_min_sec": 0.3}


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


def test_preview_url_has_version(tmp_path):
    """미리보기를 새로 만들면 주소가 바뀐다 — 같은 주소면 재생기가 다시 안 불러옴."""
    import os
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    preview = paths.job_dir(Path(job.src)) / "preview.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"v")
    os.utime(preview, ns=(1_000_000_000, 1_000_000_000))
    u1 = a.get_result(job.id)["preview_url"]
    assert u1 == f"http://127.0.0.1:9999/jobs/{job.id}/preview.mp4?v=1000000000"
    os.utime(preview, ns=(2_000_000_000, 2_000_000_000))
    assert a.get_result(job.id)["preview_url"] != u1


def test_get_result_silence_summary(tmp_path):
    """말 없는 구간 슬라이더 설명의 곳·초 — silence/gap 컷만, 겹치면 합쳐서, 되살린 곳은 뺀다."""
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 20.0, "final_sec": 15.0, "cuts": 4, "removed_sec": 5.0}
    write_out(job, "a.cuts.json", {"cuts": [
        {"start": 1.0, "end": 2.0, "word": "(무음)", "reason": "silence"},
        {"start": 1.5, "end": 3.0, "word": "(무음)", "reason": "gap"},
        {"start": 5.0, "end": 5.5, "word": "(무음)", "reason": "silence + gap"},
        {"start": 8.0, "end": 8.4, "word": "어", "reason": "filler", "words_idx": [[0, 0]]},
    ]})
    s = a.get_result(job.id)["summary"]
    assert s["silence_cuts"] == 2 and s["silence_sec"] == pytest.approx(2.5)
    assert s["cuts"] == 4   # 나머지 요약은 그대로
    job.range_ops.append({"op": "restore", "start": 5.0, "end": 5.5})
    s = a.get_result(job.id)["summary"]
    assert s["silence_cuts"] == 1 and s["silence_sec"] == pytest.approx(2.0)


def test_get_result_silence_min_sec_from_fillers(tmp_path, monkeypatch):
    """슬라이더 설명의 "N초 이상" 은 앱이 쓰는 fillers.json 의 silence_min_sec"""
    from app import subtitles
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.REVIEW)
    job.summary = {"orig_sec": 20.0}
    write_out(job, "a.cuts.json", {"cuts": []})
    assert a.get_result(job.id)["summary"]["silence_min_sec"] == 0.3
    custom = tmp_path / "fillers.json"
    custom.write_text(json.dumps({"silence_min_sec": 0.5}), encoding="utf-8")
    monkeypatch.setattr(subtitles, "fillers_path", lambda: custom)
    assert a.get_result(job.id)["summary"]["silence_min_sec"] == 0.5


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
    assert got["url"] == INFO["url"] and got["dest"] == tmp_path / "data" / "updates"
    assert got["opened"] == tmp_path / "data" / "updates" / "y.dmg"


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
    assert cmd[cmd.index("--media-out") + 1] == str(jd / "media")   # 저장 폴더가 아니라 임시 폴더에 렌더
    assert (tmp_path / "saved").is_dir() and (jd / "media").is_dir()
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


def _lock_free(lock) -> bool:
    """다른 스레드에서 lock 을 잡을 수 있으면 True (지금 스레드가 쥐고 있지 않음)."""
    got = []
    t = threading.Thread(target=lambda: got.append(lock.acquire(timeout=1) and (lock.release() or True)))
    t.start()
    t.join(5)
    return got == [True]


def test_save_notifies_outside_queue_lock(tmp_path):
    from app.jobqueue import Queue
    store = Store(tmp_path / "state.json")
    store.settings["save_dir"] = str(tmp_path / "saved")
    gate = threading.Event()

    def execute(job, kind, on_start):
        assert gate.wait(5)
        return RunResult(True)

    seen = []
    holder = {}

    def push(event, data):
        if event == "job":
            seen.append((data["status"], _lock_free(holder["q"].lock)))

    q = Queue(store, execute, api_mod.make_on_change(push), read_summary=lambda j: {})
    holder["q"] = q
    a = Api(store, q, push, FakeDialogs(), manifest=MANIFEST, data_root=tmp_path / "data",
            preview_base="http://x")
    job = store.add(video(tmp_path))
    job.status = Status.REVIEW
    job.prev_cuts = "/x/prev.cuts.json"
    view = a.save(job.id)
    assert view["status"] == Status.SAVING and job.prev_cuts is None
    gate.set()
    assert q.join(5)
    assert seen[0] == (Status.SAVING, True)
    assert all(free for _, free in seen)


# ---- 저장은 임시 폴더에 렌더하고 성공했을 때만 옮긴다 (final-review I1) ----

def _render_setup(tmp_path, monkeypatch, behaviour):
    store = Store(tmp_path / "s.json")
    save = tmp_path / "saved"
    store.settings["save_dir"] = str(save)
    job = _job(tmp_path, store)
    save.mkdir()
    (save / "a_clean.mp4").write_bytes(b"GOOD-OLD")      # 전에 저장한 좋은 파일
    (save / "a_clean.srt").write_text("old srt", encoding="utf-8")

    def fake_run(cmd, weights, on_progress, env=None, on_start=None):
        media = Path(cmd[cmd.index("--media-out") + 1])
        (media / "a_clean.srt").write_text("new srt", encoding="utf-8")
        (media / "a_clean.mp4").write_bytes(b"TRUNC")     # 렌더 도중
        return behaviour(media)

    monkeypatch.setattr(api_mod.runner, "run", fake_run)
    execute = make_execute(store, MANIFEST, tmp_path / "data", lambda: FakeQueue())
    return job, save, execute


@pytest.mark.parametrize("result", [RunResult(False, "cancelled"), RunResult(False, "오류: 디스크 가득")])
def test_cancelled_or_failed_save_leaves_save_dir_untouched(tmp_path, monkeypatch, result):
    job, save, execute = _render_setup(tmp_path, monkeypatch, lambda media: result)
    assert execute(job, RENDER, lambda h: None) == result
    assert sorted(p.name for p in save.iterdir()) == ["a_clean.mp4", "a_clean.srt"]
    assert (save / "a_clean.mp4").read_bytes() == b"GOOD-OLD"
    assert (save / "a_clean.srt").read_text(encoding="utf-8") == "old srt"
    assert not api_mod.media_dir(job).exists()


def test_successful_save_replaces_files(tmp_path, monkeypatch):
    def ok(media):
        (media / "a_clean.mp4").write_bytes(b"FULL")
        return RunResult(True)
    job, save, execute = _render_setup(tmp_path, monkeypatch, ok)
    assert execute(job, RENDER, lambda h: None) == RunResult(True)
    assert sorted(p.name for p in save.iterdir()) == ["a_clean.mp4", "a_clean.srt"]   # 숨김 임시 파일 없음
    assert (save / "a_clean.mp4").read_bytes() == b"FULL"
    assert (save / "a_clean.srt").read_text(encoding="utf-8") == "new srt"
    assert not api_mod.media_dir(job).exists()


def test_publish_failure_keeps_old_files_and_cleans_temps(tmp_path, monkeypatch):
    job, save, execute = _render_setup(tmp_path, monkeypatch, lambda media: RunResult(True))
    real = api_mod.shutil.move
    n = []

    def flaky(src, dst):
        n.append(src)
        if len(n) == 2:
            raise OSError("디스크 가득")
        return real(src, dst)
    monkeypatch.setattr(api_mod.shutil, "move", flaky)
    r = execute(job, RENDER, lambda h: None)
    assert not r.ok and "디스크 가득" in r.error
    assert sorted(p.name for p in save.iterdir()) == ["a_clean.mp4", "a_clean.srt"]
    assert (save / "a_clean.mp4").read_bytes() == b"GOOD-OLD"


def test_publish_media_across_volumes(tmp_path, monkeypatch):
    """다른 볼륨이면 rename 이 실패하므로 복사한 뒤 바꾼다."""
    media, save = tmp_path / "m", tmp_path / "s"
    media.mkdir()
    (media / "x_clean.mp4").write_bytes(b"v")
    real_rename = api_mod.os.rename

    def no_cross(src, dst):
        if str(src).startswith(str(media)):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(src, dst)
    monkeypatch.setattr(api_mod.os, "rename", no_cross)
    assert api_mod.publish_media(media, save) == [save / "x_clean.mp4"]
    assert (save / "x_clean.mp4").read_bytes() == b"v" and [p.name for p in save.iterdir()] == ["x_clean.mp4"]


# ---- 옵션을 바꾸면 달라진 곳 초기화 (final-review M1, 스펙 3.3) ----

@pytest.mark.parametrize("status", [Status.REVIEW, Status.SAVED])
def test_render_option_change_clears_changes(tmp_path, status):
    a, store, q, push = make(tmp_path)
    job = add_job(tmp_path, a, store, status=status)
    write_out(job, "a.cuts.json", {"cuts": []})
    job.prev_cuts = str(write_out(job, "prev.cuts.json", {"cuts": [{"start": 1, "end": 2}]}))
    assert a.get_result(job.id)["changes"] is not None
    assert a.set_option(job.id, "quality", "high")["accepted"]
    assert job.prev_cuts is None and a.get_result(job.id)["changes"] is None
    assert Store.load(store.path).get(job.id).prev_cuts is None


# ---- 원본이 돌아오면 목록 새로고침 때 되돌린다 (final-review I5) ----

def test_list_jobs_restores_missing_job(tmp_path):
    a, store, *_ = make(tmp_path)
    job = add_job(tmp_path, a, store, status=Status.MISSING)
    job.before_missing = Status.FAILED
    job.options.habit = "all"
    src = Path(job.src)
    src.unlink()
    assert a.list_jobs()[0]["status"] == Status.MISSING
    src.write_bytes(b"x")
    assert a.list_jobs()[0]["status"] == Status.FAILED
    assert Store.load(store.path).get(job.id).status == Status.FAILED
    assert a.get_options(job.id)["habit"] == "all"


# ---- 윈도우 GPU 결정이 남았으면 모델이 다 있어도 준비를 다시 (final-review I2) ----

def test_model_status_not_ready_when_windows_gpu_undecided(tmp_path, monkeypatch):
    a, store, *_ = make(tmp_path)
    for key, rev in (("whisper", "r1"), ("ctc", "r2")):
        d = tmp_path / "data" / "models" / key / rev
        d.mkdir(parents=True)
        (d / models.COMPLETE).write_text(rev)
    monkeypatch.setattr(models.sys, "platform", "darwin")
    assert a.model_status()["ready"]
    monkeypatch.setattr(models.sys, "platform", "win32")
    st = a.model_status()
    assert st == {"ready": False, "missing": [], "downloading": False}
    store.settings["gpu"] = {"key": models.manifest_hash(MANIFEST), "ok": False}
    assert a.model_status()["ready"]


# ---- 처리 중이면 업데이트 설치 전에 묻는다 (final-review M5) ----

@pytest.mark.parametrize("status", [Status.PROCESSING, Status.SAVING])
def test_install_update_asks_when_busy(tmp_path, monkeypatch, status):
    a, store, q, push = make(tmp_path)
    add_job(tmp_path, a, store, status=status)
    monkeypatch.setattr(api_mod.updates, "check", lambda current: dict(INFO))
    order = []
    monkeypatch.setattr(api_mod.updates, "download", lambda url, dest: Path(dest) / "y.dmg")
    monkeypatch.setattr(api_mod.updates, "open_file", lambda p: order.append("open"))
    a.check_update()
    assert a.install_update() == {"started": False, "confirm": True}
    assert a._update_thread is None and order == []
    q.shutdown = lambda timeout=3.0: order.append("shutdown") or True
    assert a.install_update(True) == {"started": True}
    a._update_thread.join(5)
    assert order == ["shutdown", "open"]
