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
