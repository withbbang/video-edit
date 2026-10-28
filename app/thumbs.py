"""타임라인 썸네일: 미리보기 영상(모든 프레임이 기준 프레임)에서 STEP 초 간격 썸네일을 PER_SHEET 장씩
타일 JPEG(시트)으로 만들어 jobs/<id>/preview/thumbs/ 에 캐시한다. thumbs.json 이 어떤 시트를 다 만들었는지 적는다.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

THUMB_VERSION = 1   # 시트 형식이 바뀌면 올린다 → 옛 시트는 지우고 다시 만든다
STEP = 1.0          # 썸네일 간격 (초)
TILE_H = 90
COLS = 10
ROWS = 10
PER_SHEET = COLS * ROWS


def video_sig(video: Path) -> dict:
    st = Path(video).stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def plan(duration: float, width: int, height: int) -> dict:
    """영상 길이·미리보기 크기 → 시트 배치. 칸 너비는 화면비를 따르고 짝수."""
    tile_w = max(2, round(TILE_H * width / height / 2) * 2)
    count = max(1, math.ceil(duration / STEP))
    return {"v": THUMB_VERSION, "step": STEP, "tile_w": tile_w, "tile_h": TILE_H, "cols": COLS, "rows": ROWS,
            "per_sheet": PER_SHEET, "count": count, "sheets": math.ceil(count / PER_SHEET)}


def sheet_name(i: int) -> str:
    return f"sheet_{i:04d}.jpg"


def _num(x: float) -> str:
    return f"{x:g}"


def sheet_cmd(video: Path, i: int, p: dict, out: Path) -> list[str]:
    """i 번째 시트: [i*per_sheet*step, +per_sheet*step) 를 step 간격으로 뽑아 cols×rows 타일 한 장.
    CPU 를 덜 쓰게 스레드 2개 (분석·저장과 같이 돈다)."""
    span = p["per_sheet"] * p["step"]
    vf = f"fps=1/{_num(p['step'])},scale={p['tile_w']}:{p['tile_h']},tile={p['cols']}x{p['rows']}"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2",
            "-ss", _num(i * span), "-t", _num(span), "-i", str(video),
            "-vf", vf, "-frames:v", "1", "-q:v", "6", str(out)]


def _write(d: Path, meta: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "thumbs.json.part"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, d / "thumbs.json")


def load_meta(d: Path, video: Path, p: dict) -> dict:
    """d/thumbs.json 을 읽는다. 형식·미리보기 영상·배치가 다르면 시트를 지우고 빈 메타를 새로 쓴다."""
    d = Path(d)
    want = {"video": video_sig(video), **p}
    try:
        meta = json.loads((d / "thumbs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict) and {k: meta.get(k) for k in want} == want and isinstance(meta.get("done"), list):
        return meta
    for f in d.glob("sheet_*.jpg"):
        f.unlink(missing_ok=True)
    meta = {**want, "done": []}
    _write(d, meta)
    return meta


def mark_done(d: Path, meta: dict, i: int) -> dict:
    meta = {**meta, "done": sorted(set(meta["done"]) | {i})}
    _write(Path(d), meta)
    return meta
