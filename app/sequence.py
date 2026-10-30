"""영상 끼워 넣기: 편집 시간 밀기·빼기, 자막 키 옮기기, 보관용 편집 뽑기/넣기 (순수 함수).

앱의 편집 시간은 모두 "이어 붙인 영상의 초" 다. 시간은 호출하는 쪽이 프레임 격자에 맞춰 넘긴다.
`Edits` 는 Job 필드 사본 {restored, range_ops, word_edits, line_times, word_styles, overlays}.
"""
import copy
import secrets

EPS = 1e-6
EDIT_KEYS = ("restored", "range_ops", "word_edits", "line_times", "word_styles", "overlays")


def _copy(e: dict) -> dict:
    out = {k: copy.deepcopy(e.get(k)) for k in EDIT_KEYS}
    for k in ("restored", "range_ops", "overlays"):
        out[k] = out[k] or []
    for k in ("word_edits", "line_times", "word_styles"):
        out[k] = out[k] or {}
    return out


def clip_spans(pieces: list, clip: str) -> list:
    """그 clip 조각들의 이어 붙인 (시작, 끝, 파일 안 in)."""
    spans, pos = [], 0.0
    for p in pieces:
        d = float(p["out"]) - float(p["in"])
        if p.get("clip") == clip:
            spans.append((pos, pos + d, float(p["in"])))
        pos += d
    return spans


def split_piece(pieces: list, at: float, new: dict) -> list:
    """이어 붙인 시간 at 이 걸친 조각을 둘로 가르고 그 사이에 new 를 넣는다. 경계면 가르지 않는다."""
    out, pos, placed = [], 0.0, False
    for p in pieces:
        a, b = pos, pos + float(p["out"]) - float(p["in"])
        pos = b
        if not placed and abs(at - a) <= EPS:
            out.append(new)
            placed = True
        if not placed and a + EPS < at < b - EPS:
            cut_in = float(p["in"]) + (at - a)
            out.append({**p, "out": cut_in})
            out.append(new)
            out.append({**p, "id": secrets.token_hex(4), "in": cut_in})
            placed = True
            continue
        out.append(p)
    if not placed:
        out.append(new)
    return out


# ---- 넣기 -------------------------------------------------------------------

def _ins_span(s, e, at, d):
    """[s,e] 를 at 에서 +d 밀기 → 조각 목록 (가로지르면 둘)."""
    if s >= at - EPS:
        return [(s + d, e + d)]
    if e <= at + EPS:
        return [(s, e)]
    return [(s, at), (at + d, e + d)]


def shift_for_insert(e: dict, at: float, d: float) -> dict:
    """at 에 길이 d 를 넣을 때: at 이후 시간 +d. 가로지르는 구간은 둘로, 이미지는 끝만 민다."""
    r = _copy(e)
    r["restored"] = [[a, b] for s, t in r["restored"] for a, b in _ins_span(float(s), float(t), at, d)]
    ops = []
    for o in r["range_ops"]:
        for a, b in _ins_span(float(o["start"]), float(o["end"]), at, d):
            ops.append({**o, "start": a, "end": b})
    r["range_ops"] = ops
    for o in r["overlays"]:
        s, t = float(o["start"]), float(o["end"])
        if s >= at - EPS:
            o["start"], o["end"] = s + d, t + d
        elif t > at + EPS:
            o["end"] = t + d
    for k, (s, t) in list(r["line_times"].items()):
        if s >= at - EPS:
            r["line_times"][k] = [s + d, t + d]
        elif t > at + EPS:
            r["line_times"][k] = [s, t + d]
    return r


# ---- 빼기 -------------------------------------------------------------------

def _rm_span(s, e, a, b):
    """[s,e] 에서 [a,b) 를 빼고 뒤를 당긴 결과. 통째로 들면 None."""
    n = b - a
    if e <= a + EPS:
        return (s, e)
    if s >= b - EPS:
        return (s - n, e - n)
    if s >= a - EPS and e <= b + EPS:
        return None
    ns = s if s < a else a
    ne = a if e <= b else e - n
    if ne - ns <= EPS:
        return None
    return (ns, ne)


def shift_for_remove(e: dict, spans: list) -> dict:
    """빼는 구간들 안에 완전히 든 편집은 지우고, 걸친 것은 잘라내고, 뒤는 당긴다.

    자막 키(word_edits·word_styles)는 시간을 몰라 건드리지 않는다 — API 가 to_stable/from_stable 로 옮긴다.
    """
    r = _copy(e)
    for a, b in sorted(spans, reverse=True):  # 뒤쪽 구간부터: 앞쪽 구간의 좌표가 안 변한다
        restored = []
        for s, t in r["restored"]:
            x = _rm_span(float(s), float(t), a, b)
            if x:
                restored.append([x[0], x[1]])
        r["restored"] = restored
        ops = []
        for o in r["range_ops"]:
            x = _rm_span(float(o["start"]), float(o["end"]), a, b)
            if x:
                ops.append({**o, "start": x[0], "end": x[1]})
        r["range_ops"] = ops
        ovs = []
        for o in r["overlays"]:
            x = _rm_span(float(o["start"]), float(o["end"]), a, b)
            if x:
                ovs.append({**o, "start": x[0], "end": x[1]})
        r["overlays"] = ovs
        lt = {}
        for k, (s, t) in r["line_times"].items():
            x = _rm_span(float(s), float(t), a, b)
            if x:
                lt[k] = [x[0], x[1]]
        r["line_times"] = lt
    return r


