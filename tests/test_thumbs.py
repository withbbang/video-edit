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
