"""자막 모델: 파이프라인의 build_captions 를 앱 프로세스에서 불러 어절·줄 목록을 만들고,
찾아 바꾸기 계획과 줄 시간 한계를 계산한다. 시간은 모두 원본 영상 기준 초."""
from __future__ import annotations

import sys

from . import paths

MIN_LINE_SEC = 0.3
STYLE_KEYS = ("font", "size", "color", "bold")   # 글자 꾸밈 항목 (pipeline.clean_style)


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


def captions(cuts_json: dict, transcript: dict, cfg: dict, word_edits: dict, line_times: dict,
             word_styles: dict | None = None) -> list[dict]:
    pl = pipeline_module()
    cuts = pl.cuts_from_dicts(cuts_json.get("cuts", []), _fps(cuts_json), _total_frames(cuts_json, transcript))
    remap = pl.TimeRemap(cuts)
    caps = pl.build_captions(transcript, remap.cut_words, remap, cfg["_always"], cfg["_single_only"],
                             word_edits=word_edits or {}, keep_empty=True)
    timed = pl.resolve_line_times(caps, line_times)   # SRT 와 같은 규칙: 저장 key 가 든 줄에 붙는다
    for c in caps:
        for w in c["words"]:
            w["edited"] = w["key"] in (word_edits or {})
            # 글자마다 꾸밈 (지금 글자 수에 맞춤 — 어절을 고쳐 길이가 바뀌면 앞에서부터 글자 순서대로 남는다)
            per = (word_styles or {}).get(w["key"]) or []
            n = len(w["text"])
            w["style"] = ([per[i] if i < len(per) else None for i in range(n)]
                          if any(per[:n]) else None)
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


def _remap_styles(old: str, new: str, styles: list) -> list:
    """글자 꾸밈을 고친 글자에 옮긴다: 그대로인 글자는 제 꾸밈, 바꾼 글자는 같은 자리 옛 글자 꾸밈, 새로 넣은 글자는 앞 글자 꾸밈."""
    import difflib
    st = list(styles) + [None] * (len(old) - len(styles))
    out: list = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            out += st[i1:i2]
        elif op == "replace":
            out += [st[min(i1 + k, i2 - 1)] for k in range(j2 - j1)]
        elif op == "insert":
            near = out[-1] if out else (st[i1] if i1 < len(st) else None)
            out += [near] * (j2 - j1)
    while out and out[-1] is None:
        out.pop()
    return out


def plan_line(words: list[dict], new_text: str, styles: dict) -> tuple[dict[str, str], dict[str, list]]:
    """한 줄 글자를 통째로 고친다. words = 그 줄 어절({key, text}), styles = 어절 키 → 글자별 꾸밈.
    공백으로 나눈 새 낱말을 지금 보이는 어절과 맞춰(difflib) 바뀐 어절만 고친다:
    바뀐 묶음은 앞 어절부터 하나씩, 남는 낱말은 그 묶음 마지막 어절에 붙이고, 모자라면 뒤 어절은 뺀다(빈 글자).
    새로 끼운 낱말은 앞 어절 뒤에(줄 맨 앞이면 첫 어절 앞에) 붙인다. 뺀 어절(빈 글자)은 그대로 둔다.
    → (어절 키 → 새 글자, 어절 키 → 옮긴 꾸밈) — 바뀐 어절만."""
    import difflib
    live = [w for w in words if w["text"]]
    toks = new_text.split()
    if not live:                                    # 다 뺀 줄에 글자를 넣으면 첫 어절을 되살린다
        if not toks or not words:
            return {}, {}
        live = [words[0]]
        old = [""]
    else:
        old = [w["text"] for w in live]
    texts = list(old)
    head: list[str] = []
    sm = difflib.SequenceMatcher(None, old, toks, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        new = toks[j1:j2]
        if op == "insert":
            if i1 == 0:
                head += new
            else:
                texts[i1 - 1] = " ".join([texts[i1 - 1], *new])
            continue
        n = i2 - i1
        for k in range(n):
            if k < n - 1:
                texts[i1 + k] = new[k] if k < len(new) else ""
            else:
                texts[i1 + k] = " ".join(new[k:])
    if head:
        texts[0] = " ".join([*head, texts[0]]).strip()
    changes, restyled = {}, {}
    for w, t0, t in zip(live, old, texts):
        if t == t0:
            continue
        changes[w["key"]] = t
        per = styles.get(w["key"])
        if per:
            restyled[w["key"]] = _remap_styles(t0, t, per)
    return changes, restyled


def line_bounds(caps: list[dict], key: str) -> tuple[float, float]:
    """줄 시간을 옮길 수 있는 범위: (앞 줄 끝, 다음 줄 시작). 없으면 (0.0, inf). SRT 에 없는 빈 줄은 이웃으로 치지 않는다."""
    caps = [c for c in caps if c["key"] == key or not c.get("empty")]
    for i, c in enumerate(caps):
        if c["key"] == key:
            lo = caps[i - 1]["end"] if i > 0 else 0.0
            hi = caps[i + 1]["start"] if i + 1 < len(caps) else float("inf")
            return float(lo), float(hi)
    raise KeyError(key)
