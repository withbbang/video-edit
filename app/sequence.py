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


# ---- 자리 맞춤 (넣기·옮기기 공통, app/ui/timeline.js 의 snapAt 과 같은 규칙) ----

def piece_bounds(pieces: list) -> list:
    """조각 사이 경계 시각 (0·전체 끝 제외, 오름차순)."""
    r, t = [], 0.0
    for p in pieces[:-1]:
        t += float(p["out"]) - float(p["in"])
        r.append(t)
    return r


def block_span(pieces: list, clip: str):
    """그 영상 첫 조각 시작 ~ 마지막 조각 끝 (이어 붙인 초). 없으면 None."""
    spans = clip_spans(pieces, clip)
    return (spans[0][0], spans[-1][1]) if spans else None


def snap_at(t: float, cuts: list, bounds: list, fps: float, total: float) -> float:
    """t 를 프레임 격자 → [0, total] 에 맞추고, 잘린 구간 (a, b) 안이면 그 안의 영상 경계 중 가장 가까운 것
    (같으면 앞쪽), 경계가 없으면 a 를 격자에 맞춘 값."""
    t = float(min(max(round(float(t) * fps) / fps, 0.0), total))   # fps 가 Fraction 이어도 float
    for a, b in cuts:
        if a + EPS < t < b - EPS:
            inside = [x for x in sorted(bounds) if a + EPS < x < b - EPS]
            if inside:
                return min(inside, key=lambda x: abs(x - t))
            return float(min(max(round(a * fps) / fps, 0.0), total))
    return t


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
        elif t > at + EPS:  # 줄은 조각 경계를 넘지 않는다: 가운데 넣으면 키가 가리키는 앞 절반만 남긴다
            r["line_times"][k] = [float(s), float(at)]
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


# ---- 옮기기 -----------------------------------------------------------------

def move_block(pieces: list, clip: str, t: float):
    """clip 덩어리(첫~끝 조각 사이 모두)를 t(옮기기 전 시각, 맞춘 값, 덩어리 밖)로 옮긴다. → (조각 목록, t2).
    합치기는 하지 않는다 (API 가 한다)."""
    s, end = block_span(pieces, clip)
    d = end - s
    block, rest, pos = [], [], 0.0
    for p in pieces:
        a, b = pos, pos + float(p["out"]) - float(p["in"])
        pos = b
        (block if a >= s - EPS and b <= end + EPS else rest).append(p)
    t2 = t if t <= s + EPS else t - d
    out, at = rest, t2
    for p in block:  # 경계에 넣으면 갈지 않으므로 앞 조각 뒤에 이어 넣는다
        out = split_piece(out, at, p)
        at += float(p["out"]) - float(p["in"])
    return out, t2


def shift_for_move(e: dict, s: float, end: float, t2: float) -> dict:
    """덩어리 [s, end] 를 빼서 t2(옮긴 뒤 시각)에 넣을 때의 편집. 덩어리 안 편집은 따라가고 바깥은 밀린다."""
    d = end - s
    r = shift_for_insert(shift_for_remove(e, [(s, end)]), t2, d)
    inner = _convert(e, [(s, end, t2)])
    r["restored"] += inner["restored"]
    r["range_ops"] += inner["range_ops"]
    r["line_times"].update(inner["line_times"])
    whole = [o for o in e.get("overlays") or []
             if float(o["start"]) >= s - EPS and float(o["end"]) <= end + EPS]
    r["overlays"] += _convert({"overlays": whole}, [(s, end, t2)])["overlays"]
    return r


# ---- 자막 키 ----------------------------------------------------------------

def _seg_clips(tr: dict) -> dict:
    """세그먼트 id → (clip, 파일 세그먼트 id, 첫 어절 번호). 이어 붙인 전사는 tr["pieces"] 의 seg_from/seg_to 로
    세그먼트가 든 조각의 clip 을 찾는다 (조각 범위 밖 세그먼트는 뺀다). pieces 가 없는 한 파일 전사는 모두 "main"."""
    segs = tr.get("segments", [])
    pcs = tr.get("pieces")
    out = {}
    if pcs is None:
        for s in segs:
            src = s.get("src")
            out[str(s["id"])] = ("main", src["sid"], int(src["w0"])) if src else ("main", s["id"], 0)
        return out
    for p in pcs:
        for s in segs[int(p["seg_from"]):int(p["seg_to"])]:
            src = s.get("src") or {"sid": s["id"], "w0": 0}
            out[str(s["id"])] = (p["clip"], src["sid"], int(src["w0"]))
    return out


