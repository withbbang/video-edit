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


def test_load_skips_bad_entries_keeps_rest(tmp_path):
    """잘못된 항목 하나(src 없음·null·옛 형식) 때문에 목록 전체를 잃지 않는다."""
    import json
    good = tmp_path / "a.mp4"
    good.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    j = s.add(good)
    data = {"settings": {"save_dir": "/s"},
            "jobs": [{"id": "bad1", "src": None}, "not a dict", {"id": "bad2"},
                     {"id": "bad3", "src": "/x.mp4", "options": "oops"}, j.to_dict(),
                     {"id": "bad4", "src": "/y.mp4", "unknown_field": 1, "status": "waiting"}]}
    (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    r = Store.load(tmp_path / "state.json")
    assert [x.id for x in r.jobs] == [j.id, "bad4"]
    assert r.settings["save_dir"] == "/s"
    assert r.get("bad4").status == "missing"


def test_missing_source_restored_with_prior_status_and_options(tmp_path):
    """외장 드라이브를 빼고 켠 뒤 다시 꽂으면 전 상태·옵션으로 돌아온다 (final-review I5)."""
    import json as _json
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    for p in (a, b):
        p.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    ja, jb = s.add(a), s.add(b)
    ja.status, ja.summary, ja.progress = "review", {"cuts": 3}, 1.0
    ja.options = JobOptions(habit="all", quality="high")
    jb.status = "saved"            # 결과 캐시가 없는 저장됨 → 대기로
    cuts = paths.job_dir(a) / "out" / "a.cuts.json"
    cuts.parent.mkdir(parents=True)
    cuts.write_text(_json.dumps({"cuts": []}))
    s.save()
    a.rename(tmp_path / "a.moved")
    b.rename(tmp_path / "b.moved")
    r = Store.load(tmp_path / "state.json")
    assert [j.status for j in r.jobs] == ["missing", "missing"]
    r.save()                       # 원본 없음 상태로 한 번 저장돼도 전 상태를 잃지 않는다
    r = Store.load(tmp_path / "state.json")
    assert r.refresh_missing() == []
    (tmp_path / "a.moved").rename(a)
    (tmp_path / "b.moved").rename(b)
    changed = r.refresh_missing(mark_new=False)
    assert [j.id for j in changed] == [ja.id, jb.id]
    ra, rb = r.get(ja.id), r.get(jb.id)
    assert (ra.status, ra.summary, ra.before_missing) == ("review", {"cuts": 3}, None)
    assert ra.options == JobOptions(habit="all", quality="high")
    assert rb.status == "waiting"
    # 다시 켜도 그대로
    r.save()
    assert [j.status for j in Store.load(tmp_path / "state.json").jobs] == ["review", "waiting"]


def test_refresh_missing_skips_busy_and_listed(tmp_path):
    a = tmp_path / "a.mp4"
    s = Store(tmp_path / "state.json")
    j = s.add(a)               # 파일 없음
    j.status = "processing"
    assert s.refresh_missing() == [] and j.status == "processing"
    j.status = "waiting"
    assert s.refresh_missing(skip={j.id}) == [] and j.status == "waiting"
    assert s.refresh_missing(mark_new=False) == [] and j.status == "waiting"
    assert s.refresh_missing() == [j] and j.status == "missing" and j.before_missing == "waiting"


def test_prune_job_dirs(tmp_path):
    """목록에 없는 영상의 캐시만 지운다 (12자리 16진수 이름만) (final-review I4)."""
    from app.store import prune_job_dirs
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    j = s.add(a)
    root = paths.data_dir() / "jobs"
    keep, orphan, other = root / j.id, root / "0123456789ab", root / "notes"
    for d in (keep, orphan, other):
        (d / "out").mkdir(parents=True)
    assert prune_job_dirs(s, root) == [orphan]
    assert keep.is_dir() and other.is_dir() and not orphan.exists()
    assert prune_job_dirs(s, tmp_path / "none") == []
