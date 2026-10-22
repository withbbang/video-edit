from app import paths
from app.options import JobOptions
from app.store import Store


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("YTEDIT_DATA_DIR", str(tmp_path / "d"))
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    for p in (a, b):
        p.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    ja, jb = s.add(a), s.add(b)
    assert ja.id == paths.job_dir(a).name
    ja.options = JobOptions(habit="all")
    jb.status, jb.summary, jb.prev_cuts = "review", {"orig_sec": 1.0, "final_sec": 0.5, "cuts": 2, "removed_sec": 0.5}, "x.json"
    s.settings["save_dir"] = "/z/한글"
    s.save()
    r = Store.load(tmp_path / "state.json")
    assert [j.to_dict() for j in r.jobs] == [j.to_dict() for j in s.jobs]
    assert r.settings["save_dir"] == "/z/한글"
    assert r.get(ja.id).options.habit == "all"


def test_restore_resets_running_and_marks_missing(tmp_path):
    a, b, c = (tmp_path / n for n in ("a.mp4", "b.mp4", "c.mp4"))
    for p in (a, b, c):
        p.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    ja, jb, jc = s.add(a), s.add(b), s.add(c)
    ja.status, jb.status, jc.status = "processing", "saving", "review"
    ja.progress = 0.5
    s.save()
    c.unlink()
    r = Store.load(tmp_path / "state.json")
    assert [j.status for j in r.jobs] == ["waiting", "waiting", "missing"]
    assert r.jobs[0].progress == 0


def test_add_duplicate_returns_none(tmp_path):
    s = Store(tmp_path / "state.json")
    assert s.add(tmp_path / "a.mp4") is not None
    assert s.add(tmp_path / "a.mp4") is None


def test_remove_and_get(tmp_path):
    s = Store(tmp_path / "state.json")
    j = s.add(tmp_path / "a.mp4")
    assert s.get(j.id) is j
    s.remove(j.id)
    assert s.get(j.id) is None and s.jobs == []


def test_corrupt_state_gives_empty(tmp_path):
    (tmp_path / "state.json").write_text("{")
    assert Store.load(tmp_path / "state.json").jobs == []
    assert Store.load(tmp_path / "none.json").jobs == []
