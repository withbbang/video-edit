"""썸네일 시트: 미리보기 영상에서 1초 간격 썸네일을 100장씩 타일 이미지로 만든다."""
import shutil
import subprocess

import pytest

from app import thumbs


def test_plan_one_hour_1080p():
    p = thumbs.plan(3600.0, 960, 540)
    assert (p["tile_w"], p["tile_h"], p["count"], p["sheets"]) == (160, 90, 3600, 36)
    assert (p["step"], p["cols"], p["rows"], p["per_sheet"]) == (1.0, 10, 10, 100)


def test_plan_short_and_partial():
    p = thumbs.plan(0.4, 960, 540)
    assert p["count"] == 1 and p["sheets"] == 1
    p = thumbs.plan(150.5, 540, 960)   # 세로 영상
    assert p["count"] == 151 and p["sheets"] == 2 and p["tile_w"] == 50


def test_sheet_cmd_window_and_threads(tmp_path):
    cmd = thumbs.sheet_cmd(tmp_path / "v.mp4", 3, thumbs.plan(3600, 960, 540), tmp_path / "s.jpg")
    assert cmd[cmd.index("-ss") + 1] == "300" and cmd[cmd.index("-t") + 1] == "100"
    assert cmd[cmd.index("-threads") + 1] == "2"
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=160:90" in vf and vf.endswith("tile=10x10")
    assert cmd[-1] == str(tmp_path / "s.jpg")


def test_sheet_name():
    assert thumbs.sheet_name(7) == "sheet_0007.jpg"


def test_meta_mismatch_resets_sheets(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"a")
    d = tmp_path / "thumbs"
    p = thumbs.plan(300, 960, 540)
    m = thumbs.mark_done(d, thumbs.load_meta(d, v, p), 1)
    assert m["done"] == [1]
    (d / thumbs.sheet_name(1)).write_bytes(b"x")
    assert thumbs.load_meta(d, v, p)["done"] == [1]
    v.write_bytes(b"bb")                                    # 미리보기 영상이 바뀜
    assert thumbs.load_meta(d, v, p)["done"] == []
    assert not (d / thumbs.sheet_name(1)).exists()


def test_meta_plan_change_resets(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"a")
    d = tmp_path / "thumbs"
    thumbs.mark_done(d, thumbs.load_meta(d, v, thumbs.plan(300, 960, 540)), 0)
    assert thumbs.load_meta(d, v, thumbs.plan(300, 540, 960))["done"] == []


def test_mark_done_sorted_unique(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"a")
    d = tmp_path / "thumbs"
    m = thumbs.load_meta(d, v, thumbs.plan(300, 960, 540))
    for i in (2, 0, 2):
        m = thumbs.mark_done(d, m, i)
    assert m["done"] == [0, 2] and thumbs.load_meta(d, v, thumbs.plan(300, 960, 540))["done"] == [0, 2]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")
def test_sheet_cmd_real_ffmpeg(tmp_path):
    v = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=12",
                    "-c:v", "libx264", "-g", "1", "-pix_fmt", "yuv420p", str(v)], check=True)
    p = thumbs.plan(12.0, 320, 180)
    out = tmp_path / "s.jpg"
    subprocess.run(thumbs.sheet_cmd(v, 0, p, out), check=True)
    wh = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                        capture_output=True, text=True, check=True).stdout.strip()
    assert wh == "1600,900"


# ---- 일꾼 ----------------------------------------------------------------------

import threading  # noqa: E402

from app.runner import RunResult  # noqa: E402


class FakeRun:
    """sheet_cmd 의 마지막 인자(출력 경로)에 바이트를 쓴다. gate 가 있으면 cancel 될 때까지 기다린다."""

    def __init__(self, fail=(), block=False):
        self.calls, self.fail, self.block = [], set(fail), block
        self.started = threading.Event()
        self.worker = None

    def __call__(self, cmd):
        out = cmd[-1]
        self.calls.append(out)
        if self.block:
            gate = threading.Event()

            class H:
                def cancel(self_inner):
                    gate.set()
            self.worker.started(H())
            self.started.set()
            gate.wait(5)
            return RunResult(False, "cancelled")
        if any(out.endswith(thumbs.sheet_name(i)) for i in self.fail):
            return RunResult(False, "boom")
        with open(out, "wb") as f:
            f.write(b"jpg")
        return RunResult(True, "")


