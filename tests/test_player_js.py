"""화면 재생기(app/ui/player.js)의 순수 함수: 원본↔결과 시간 변환, 구간에 걸친 조각 고르기 (node 로 실행)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLAYER = Path(__file__).resolve().parent.parent / "app" / "ui" / "player.js"
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node 필요")


def run(body: str):
    """player.js 를 불러 body(마지막 식이 out)를 돌리고 out 을 JSON 으로 받는다."""
    code = (f"const P = require({json.dumps(str(PLAYER))});\n"
            "const { timeMap, fragsFor, planVideo } = P;\n"
            f"let out;\n{body}\nprocess.stdout.write(JSON.stringify(out));")
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def js(expr: str, method: str, args: list):
    return run(f"const m = {expr}; out = {json.dumps(args)}.map((a) => m.{method}(a));")


def js_total(keeps: str) -> float:
    return run(f"out = timeMap({keeps}).total;")


@needs_node
def test_time_map_basic():
    m = js("timeMap([[0.5, 2.0], [3.0, 4.0]])", "toOut", [0.0, 0.5, 1.0, 2.5, 3.0, 4.0, 9.0])
    assert m == pytest.approx([0.0, 0.0, 0.5, 1.5, 1.5, 2.5, 2.5])
    assert js_total("[[0.5, 2.0], [3.0, 4.0]]") == pytest.approx(2.5)
    assert js("timeMap([[0.5, 2.0], [3.0, 4.0]])", "toOrig", [0.0, 1.5, 2.5]) == pytest.approx([0.5, 3.0, 4.0])


@needs_node
def test_time_map_cut_at_end_and_short_keep():
    m = "timeMap([[0, 1], [1.5, 1.6]])"
    assert js(m, "toOut", [1.55, 5.0]) == pytest.approx([1.05, 1.1])
    assert js(m, "inCut", [0.5, 1.2, 1.55, 1.7]) == [False, True, False, True]
    assert js(m, "toOrig", [0.5, 1.05, 1.1, 9.0]) == pytest.approx([0.5, 1.55, 1.6, 1.6])


@needs_node
def test_time_map_round_trip_inside_keeps():
    m = "timeMap([[0.2, 1.0], [1.4, 2.0], [3.0, 3.5]])"
    ts = [0.2, 0.7, 1.4, 1.9, 3.0, 3.4]
    outs = js(m, "toOut", ts)
    assert run(f"const m = {m}; out = {json.dumps(outs)}.map((o) => m.toOrig(o));") == pytest.approx(ts)


@needs_node
def test_time_map_empty():
    assert run("const m = timeMap([]); out = [m.total, m.toOut(3), m.toOrig(0), m.inCut(1)];") == [0, 0, 0, True]


@needs_node
def test_frags_for():
    frags = "[[0, 100, 200], [0.5, 200, 300], [1.0, 300, 400]]"
    assert run(f"out = fragsFor({frags}, 0.4, 0.6);") == [[100, 199], [200, 299]]
    assert run(f"out = fragsFor({frags}, 0.6, 1.0);") == [[200, 299]]   # 1.0 에 시작하는 조각은 안 넣는다
    assert run(f"out = fragsFor({frags}, 2.0, 9.0);") == [[300, 399]]   # 마지막 조각은 끝이 없다


@needs_node
def test_plan_video_offsets_and_windows():
    # 남길 구간마다 timestampOffset = o − s, 창 = [o, o + (e − s) + 1프레임]
    frags = "[[0, 100, 200], [0.5, 200, 300], [1.0, 300, 400], [1.5, 400, 500]]"
    p = run(f"out = planVideo({frags}, [[0.2, 0.7], [1.2, 1.6]], 0.04);")
    assert [x["keep"] for x in p] == [0, 0, 1, 1]
    assert [[x["a"], x["b"]] for x in p] == [[100, 199], [200, 299], [300, 399], [400, 499]]
    assert [x["off"] for x in p] == pytest.approx([-0.2, -0.2, -0.7, -0.7])
    assert [x["ws"] for x in p] == pytest.approx([0, 0, 0.5, 0.5])
    assert [x["we"] for x in p] == pytest.approx([0.54, 0.54, 0.94, 0.94])
    # 결과 시간에서 조각이 차지하는 범위 (구간 안으로 잘라서)
    assert [v for x in p for v in (x["from"], x["to"])] == pytest.approx([0, 0.3, 0.3, 0.5, 0.5, 0.8, 0.8, 0.9])


@needs_node
def test_plan_audio_last_piece_ends_at_duration():
    # 마지막 조각 끝이 무한이면 "다 넣었는지" 판단이 영원히 거짓이 된다 → 결과 길이에서 끝낸다
    p = run("out = P.planAudio([[0, 10, 20], [0.5, 20, 30]], 0.8);")
    assert [v for x in p for v in (x["from"], x["to"])] == pytest.approx([0, 0.5, 0.5, 0.8])
    assert [[x["a"], x["b"]] for x in p] == [[10, 19], [20, 29]]


@needs_node
def test_fail_reports_before_teardown():
    # 재생 중 오류: 앱이 원본 위치·재생 여부를 기억할 수 있게 onerror 를 요소를 비우기(currentTime 0) 전에 부른다
    out = run("""
const v = { currentTime: 12.5, paused: false, listeners: 0,
  removeEventListener() {}, pause() { this.paused = true; },
  removeAttribute() { this.currentTime = 0; }, load() {} };
const g = new P.Gapless(v);
let seen = null;
g.onerror = () => { seen = { t: v.currentTime, paused: v.paused, dead: g.dead }; };
g.fail(new Error('x'));
out = { seen, after: v.currentTime, dead: g.dead };
""")
    assert out["seen"] == {"t": 12.5, "paused": False, "dead": False}
    assert out["after"] == 0 and out["dead"] is True