def to_stable(keys: dict, tr: dict) -> dict:
    """"seg:i" → "clip|sid|w0+i" (전사가 바뀌어도 변하지 않는 키). clip 은 그 세그먼트가 든 조각의 영상 id —
    같은 파일을 두 번 넣어도 키가 겹치지 않는다. 한 파일 전사(첫 끼워 넣기 전)는 clip "main", sid = 세그먼트 id, w0 = 0.
    전사에 없는 키는 버린다."""
    segs = _seg_clips(tr)
    out = {}
    for k, v in keys.items():
        seg_id, _, i = str(k).partition(":")
        s = segs.get(seg_id)
        if s is None or not i.isdigit():
            continue
        out[f"{s[0]}|{s[1]}|{s[2] + int(i)}"] = copy.deepcopy(v)
    return out


def from_stable(keys: dict, tr: dict) -> dict:
    """to_stable 의 거꾸로. 새 전사에 없는 키는 버린다."""
    rev = {}
    words = {str(s["id"]): len(s.get("words", [])) for s in tr.get("segments", [])}
    for seg_id, (clip, sid, w0) in _seg_clips(tr).items():
        for i in range(words.get(seg_id, 0)):
            rev[f"{clip}|{sid}|{w0 + i}"] = f"{seg_id}:{i}"
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


def extract_clip(e: dict, stable: dict, clip_spans: list, clip: str) -> dict:
    """보관용: 그 영상 조각들 안의 편집을 파일 안 초로 뽑는다.

    stable: {word_edits, line_times, word_styles} — 키는 to_stable 을 거친 것. clip 이 그 영상 id 이고,
    자막 키는 "clip|" 을 떼어 "sid|w" (그 파일 단독 전사 기준)로 저장한다.
    clip_spans: [(이어 붙인 시작, 끝, 파일 in)].
    같은 id 로 파일 초에서 이어지는 이미지 조각은 하나로 합친다.
    """
    r = _convert(e, [(cs, ce, fin) for cs, ce, fin in clip_spans])
    # 자막 줄 시간은 stable 키로 바꾼 쪽의 값을 쓴다 (e 의 "seg:i" 키는 전사 없이 못 바꿈)
    lt = _convert({"line_times": stable.get("line_times") or {}}, [(cs, ce, fin) for cs, ce, fin in clip_spans])["line_times"]
    # 이어 붙인 쪽에서 갈렸던 같은 id 이미지 조각 중 파일 초에서 이어지는 것은 하나로 합친다
    merged = []
    for o in sorted(r["overlays"], key=lambda o: o["start"]):
        for m in merged:
            if m.get("id") == o.get("id") and abs(m["end"] - o["start"]) <= EPS:
                m["end"] = o["end"]
                break
        else:
            merged.append(o)
    r["overlays"] = merged
    pre = f"{clip}|"
    r["line_times"] = {k[len(pre):]: v for k, v in lt.items() if k.startswith(pre)}
    for name in ("word_edits", "word_styles"):
        r[name] = {k[len(pre):]: copy.deepcopy(v) for k, v in (stable.get(name) or {}).items() if k.startswith(pre)}
    return r


def place_clip(saved: dict, clip_spans: list, clip: str) -> dict:
    """extract_clip 의 거꾸로: 파일 안 초를 조각마다 이어 붙인 초로. 자막 키("sid|w")엔 새 영상 id 를 붙여
    stable 키("clip|sid|w")로 돌려준다 (API 가 from_stable).

    여러 조각에 걸쳐 갈린 이미지는 첫 부분만 원래 id 를 갖고 나머지는 새 id(token_hex(4)).
    """
    r = _convert(saved, [(fin, fin + (ce - cs), cs) for cs, ce, fin in clip_spans])
    seen = set()
    for o in r["overlays"]:
        if o.get("id") in seen:
            o["id"] = secrets.token_hex(4)
        seen.add(o.get("id"))
    r["line_times"] = {f"{clip}|{k}": v for k, v in r["line_times"].items()}
    for name in ("word_edits", "word_styles"):
        r[name] = {f"{clip}|{k}": copy.deepcopy(v) for k, v in (saved.get(name) or {}).items()}
    return r