def worker(run):
    seen = []
    w = thumbs.Thumbs(run, seen.append)
    run.worker = w
    return w, seen


def video(tmp_path, name="v.mp4"):
    v = tmp_path / name
    v.write_bytes(b"v")
    return v


def test_builds_all_sheets_in_order_and_notifies(tmp_path):
    run = FakeRun()
    w, seen = worker(run)
    v, d = video(tmp_path), tmp_path / "thumbs"
    meta = w.request("A", v, d, 250.0, (960, 540))
    assert meta["done"] == [] and meta["sheets"] == 3
    assert w.wait_idle(5)
    assert [p.rsplit("/", 1)[-1] for p in run.calls] == [thumbs.sheet_name(i) for i in range(3)]
    assert seen == ["A", "A", "A"]
    assert w.request("A", v, d, 250.0, (960, 540))["done"] == [0, 1, 2]
    assert w.wait_idle(5) and len(run.calls) == 3   # 다 있으면 다시 만들지 않는다
    w.shutdown()


def test_new_job_replaces_current(tmp_path):
    run = FakeRun(block=True)
    w, _ = worker(run)
    a, b = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    w.request("A", a, tmp_path / "ta", 250.0, (960, 540))
    assert run.started.wait(5)
    run.block = False
    w.request("B", b, tmp_path / "tb", 150.0, (960, 540))
    assert w.wait_idle(5)
    assert all("/tb/" in p for p in run.calls[1:]) and len(run.calls) == 3
    w.shutdown()


def test_cancel_holds_until_release(tmp_path):
    run = FakeRun()
    w, _ = worker(run)
    v, d = video(tmp_path), tmp_path / "thumbs"
    assert w.cancel("A", 1)
    w.request("A", v, d, 50.0, (960, 540))
    assert w.wait_idle(5) and run.calls == []
    w.release("A")
    w.request("A", v, d, 50.0, (960, 540))
    assert w.wait_idle(5) and len(run.calls) == 1
    w.shutdown()


def test_cancel_stops_running_job(tmp_path):
    run = FakeRun(block=True)
    w, seen = worker(run)
    w.request("A", video(tmp_path), tmp_path / "thumbs", 250.0, (960, 540))
    assert run.started.wait(5)
    assert w.cancel("A", 5)
    assert w.wait_idle(5) and len(run.calls) == 1 and seen == []
    w.shutdown()


def test_failed_sheet_skipped_not_retried(tmp_path):
    run = FakeRun(fail={1})
    w, seen = worker(run)
    v, d = video(tmp_path), tmp_path / "thumbs"
    w.request("A", v, d, 250.0, (960, 540))
    assert w.wait_idle(5)
    assert w.request("A", v, d, 250.0, (960, 540))["done"] == [0, 2]
    assert w.wait_idle(5)
    assert sum(p.endswith(thumbs.sheet_name(1)) for p in run.calls) == 1 and seen == ["A", "A"]
    w.shutdown()


def test_drop_forgets_failures(tmp_path):
    run = FakeRun(fail={0})
    w, _ = worker(run)
    v, d = video(tmp_path), tmp_path / "thumbs"
    w.request("A", v, d, 50.0, (960, 540))
    assert w.wait_idle(5)
    w.drop("A")
    run.fail = set()
    w.request("A", v, d, 50.0, (960, 540))
    assert w.wait_idle(5) and len(run.calls) == 2
    w.shutdown()


def test_request_without_video_is_none(tmp_path):
    w, _ = worker(FakeRun())
    assert w.request("A", tmp_path / "nope.mp4", tmp_path / "thumbs", 10.0, (960, 540)) is None


def test_mark_done_does_not_recreate_deleted_folder(tmp_path):
    # 영상을 빼서 작업 폴더가 지워진 뒤 일꾼이 늦게 써도 폴더를 되살리지 않는다
    v = video(tmp_path)
    d = tmp_path / "job" / "preview" / "thumbs"
    d.parent.mkdir(parents=True)
    m = thumbs.load_meta(d, v, thumbs.plan(50, 960, 540))
    shutil.rmtree(tmp_path / "job")
    with pytest.raises(OSError):
        thumbs.mark_done(d, m, 0)
    assert not (tmp_path / "job").exists()
