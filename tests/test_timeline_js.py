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


def test_clip_bands():
    # 보기 [10, 20) 초: w 1000, cw 100, spc 1 → 1초 = 100px
    v = '{t0: 10, spc: 1}'
    pieces = ('[{clip: "b", start: 2, end: 5}, {clip: "b", start: 12, end: 13.5}, {clip: "c", start: 18, end: 25},'
              ' {clip: "d", start: 20, end: 22}, {clip: "e", start: 9, end: 11}]')
    out = run(f"out = T.clipBands({pieces}, {v}, 1000, 100);")
    # 보기 밖(b 첫 조각, d)은 빠지고, 걸친 것은 화면 끝에서 자른다
    assert out == [{"clip": "b", "x0": 200, "x1": 350}, {"clip": "c", "x0": 800, "x1": 1000},
                   {"clip": "e", "x0": 0, "x1": 100}]
    assert run(f"out = T.clipBands([], {v}, 1000, 100);") == []


def test_tl_data_key():
    """썸네일·소리 줄을 다시 읽을지: 같은 작업이라도 길이나 조각 자리(넣기·빼기·실행 취소)가 바뀌면 키가 달라진다."""
    base = '{duration: 10, layout: []}'
    k = run(f'out = [T.tlDataKey("j", {base}), T.tlDataKey("j", {base}), T.tlDataKey("k", {base}),'
            ' T.tlDataKey("j", {duration: 12, layout: []}),'
            ' T.tlDataKey("j", {duration: 12, layout: [["a1", 0, 4], ["b1", 4, 6], ["a2", 6, 12]]}),'
            ' T.tlDataKey("j", {duration: 12, layout: [["a1", 0, 8], ["b1", 8, 10], ["a2", 10, 12]]}),'
            ' T.tlDataKey("j", {duration: 10})];')
    assert k[0] == k[1] == k[6]
    assert len({k[0], k[2], k[3], k[4], k[5]}) == 5


def test_snap_and_drop_target():
    assert run("out = T.snapAt(5.3, [[4.6, 5.5]], [5], 30, 10);") == 5
    assert run("out = T.snapAt(3.0, [[2.6, 3.4]], [5], 30, 10);") == pytest.approx(2.6)
    assert run("out = T.dropTarget(5.3, [[4.6, 5.5]], [5, 7], 30, 10, [5, 7]);") is None      # 제자리(시작)
    assert run("out = T.dropTarget(6.0, [], [5, 7], 30, 10, [5, 7]);") is None               # 덩어리 안
    assert run("out = T.dropTarget(8.0, [], [5, 7], 30, 10, [5, 7]);") == 8
    assert run("out = [T.isDrag(3, 2), T.isDrag(4, 1)];") == [False, True]
    # 겹치거나 맞닿은 잘린 구간은 합쳐서 본다 (서버 snap_at 은 합친 컷을 받음)
    assert run("out = T.snapAt(5.75, [[5.0, 5.8], [5.7, 6.4]], [6.0], 30, 100);") == pytest.approx(6.0)
    assert run("out = T.snapAt(5.5, [[5.0, 6.0], [6.0, 6.4]], [6.0], 30, 100);") == pytest.approx(6.0)


# ---- 음원(BGM) 줄 ----
PLAYER = Path(__file__).resolve().parent.parent / "app" / "ui" / "player.js"


def run_bgm(body: str):
    """timeline.js(T)와 player.js 의 timeMap(map) 을 불러 body 를 돌린다."""
    return run(f"const {{ timeMap }} = require({json.dumps(str(PLAYER))});\n"
               "const map = timeMap([[0, 1], [3, 10]]);\n" + body)


def test_bgm_lanes():
    out = run('out = T.bgmLanes([{id:"c",at:6,in:0,out:3},{id:"b",at:3,in:0,out:5},{id:"a",at:0,in:0,out:5}]);')
    assert out == {"a": 0, "b": 1, "c": 0}
    out = run('out = T.bgmLanes([{id:"y",at:0,in:0,out:2},{id:"x",at:0,in:0,out:2}]);')   # 같은 at 은 id 순
    assert out == {"x": 0, "y": 1}


def test_bgm_span():
    # keeps [[0,1],[3,10]] → 결과 길이 8
    assert run_bgm("out = T.bgmSpan({at:0.5,in:0,out:2}, map);") == {"o0": 0.5, "o1": 4.5, "cut": False}
    r = run_bgm("out = T.bgmSpan({at:7.5,in:0,out:3}, map);")
    assert r["cut"] is True and r["o1"] == 10 and r["o0"] == pytest.approx(9.5)
    assert run_bgm("out = T.bgmSpan({at:9,in:0,out:3}, map);") is None


def test_bgm_polyline_flat_over_cut():
    # w 1000, cw 100, 보기 [0,10) → 1초 = 100px. 곡 0~2초: 0dB → -20dB, 결과 0~2 = 원본 0~1, 3~4
    pts = run_bgm("const v = {t0: 0, spc: 1};"
                  "out = T.bgmPolyline({at:0,in:0,out:2}, [[0,0],[2,-20]], map, v, 100, 46);")
    first = [p for p in pts if p[0] <= 100 + 1e-6]
    # 원본 1초(앞 조각 끝)와 3초(다음 조각 시작) 사이는 평평: x=100 점과 x=300 점의 y 가 같다
    y_at_100 = [y for x, y in pts if abs(x - 100) < 1e-6]
    y_at_300_first = [y for x, y in pts if abs(x - 300) < 1e-6][0]
    assert y_at_100[-1] == pytest.approx(y_at_300_first)
    assert first[0][0] == 0 and first[0][1] == pytest.approx(run("out = T.dbToY(0, 46);"))
    assert pts[-1][0] == pytest.approx(400) and pts[-1][1] == pytest.approx(run("out = T.dbToY(-20, 46);"))


