"""이미지 겹쳐 넣기(--overlays): 원본 구간 → 결과 구간, 필터 조각, 실제 ffmpeg 로 새긴 픽셀."""
import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

import pipeline as pl
from tests.test_caption_style import _caps, _ffmpeg_with_ass

F = Fraction(30)


def test_windows_follow_cuts():
    keeps = [(0, 90), (120, 300)]                                   # 원본 3~4초가 잘림
    assert pl.overlay_windows(2.0, 5.0, keeps, F) == [(60, 120)]    # 2~3초 + 4~5초가 이어 붙음
    assert pl.overlay_windows(3.0, 4.0, keeps, F) == []             # 통째로 잘림
    assert pl.overlay_windows(0.0, 10.0, keeps, F) == [(0, 270)]


def test_filter_order_and_geometry_rotated():
    ov = [{"path": "/x/a.png", "x": 0.5, "y": 0.1, "w": 0.25, "start": 0.0, "end": 1.0}]
    frag, inputs = pl.overlay_filter(ov, [(0, 300)], F, 1080, 1920, 3)   # 세로 화면 W=1080
    assert inputs == ["-i", "/x/a.png"]
    assert "[3:v]scale=270:-2" in frag and "x=540:y=192" in frag and "between(t," in frag
    assert pl.overlay_filter([], [(0, 300)], F, 1920, 1080, 2) == ("", [])


def test_filter_skips_overlay_fully_cut():
    ov = [{"path": "/x/a.png", "x": 0, "y": 0, "w": 0.1, "start": 3.0, "end": 4.0}]
    assert pl.overlay_filter(ov, [(0, 90), (120, 300)], F, 1920, 1080, 2) == ("", [])


def test_output_size_rotated_and_odd():
    assert pl.output_size({"width": 1921, "height": 1081}) == (1920, 1080)
    assert pl.output_size({"width": 1920, "height": 1080, "rotation": 90}) == (1080, 1920)


def test_load_overlays_drops_bad(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    good = {"path": str(png), "x": 0.1, "y": 0.2, "w": 0.3, "start": 0.0, "end": 1.0}
    p = tmp_path / "o.json"
    p.write_text(json.dumps([good, {**good, "path": str(tmp_path / "없음.png")}, {"x": 1}]), encoding="utf-8")
    assert pl.load_overlays(p) == [good]
    assert pl.load_overlays(None) == []
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert pl.load_overlays(tmp_path / "bad.json") == []


def _png(path, rgba):
    h, w = rgba.shape[:2]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}",
                    "-i", "-", str(path)], input=rgba.tobytes(), check=True)


def _frame(mp4, t, w=320, h=240):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(mp4), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def _render(tmp_path, ovs, burn=None):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    wav = tmp_path / "cut.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2", str(wav)],
                   check=True)
    out = tmp_path / "out.mp4"
    info = {"fps": F, "width": 320, "height": 240}
    pl.render_video(src, wav, None, out, [(0, 60)], info, tmp_path, ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
                    burn, overlays=ovs)
    return out


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")
def test_render_overlay_pixels(tmp_path):
    rgba = np.zeros((8, 8, 4), np.uint8)
    rgba[2:6, 2:6] = (255, 0, 0, 255)        # 가운데 4x4 만 불투명 빨강, 나머지는 투명
    png = tmp_path / "red.png"
    _png(png, rgba)
    out = _render(tmp_path, [{"path": str(png), "x": 0.5, "y": 0.5, "w": 0.1, "start": 0.0, "end": 1.0}])
    img = _frame(out, 0.5)
    # 너비 32px(=8px 의 4배) → 가운데 16x16 이 빨강, 가장자리 8px 은 투명이라 검정
    assert img[136, 176, 0] > 200 and img[136, 176, 1] < 60
    assert img[122, 162].max() < 40                       # 투명 부분은 바탕 그대로
    assert _frame(out, 1.5)[136, 176].max() < 40          # 구간이 끝나면 없음


@pytest.mark.skipif(_ffmpeg_with_ass() is None, reason="libass 가 든 ffmpeg 필요")
def test_overlay_goes_under_subtitles(tmp_path, monkeypatch):
    ass = tmp_path / "s.ass"
    ass.write_text(pl.build_ass([(0.0, 1.0, _caps(("a", "자막"))[0])], {}, 320, 240), encoding="utf-8")
    png = tmp_path / "a.png"
    _png(png, np.full((4, 4, 4), 255, np.uint8))
    monkeypatch.setenv("PATH", str(Path(_ffmpeg_with_ass()).parent) + os.pathsep + os.environ["PATH"])
    _render(tmp_path, [{"path": str(png), "x": 0, "y": 0, "w": 0.1, "start": 0.0, "end": 1.0}], burn=(ass, None))
    script = (tmp_path / "video_filter.txt").read_text(encoding="utf-8")
    assert script.index("overlay=") < script.index("ass=")


def test_overlays_arg_defaults_to_none():
    assert pl.build_parser().parse_args(["x.mp4"]).overlays is None
    assert pl.build_parser().parse_args(["x.mp4", "--overlays", "o.json"]).overlays == Path("o.json")
