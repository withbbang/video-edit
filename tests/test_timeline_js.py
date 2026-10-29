"""타임라인 보기 범위 계산(app/ui/timeline.js)의 순수 함수: 확대·따라가기·썸네일 칸 (node 로 실행)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TIMELINE = Path(__file__).resolve().parent.parent / "app" / "ui" / "timeline.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node 필요")


def run(body: str):
    """timeline.js 를 불러 body(마지막에 out 을 채움)를 돌리고 out 을 JSON 으로 받는다."""
    code = (f"const T = require({json.dumps(str(TIMELINE))});\nlet out;\n{body}\n"
            "process.stdout.write(JSON.stringify(out));")
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_fit_and_clamp():
    v = run("out = T.fitView(3600, 1000, 100);")
    assert v == {"t0": 0, "spc": 360}
    assert run("out = T.fitView(120, 950, 96).spc;") == pytest.approx(120 * 96 / 950)   # 줄 너비에 꼭 맞게
    v = run("out = T.clampView({t0: -5, spc: 1e-9}, 3600, 1000, 100, 30);")
    assert v["t0"] == 0 and v["spc"] == pytest.approx(1 / 30)
    v = run("out = T.clampView({t0: 3599, spc: 1}, 3600, 1000, 100, 30);")   # 끝을 넘지 않게
    assert v == {"t0": 3590, "spc": 1}
    v = run("out = T.clampView({t0: 10, spc: 9999}, 3600, 1000, 100, 30);")   # 전체보다 축소 안 됨
    assert v == {"t0": 0, "spc": 360}


def test_zoom_keeps_anchor():
    x0, x1 = run("const v = {t0: 1000, spc: 10}; const z = T.zoomAt(v, 2, 1234, 3600, 1000, 100, 30);"
                 "out = [T.tToX(v, 1234, 100), T.tToX(z, 1234, 100)];")
    assert x0 == pytest.approx(x1)
    z = run("out = T.zoomAt({t0: 1000, spc: 10}, 2, 1234, 3600, 1000, 100, 30);")
    assert z["spc"] == 5


def test_t_x_roundtrip():
    assert run("const v = {t0: 12, spc: 0.5}; out = T.xToT(v, T.tToX(v, 17.25, 80), 80);") == pytest.approx(17.25)


def test_follow_pages():
    v = {"t0": 100, "spc": 1}   # w 1000, cw 100 → 보이는 범위 [100, 110)
    assert run(f"out = T.followT({json.dumps(v)}, 105, 3600, 1000, 100, 30);") == v
    f = run(f"out = T.followT({json.dumps(v)}, 200, 3600, 1000, 100, 30);")
    assert f["spc"] == 1 and run(f"out = T.tToX({json.dumps(f)}, 200, 100);") == pytest.approx(100)   # 0.1*w
    f = run(f"out = T.followT({json.dumps(v)}, 50, 3600, 1000, 100, 30);")
    assert f["t0"] == pytest.approx(49)


def test_play_follow_scrolls_continuously():
    v = {"t0": 100, "spc": 1}   # 보이는 범위 [100, 110), 가운데 105
    assert run(f"out = T.playFollow({json.dumps(v)}, 103, 3600, 1000, 100, 30);") == v   # 가운데 전엔 그대로
    f = run(f"out = T.playFollow({json.dumps(v)}, 105.3, 3600, 1000, 100, 30);")
    assert f["spc"] == 1 and f["t0"] == pytest.approx(100.3)   # 가운데를 넘으면 재생 위치를 가운데에 두고 같이 흐른다
    f = run(f"out = T.playFollow({json.dumps(v)}, 300, 3600, 1000, 100, 30);")
    assert f["t0"] == pytest.approx(295)   # 보기 밖(찾아가기)이면 가운데로
    f = run(f"out = T.playFollow({json.dumps(v)}, 3599, 3600, 1000, 100, 30);")
    assert f["t0"] == pytest.approx(3590)   # 끝에선 더 넘기지 않는다


def test_play_spc_limits_scroll_speed():
    # 칸 100px, 초당 160px 까지 → 칸 하나 0.625초보다 잘게는 재생하지 않는다
    assert run("out = T.playSpc(1 / 30, 100, 160);") == pytest.approx(0.625)
    assert run("out = T.playSpc(2, 100, 160);") == 2   # 이미 충분히 느리면 그대로


def test_cells():
    c = run("out = T.cells({t0: 10.5, spc: 1}, 13, 400, 100);")
    assert [x["t"] for x in c] == [10, 11, 12] and c[0]["x"] == pytest.approx(-50)


def test_sheet_cell():
    meta = {"step": 1, "per_sheet": 100, "cols": 10, "count": 250}
    assert run(f"out = T.sheetCell(123.4, {json.dumps(meta)});") == {"sheet": 1, "col": 3, "row": 2}
    assert run(f"out = T.sheetCell(250, {json.dumps(meta)});") is None


def test_wave_max():
    b = "new Uint8Array([0, 10, 200, 30, 5])"
    assert run(f"out = T.waveMax({b}, 0.01, 0.01, 0.035);") == 200
    assert run(f"out = T.waveMax({b}, 0.01, 0.031, 0.032);") == 30   # 한 값보다 좁아도 그 자리 값
    assert run(f"out = T.waveMax({b}, 0.01, 0.2, 0.3);") == 0        # 밖


def test_slider_roundtrip():
    s = run("out = [T.spcToSlider(1/30, 1/30, 360), T.spcToSlider(360, 1/30, 360),"
            " T.sliderToSpc(T.spcToSlider(2, 1/30, 360), 1/30, 360)];")
    assert s[0] == 1000 and s[1] == 0 and s[2] == pytest.approx(2)


def test_zoom_past_limit_keeps_anchor():
    # 이미 1프레임/칸인데 더 확대해도 마우스 자리의 시간이 옆으로 밀리지 않는다
    x0, x1 = run("const v = {t0: 29.9, spc: 1/30}; const z = T.zoomAt(v, 3, 30, 120, 960, 96, 30);"
                 "out = [T.tToX(v, 30, 96), T.tToX(z, 30, 96)];")
    assert x0 == pytest.approx(x1)


# ---- 이미지 겹쳐 넣기: 화면 자리·끌기 ---------------------------------------------
BOX = "{x: 10, y: 20, w: 1600, h: 900}"
O = "{x: 0.1, y: 0.2, w: 0.25, aspect: 2}"


def test_ov_rect():
    r = run(f"out = T.ovRect({O}, {BOX});")
    assert r == pytest.approx({"x": 170, "y": 200, "w": 400, "h": 200})
    r = run("out = T.ovRect({x: 0, y: 0, w: 0.5, aspect: 1}, {x: 0, y: 0, w: 900, h: 1600});")   # 세로 영상
    assert r["h"] == pytest.approx(450)


def test_ov_move_clamps_inside():
    assert run(f"out = T.ovMove({O}, 160, 90, {BOX});") == pytest.approx({"x": 0.2, "y": 0.3})
    m = run(f"out = T.ovMove({O}, 99999, 99999, {BOX});")        # 오른쪽 아래 끝까지만
    assert m["x"] == pytest.approx(0.75) and m["y"] == pytest.approx(1 - 200 / 900)
    assert run(f"out = T.ovMove({O}, -99999, -99999, {BOX});") == {"x": 0, "y": 0}


def test_ov_rect_free_height():
    r = run("out = T.ovRect({x: 0, y: 0, w: 0.5, h: 0.1, aspect: 2}, {x: 0, y: 0, w: 1600, h: 900});")
    assert r == pytest.approx({"x": 0, "y": 0, "w": 800, "h": 90})


def test_ov_resize_edges_independent():
    o = "{x: 0.25, y: 0.25, w: 0.5, h: 0.5, aspect: 1}"
    bx = "{x: 0, y: 0, w: 1000, h: 1000}"
    # 위 변만: 위로 100px → 위쪽만 늘어남
    assert run(f"out = T.ovResizeEdge({o}, 'n', 0, -100, {bx}, false);") == pytest.approx({"x": 0.25, "y": 0.15, "w": 0.5, "h": 0.6})
    # 왼쪽 변만
    assert run(f"out = T.ovResizeEdge({o}, 'w', -50, 999, {bx}, false);") == pytest.approx({"x": 0.2, "y": 0.25, "w": 0.55, "h": 0.5})
    # 오른쪽 아래 모서리: 가로·세로 따로
    assert run(f"out = T.ovResizeEdge({o}, 'se', 100, -200, {bx}, false);") == pytest.approx({"x": 0.25, "y": 0.25, "w": 0.6, "h": 0.3})
    # Shift(비율 유지) 모서리: 가로 기준, 반대쪽 모서리 고정
    assert run(f"out = T.ovResizeEdge({o}, 'nw', -100, 0, {bx}, true);") == pytest.approx({"x": 0.15, "y": 0.15, "w": 0.6, "h": 0.6})


def test_ov_resize_edges_clamped():
    o = "{x: 0.25, y: 0.25, w: 0.5, h: 0.5, aspect: 1}"
    bx = "{x: 0, y: 0, w: 1000, h: 1000}"
    assert run(f"out = T.ovResizeEdge({o}, 'e', 99999, 0, {bx}, false);") == pytest.approx({"x": 0.25, "y": 0.25, "w": 0.75, "h": 0.5})
    assert run(f"out = T.ovResizeEdge({o}, 'n', 0, 99999, {bx}, false);") == pytest.approx({"x": 0.25, "y": 0.73, "w": 0.5, "h": 0.02})
    assert run(f"out = T.ovResizeEdge({o}, 'se', 99999, 0, {bx}, true);") == pytest.approx({"x": 0.25, "y": 0.25, "w": 0.75, "h": 0.75})
    # 높이가 없는 예전 항목은 그림 비율에서 높이를 구해 시작
    r = run("out = T.ovResizeEdge({x: 0, y: 0, w: 0.5, aspect: 2}, 's', 0, 0, {x: 0, y: 0, w: 1600, h: 900}, false);")
    assert r["h"] == pytest.approx(400 / 900)


def test_bar_drag():
    assert run("out = T.barDrag('end', 1, 2, -5, 10, 30);") == pytest.approx({"start": 1, "end": 1 + 1 / 30})
    assert run("out = T.barDrag('move', 8, 9.5, 3, 10, 30);") == pytest.approx({"start": 8.5, "end": 10})
    assert run("out = T.barDrag('move', 1, 2, -5, 10, 30);") == pytest.approx({"start": 0, "end": 1})
    assert run("out = T.barDrag('start', 1, 2, 0.51, 10, 30);") == pytest.approx({"start": 1.5, "end": 2})   # 프레임에 맞춤
    assert run("out = T.barDrag('start', 1, 2, 5, 10, 30);") == pytest.approx({"start": 2 - 1 / 30, "end": 2})
    assert run("out = T.barDrag('end', 1, 2, 50, 10, 30);") == pytest.approx({"start": 1, "end": 10})


def test_lanes_drag():
    # 위로 끌면(dy<0) 커지고 아래로 끌면 작아진다: 한 줄(20)보다 작게, 내용(140)보다 크게 안 됨
    assert run("out = T.lanesDrag(80, -30, 140, 400, 120, 20);") == 110
    assert run("out = T.lanesDrag(80, 200, 140, 400, 120, 20);") == 20
    assert run("out = T.lanesDrag(80, -500, 140, 400, 120, 20);") == 140
    # 영상이 120px 아래로 줄지 않게: 영상 150 이면 30 만큼만 더 커진다
    assert run("out = T.lanesDrag(80, -100, 300, 150, 120, 20);") == 110
    # 이미 영상이 최소 이하여도 줄이는 건 된다, 키우지는 않는다
    assert run("out = T.lanesDrag(80, -10, 300, 100, 120, 20);") == 80
    assert run("out = T.lanesDrag(80, 10, 300, 100, 120, 20);") == 70
    # 내용이 한 줄보다 작으면 내용 높이
    assert run("out = T.lanesDrag(16, 5, 16, 400, 120, 20);") == 16