# ---- 자막 키 ----------------------------------------------------------------

def to_stable(keys: dict, tr: dict) -> dict:
    """"seg:i" → "fkey|sid|w0+i" (전사가 바뀌어도 변하지 않는 키). 전사에 없는 키는 버린다."""
    segs = {str(s["id"]): s for s in tr.get("segments", [])}
    out = {}
    for k, v in keys.items():
        seg_id, _, i = str(k).partition(":")
        s = segs.get(seg_id)
        src = (s or {}).get("src")
        if not src or not i.isdigit():
            continue
        out[f"{src['fkey']}|{src['sid']}|{int(src['w0']) + int(i)}"] = copy.deepcopy(v)
    return out


def from_stable(keys: dict, tr: dict) -> dict:
    """to_stable 의 거꾸로. 새 전사에 없는 키는 버린다."""
    rev = {}
    for s in tr.get("segments", []):
        src = s.get("src")
        if not src:
            continue
        for i in range(len(s.get("words", []))):
            rev[f"{src['fkey']}|{src['sid']}|{int(src['w0']) + i}"] = f"{s['id']}:{i}"
    return {rev[k]: copy.deepcopy(v) for k, v in keys.items() if k in rev}


# ---- 보관: 파일 안 초 <-> 이어 붙인 초 -----------------------------------------

def _map(s, e, src0, src1, dst0):
    """[s,e] 와 [src0,src1) 의 겹침을 dst0 기준으로 옮긴다. 겹침이 없으면 None."""
    a, b = max(s, src0), min(e, src1)
    if b - a <= EPS:
        return None
    return (a - src0 + dst0, b - src0 + dst0)


def _convert(e: dict, windows: list) -> dict:
    """windows: [(src0, src1, dst0)] — 시간을 모두 src→dst 로. 여러 창에 걸친 항목은 여럿이 된다."""
    r = {k: [] for k in ("restored", "range_ops", "overlays")}
    for s, t in e.get("restored") or []:
        for w in windows:
            x = _map(float(s), float(t), *w)
            if x:
                r["restored"].append([x[0], x[1]])
    for o in e.get("range_ops") or []:
        for w in windows:
            x = _map(float(o["start"]), float(o["end"]), *w)
            if x:
                r["range_ops"].append({**o, "start": x[0], "end": x[1]})
    for o in e.get("overlays") or []:
        for w in windows:
            x = _map(float(o["start"]), float(o["end"]), *w)
            if x:
                r["overlays"].append({**o, "start": x[0], "end": x[1]})
    lt = {}
    for k, (s, t) in (e.get("line_times") or {}).items():
        for w in windows:
            if w[0] - EPS <= float(s) < w[1] - EPS:  # 줄은 시작이 든 창에 속한다
                x = _map(float(s), float(t), *w) or (float(s) - w[0] + w[2], float(s) - w[0] + w[2])
                lt[k] = [x[0], x[1]]
                break
    r["line_times"] = lt
    return r


def extract_clip(e: dict, stable: dict, clip_spans: list, fkey: str) -> dict:
    """보관용: 그 영상 조각들 안의 편집을 파일 안 초로 뽑는다.

    stable: {word_edits, line_times, word_styles} — 키는 to_stable 을 거친 것. fkey 가 그 파일의 키.
    clip_spans: [(이어 붙인 시작, 끝, 파일 in)].
    """
    r = _convert(e, [(cs, ce, fin) for cs, ce, fin in clip_spans])
    # 자막 줄 시간은 stable 키로 바꾼 쪽의 값을 쓴다 (e 의 "seg:i" 키는 전사 없이 못 바꿈)
    lt = _convert({"line_times": stable.get("line_times") or {}}, [(cs, ce, fin) for cs, ce, fin in clip_spans])["line_times"]
    pre = f"{fkey}|"
    r["line_times"] = {k: v for k, v in lt.items() if k.startswith(pre)}
    for name in ("word_edits", "word_styles"):
        r[name] = {k: copy.deepcopy(v) for k, v in (stable.get(name) or {}).items() if k.startswith(pre)}
    return r


def place_clip(saved: dict, clip_spans: list) -> dict:
    """extract_clip 의 거꾸로: 파일 안 초를 조각마다 이어 붙인 초로. 자막 키는 stable 키 그대로(API 가 from_stable)."""
    r = _convert(saved, [(fin, fin + (ce - cs), cs) for cs, ce, fin in clip_spans])
    for name in ("word_edits", "word_styles"):
        r[name] = copy.deepcopy(saved.get(name) or {})
    return r
