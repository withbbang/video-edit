"""BGM(음원) 넣기의 곡선·시간 계산 — 순수 파이썬(numpy 없음).

시간 말: "원본 초"(컷 전), "결과 초"(컷 뒤), "곡 초"(BGM 파일 안). 음량 점은 [곡 초, dB] 목록.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

AUTO_BASE_DB = 0.0     # 말 없을 때 기본 음량
AUTO_DUCK_DB = -15.0   # 말하는 동안 낮춘 음량
AUTO_RAMP = 0.3        # 말 앞뒤로 내리고 올리는 시간(초)
AUTO_FADE = 0.5        # 시작 페이드 인·끝 페이드 아웃(초)
SPEECH_JOIN = 0.5      # 이보다 짧은 말 사이 틈은 한 덩어리로 봄(초)
DB_MIN = -40.0         # 음량 점 하한 (pipeline.BGM_FLOOR_DB 와 같음 — 이 아래는 무음)
DB_MAX = 6.0
MIN_LEN = 0.1          # BGM 구간 최소 길이(초)
VOL_DEFAULT = 100      # 음원 전체 음량(%) — 화면 슬라이더. 100 = 불러올 때 맞춘 음량 그대로
VOL_MAX = 200
MUTE_GAIN = -120.0     # 음량 0% 일 때 넘기는 gain(dB) — 사실상 무음


def eff_gain(b) -> float:
    """렌더·미리보기에 쓰는 gain(dB) = 불러올 때 맞춘 gain + 전체 음량(%)."""
    vol = b.get("vol", VOL_DEFAULT)
    if vol <= 0:
        return MUTE_GAIN
    return float(b["gain"]) + 20 * math.log10(vol / 100)
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"}   # 불러올 수 있는 음원
TARGET_LUFS = -24.0    # 불러올 때 맞추는 기본 음량(적분 라우드니스)
GAIN_MIN, GAIN_MAX = -30.0, 12.0


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


TIE_GAP = 1e-3         # 같은 시간의 점을 이만큼 뒤로 민다 (app/ui 의 점 추가·끌기와 같은 값)


def strict_points(points, lo, hi):
    """점 [[곡 초, dB], …] 을 [lo, hi] 안으로 넣고 시간순(같은 시간은 들어온 순서)으로, 시간이 겹치지 않게 한다.
    같은 시간이면 앞 점 + TIE_GAP 로 밀고, 밀어서 hi 를 넘으면 그 점은 버린다.
    (같은 시간 점이 있으면 미리보기(JS)와 렌더(np.interp)의 보간이 달라지기 때문)"""
    out = []
    for t, db in sorted(([min(max(float(t), lo), hi), float(db)] for t, db in points), key=lambda p: p[0]):
        if out and t <= out[-1][0]:
            t = out[-1][0] + TIE_GAP
            if t > hi:
                continue
        out.append([t, db])
    return out


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


def _hidden() -> dict:
    from . import runner
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if runner.IS_WIN else {}


def _loudness_gain(m4a: Path) -> float:
    """ebur128 요약의 적분 음량(I:)을 TARGET_LUFS 로 맞추는 이득(dB). 못 읽거나 무음이면 0."""
    from . import runner
    try:
        res = subprocess.run(["ffmpeg", "-nostats", "-i", str(m4a), "-af", "ebur128", "-f", "null", "-"],
                             capture_output=True, text=True, env=runner.build_env(), timeout=300, **_hidden())
    except (OSError, subprocess.SubprocessError):
        return 0.0
    found = re.findall(r"^\s*I:\s*(-?[0-9.]+)\s*LUFS", res.stderr or "", re.M)
    try:
        loud = float(found[-1])
    except (IndexError, ValueError):
        return 0.0
    if not math.isfinite(loud):
        return 0.0
    return min(max(TARGET_LUFS - loud, GAIN_MIN), GAIN_MAX)


def import_audio(src: Path, folder: Path) -> dict | None:
    """음원을 folder/<sha1 앞 16자>.m4a (AAC 48k 스테레오)로 바꿔 두고 {"file", "dur", "gain"} 을 돌려준다.
    소리가 없거나 영상이 들어 있거나(앨범 아트 제외) 못 읽으면 None. 이미 있으면 다시 만들지 않는다."""
    from . import runner
    src = Path(src)
    if src.suffix.lower() not in AUDIO_EXTS:
        return None
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:stream=codec_type:stream_disposition=attached_pic", "-of", "json",
                              str(src)], capture_output=True, text=True, env=runner.build_env(), timeout=30, **_hidden())
        data = json.loads(res.stdout or "{}")
        streams = data.get("streams") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not any(s.get("codec_type") == "audio" for s in streams) or \
            any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic") for s in streams):
        return None
    h = hashlib.sha1()
    try:
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    dst = folder / (h.hexdigest()[:16] + ".m4a")
    if not dst.is_file():
        tmp = dst.with_name(dst.name + ".part.m4a")
        try:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "2", "-ar", "48000",
                            "-c:a", "aac", "-b:a", "192k", str(tmp)], capture_output=True, env=runner.build_env(),
                           check=True, timeout=1800, **_hidden())
            os.replace(tmp, dst)
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            tmp.unlink(missing_ok=True)
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(dst)],
                             capture_output=True, text=True, env=runner.build_env(), timeout=30, **_hidden())
        dur = float(json.loads(res.stdout)["format"]["duration"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    if not math.isfinite(dur) or dur <= 0:
        return None
    return {"file": dst.name, "dur": dur, "gain": _loudness_gain(dst)}
