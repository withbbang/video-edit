"""BGM(음원) 넣기의 곡선·시간 계산 — 순수 파이썬(numpy 없음).

시간 말: "원본 초"(컷 전), "결과 초"(컷 뒤), "곡 초"(BGM 파일 안). 음량 점은 [곡 초, dB] 목록.
"""
from __future__ import annotations

AUTO_BASE_DB = 0.0     # 말 없을 때 기본 음량
AUTO_DUCK_DB = -15.0   # 말하는 동안 낮춘 음량
AUTO_RAMP = 0.3        # 말 앞뒤로 내리고 올리는 시간(초)
AUTO_FADE = 0.5        # 시작 페이드 인·끝 페이드 아웃(초)
SPEECH_JOIN = 0.5      # 이보다 짧은 말 사이 틈은 한 덩어리로 봄(초)
DB_MIN = -40.0         # 음량 점 하한 (pipeline.BGM_FLOOR_DB 와 같음 — 이 아래는 무음)
DB_MAX = 6.0
MIN_LEN = 0.1          # BGM 구간 최소 길이(초)


def keeps_from_cuts(cuts, duration, fps):
    """컷(원본 초) → 남길 구간. 겹친 컷은 합치고 프레임 격자에 맞추며 길이 0 구간은 뺀다."""
    merged = []
    for s, e in sorted((max(0.0, float(a)), min(float(duration), float(b))) for a, b in cuts):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    keeps, pos = [], 0.0
    for s, e in merged + [[float(duration), float(duration)]]:
        if s > pos:
            keeps.append([pos, s])
        pos = max(pos, e)
    out = []
    for s, e in keeps:
        s, e = round(s * fps) / fps, round(e * fps) / fps
        if e > s:
            out.append([s, e])
    return out


def out_total(keeps):
    return sum(e - s for s, e in keeps)


def to_out(t, keeps):
    """원본 초 → 결과 초 (player.js timeMap.toOut 과 같은 규칙).
    잘린 구간 안은 그 구간 끝의 결과 시간, 마지막 남길 구간 뒤는 전체 길이."""
    o = 0.0
    for s, e in keeps:
        if t < s:
            return o
        if t <= e:
            return o + (t - s)
        o += e - s
    return o


def speech_spans(words, keeps):
    """단어(원본 초)마다 남긴 부분을 결과 초로 옮겨 정렬·합친 말 구간. 틈 < SPEECH_JOIN 도 합침."""
    spans = []
    for w in words:
        ws, we = float(w["start"]), float(w["end"])
        o = 0.0
        for ks, ke in keeps:
            a, b = max(ws, ks), min(we, ke)
            if b > a:
                spans.append([o + (a - ks), o + (b - ks)])
            o += ke - ks
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s - merged[-1][1] < SPEECH_JOIN:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _trap(x, s, e):
    """말 구간 [s, e] 하나가 만드는 음량: 앞뒤 램프 사이 DUCK, 밖은 BASE."""
    if x <= s - AUTO_RAMP or x >= e + AUTO_RAMP:
        return AUTO_BASE_DB
    if s <= x <= e:
        return AUTO_DUCK_DB
    d = (s - x) if x < s else (x - e)
    return AUTO_DUCK_DB + (AUTO_BASE_DB - AUTO_DUCK_DB) * d / AUTO_RAMP


def auto_points(at, inn, out, speech):
    """BGM 결과 구간 [at, at+out-inn] 의 자동 음량 곡선 (곡 초 점 목록).
    말 구간은 내리고, 양끝은 페이드. 겹치는 곳은 각 곡선의 작은 값."""
    end = at + (out - inn)
    length = end - at
    if length <= 0:
        return []
    if length < 2 * AUTO_FADE:
        fade_pts = [(at, DB_MIN), (at + length / 2, AUTO_BASE_DB), (end, DB_MIN)]
    else:
        fade_pts = [(at, DB_MIN), (at + AUTO_FADE, AUTO_BASE_DB),
                    (end - AUTO_FADE, AUTO_BASE_DB), (end, DB_MIN)]

    def fade(x):
        for (x0, y0), (x1, y1) in zip(fade_pts, fade_pts[1:]):
            if x <= x1:
                return y1 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return fade_pts[-1][1]

    spans = [(s, e) for s, e in speech if e + AUTO_RAMP > at and s - AUTO_RAMP < end]
    xs = {at, end}
    xs.update(x for x, _ in fade_pts)
    for s, e in spans:
        xs.update(x for x in (s - AUTO_RAMP, s, e, e + AUTO_RAMP) if at <= x <= end)
    pts = []
    for x in sorted(xs):
        db = min([fade(x)] + [_trap(x, s, e) for s, e in spans])
        if pts and abs(x - pts[-1][0]) < 1e-9:
            pts[-1][1] = min(pts[-1][1], db)
        else:
            pts.append([x, db])
    return [[inn + (x - at), db] for x, db in pts]


def resolve_points(b, speech):
    """수동 점이 있으면 그것, None 이면 자동 곡선."""
    if b.get("points") is not None:
        return b["points"]
    return auto_points(b["at"], b["in"], b["out"], speech)


def db_at(points, t):
    """곡 초 t → dB. 점 사이는 직선, 첫 점 앞·마지막 점 뒤는 그 점 값, 점이 없으면 0.
    (pipeline.bgm_db_at 의 스칼라 판)"""
    if not points:
        return 0.0
    pts = sorted((float(a), float(b)) for a, b in points)
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if t <= x1:
            return y1 if x1 == x0 else y0 + (y1 - y0) * (t - x0) / (x1 - x0)
    return pts[-1][1]
