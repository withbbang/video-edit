"""BGM 미리보기 섞기(app/ui/bgmmix.js)의 순수 함수: 재생 계획·배율 곡선 (node 로 실행)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BGMMIX = Path(__file__).resolve().parent.parent / "app" / "ui" / "bgmmix.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node 필요")


def run(body: str):
    code = (f"const M = require({json.dumps(str(BGMMIX))});\nlet out;\n{body}\n"
            "process.stdout.write(JSON.stringify(out));")
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def plan(t, total=100):
    return run(f"out = M.bgmPlan([{{id:'a',at:2,in:1,out:5}}], {t}, {total});")


def test_plan_before_during_after():
    assert plan(0) == [{"id": "a", "wait": 2, "offset": 1, "dur": 4}]
    assert plan(4) == [{"id": "a", "wait": 0, "offset": 3, "dur": 2}]
    assert plan(7) == []


def test_plan_clamped_to_total():
    assert plan(0, 3) == [{"id": "a", "wait": 2, "offset": 1, "dur": 1}]
    assert plan(0, 2) == []


def test_gain_curve_db_interpolated():
    c = run("out = Array.from(M.gainCurve([[0,0],[1,-40]], 0, 0, 1, 0.5));")
    assert c == pytest.approx([1, 0.1, 0], abs=1e-6)
    c = run("out = Array.from(M.gainCurve([[0,0],[1,0]], 6, 0, 1, 0.5));")
    assert c[0] == pytest.approx(10 ** (6 / 20), rel=1e-5)


def test_gain_curve_mute_judged_before_gain():
    c = run("out = Array.from(M.gainCurve([[0,-40],[1,-40]], 6, 0, 1, 0.5));")
    assert c == [0, 0, 0]


def test_gain_curve_sorts_points():
    c = run("out = Array.from(M.gainCurve([[1,-40],[0,0]], 0, 0, 1, 0.5));")
    assert c == pytest.approx([1, 0.1, 0], abs=1e-6)