def test_bgm_snap():
    assert run("out = T.bgmSnap(15.9, 4, 20, [], 10);") == pytest.approx(16)     # 끝 맞춤
    assert run("out = T.bgmSnap(0.5, 4, 20, [], 10);") == 0
    assert run("out = T.bgmSnap(2.95, 4, 20, [7], 10);") == pytest.approx(3)     # 끝이 7 에 붙음
    assert run("out = T.bgmSnap(9, 4, 20, [7], 10);") == 9                       # 멀면 그대로
    assert run("out = T.bgmSnap(0.5, 4, 20, [], 10, 2);") == 0.5                 # thr 보다 멀면 그대로


def test_bgm_drag():
    b = "{at:5,in:2,out:6,dur:10}"
    assert run(f"out = T.bgmDrag('end', {b}, 9, 30);")["out"] == 10
    assert run(f"out = T.bgmDrag('end', {b}, -9, 30);")["out"] == pytest.approx(2.1)
    r = run(f"out = T.bgmDrag('start', {b}, -5, 30);")
    assert (r["in"], r["at"], r["out"]) == (0, 3, 6)
    assert run(f"out = T.bgmDrag('start', {b}, 9, 30);")["in"] == pytest.approx(5.9)
    r = run("out = T.bgmDrag('start', {at:1,in:2,out:6,dur:10}, -5, 30);")   # at >= 0 에서 막힘
    assert (r["in"], r["at"]) == (1, 0)
    assert run(f"out = T.bgmDrag('move', {b}, -9, 30);")["at"] == 0
    assert run(f"out = T.bgmDrag('move', {b}, 99, 30);")["at"] == pytest.approx(29.9)   # 결과 끝 밖으로 빠져 안 보이게 되지 않음


def test_bgm_axis_and_x():
    assert run("out = [T.dbToY(6, 92), T.dbToY(-40, 92), T.yToDb(-5, 92), T.yToDb(999, 92), T.yToDb(46, 92)];") == [0, 92, 6, -40, -17]
    # 보기 [0,10): x 300 → 원본 3초 → 결과 1초, 잘린 구간 안(x 200 = 원본 2초)은 그 구간 끝(결과 1초)
    assert run_bgm("const v = {t0: 0, spc: 1}; out = [T.xToOutT(300, map, v, 100), T.xToOutT(200, map, v, 100)];") == [1, 1]


def test_db_at_matches_python():
    from app import bgm
    cases = [([[0, 0], [2, -20]], t) for t in (-1, 0, 1, 2, 3)] + \
            [([[4, -3], [1, 2], [2, -8]], t) for t in (0, 1, 1.5, 2, 3, 4, 9)] + [([], 1), ([[1, -5]], 0), ([[1, -5], [1, 3]], 1)]
    got = run("out = " + json.dumps(cases) + ".map(([p, t]) => T.dbAt(p, t));")
    assert got == pytest.approx([bgm.db_at(p, t) for p, t in cases])


def test_db_at_matches_python_after_tie_fix():
    # 같은 시간 점은 JS(정렬 순서)와 렌더(np.interp)가 다르게 보간한다 → 서버가 strict_points 로 풀어 저장한 점은 둘이 같다
    import sys
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import pipeline
    from app import bgm
    pts = bgm.strict_points([[0, 0], [5, 0], [5, -20], [10, -20], [10, -3]], 0, 30)
    ts = [0, 2.5, 5, 5.0005, 5.001, 7.5, 10, 10.0005, 20]
    got = run("out = " + json.dumps(ts) + ".map((t) => T.dbAt(" + json.dumps(pts) + ", t));")
    assert got == pytest.approx([bgm.db_at(pts, t) for t in ts])
    assert got == pytest.approx(list(pipeline.bgm_db_at(pts, np.array(ts, dtype=float))))


def test_time_map_pieces():
    assert run_bgm("out = map.pieces;") == [{"s": 0, "e": 1, "o": 0}, {"s": 3, "e": 10, "o": 1}]


def test_bgm_polyline_many_keeps():
    # 1000 개 남길 구간: [2i, 2i+1], 결과 시간 i 초가 원본 2i. 곡 0~1000초 0dB → -40dB 직선, 1초 = 10px
    body = ("const keeps = Array.from({length: 1000}, (_, i) => [2 * i, 2 * i + 1]);"
            "const m = timeMap(keeps); const v = {t0: 500, spc: 1};"
            "out = T.bgmPolyline({at: 0, in: 0, out: 1000}, [[0, 0], [1000, -40]], m, v, 10, 46, 100);")
    pts = run_bgm(body)
    xs = [p[0] for p in pts]
    assert xs == sorted(xs) and len(pts) < 100                      # 보이는 구간 [500, 510) 만
    # 원본 501~502 는 잘린 구간: x(501)=10 과 x(502)=20 에서 y 가 같다
    y = lambda x: [q[1] for q in pts if abs(q[0] - x) < 1e-6]
    assert y(10)[-1] == pytest.approx(y(20)[0])
    assert y(30)[-1] == pytest.approx(y(40)[0])
    assert y(30)[0] > y(20)[-1]                                     # 구간 안(20 → 30)에서는 곡선이 내려감 (y 는 아래로 커짐)
