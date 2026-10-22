"""자막 모델: 파이프라인의 build_captions 를 앱 프로세스에서 불러 어절·줄 목록을 만들고,
찾아 바꾸기 계획과 줄 시간 한계를 계산한다. 시간은 모두 원본 영상 기준 초."""
from __future__ import annotations

import sys

from . import paths

MIN_LINE_SEC = 0.3


def pipeline_module():
    """scripts/pipeline.py 를 처음 쓸 때 불러온다 (import 만으로는 아무 일도 하지 않는다)."""
    scripts = str(paths.bundle_dir() / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import pipeline
    return pipeline


def fillers_path():
    user = paths.data_dir() / "fillers.json"
    return user if user.is_file() else paths.bundle_dir() / "fillers.json"


def load_cfg() -> dict:
    return pipeline_module().load_fillers(fillers_path())


def transcript_sha1(tr: dict) -> str:
    return pipeline_module().transcript_sha1(tr)


def transcript_words(tr: dict) -> dict[str, str]:
    """어절 키 "seg:i" → 원래 글자"""
    return {f"{s['id']}:{i}": w["word"].strip()
            for s in tr.get("segments", []) for i, w in enumerate(s.get("words", []))}


def _fps(cuts_json: dict):
    from fractions import Fraction
    try:
        return Fraction(str(cuts_json.get("fps") or "30/1"))
    except (ValueError, ZeroDivisionError):
        return Fraction(30)


def _total_frames(cuts_json: dict, transcript: dict) -> int:
    try:
        dur = float(transcript.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return int(round(dur * float(_fps(cuts_json)))) if dur > 0 else 10 ** 9


def captions(cuts_json: dict, transcript: dict, cfg: dict, word_edits: dict, line_times: dict) -> list[dict]:
    pl = pipeline_module()
    cuts = pl.cuts_from_dicts(cuts_json.get("cuts", []), _fps(cuts_json), _total_frames(cuts_json, transcript))
    remap = pl.TimeRemap(cuts)
    caps = pl.build_captions(transcript, remap.cut_words, remap, cfg["_always"], cfg["_single_only"],
                             word_edits=word_edits or {}, keep_empty=True)
    timed = pl.resolve_line_times(caps, line_times)   # SRT 와 같은 규칙: 저장 key 가 든 줄에 붙는다
    for c in caps:
        for w in c["words"]:
            w["edited"] = w["key"] in (word_edits or {})
        # 어절을 모두 비운 줄: 화면에서 되돌릴 수 있게 남기지만 SRT 에는 없다
        c["empty"] = not "".join(w["text"] for w in c["words"]).strip()
        hit = timed.get(c["key"])
        c["timed"] = hit is not None
        if hit is not None:
            c["start"], c["end"] = float(hit[1][0]), float(hit[1][1])
    return caps


def plan_replace(caps: list[dict], find: str, repl: str) -> tuple[dict[str, str], int]:
    needle = "".join((find or "").split())
    if not needle:
        return {}, 0
    ops: dict[str, list[tuple[int, int, str]]] = {}    # 어절 키 → [(시작, 끝, 바꿀 글자)]
    texts: dict[str, str] = {}
    count = 0
    for line in caps:
        words = line["words"]
        flat, owner = "", []                            # 공백 없는 줄 글자, 글자마다 (어절 순번, 어절 안 위치)
        for wi, w in enumerate(words):
            for ci, ch in enumerate(w["text"]):
                if not ch.isspace():
                    flat += ch
                    owner.append((wi, ci))
        pos = 0
        while True:
            at = flat.find(needle, pos)
            if at < 0:
                break
            pos = at + len(needle)
            count += 1
            (a, ao), (b, bo) = owner[at], owner[pos - 1]
            for wi in range(a, b + 1):
                w = words[wi]
                texts[w["key"]] = w["text"]
                s = ao if wi == a else 0
                e = bo + 1 if wi == b else len(w["text"])
                ops.setdefault(w["key"], []).append((s, e, repl if wi == a else ""))
    out = {}
    for key, lst in ops.items():
        t, res, last = texts[key], "", 0
        for s, e, r in sorted(lst):
            res += t[last:s] + r
            last = e
        res += t[last:]
        if res != t:
            out[key] = res
    return out, count


def replace_plan(caps: list[dict], find: str, repl: str) -> dict[str, str]:
    """바뀔 어절 키 → 새 글자. 한 줄 안에서 공백을 무시하고(대소문자 구분) 찾는다.
    여러 어절에 걸치면 첫 어절=앞부분+repl, 끝 어절=뒷부분, 사이=빈 글자."""
    return plan_replace(caps, find, repl)[0]


def line_bounds(caps: list[dict], key: str) -> tuple[float, float]:
    """줄 시간을 옮길 수 있는 범위: (앞 줄 끝, 다음 줄 시작). 없으면 (0.0, inf). SRT 에 없는 빈 줄은 이웃으로 치지 않는다."""
    caps = [c for c in caps if c["key"] == key or not c.get("empty")]
    for i, c in enumerate(caps):
        if c["key"] == key:
            lo = caps[i - 1]["end"] if i > 0 else 0.0
            hi = caps[i + 1]["start"] if i + 1 < len(caps) else float("inf")
            return float(lo), float(hi)
    raise KeyError(key)
