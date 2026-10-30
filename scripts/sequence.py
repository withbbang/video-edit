"""조각 목록(영상 A 사이에 영상 B 끼우기)과 이어 붙인 전사. 순수 함수만 둔다 (pipeline 을 import 하지 않는다 —
pipeline 이 이 모듈을 import 한다)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass
class Piece:
    id: str
    clip: str
    src: Path
    fkey: str      # 원본 파일 식별 (경로·크기·mtime 의 sha1 앞 16자)
    t_in: float
    t_out: float


@dataclass
class Span:
    piece: Piece
    f_in: int      # 파일 안 프레임 [f_in, f_out)
    f_out: int
    start_f: int   # 이어 붙인 시간에서의 시작 프레임


def _source_sig(src: Path) -> dict:
    # pipeline.source_sig 와 같은 값 (경로·크기·mtime)
    st = src.stat()
    return {"path": str(src), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def file_key(src: Path) -> str:
    return hashlib.sha1(json.dumps(_source_sig(src), sort_keys=True).encode()).hexdigest()[:16]


def load_sequence(path: Path) -> list[Piece]:
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for it in items:
        src = Path(it["src"])
        out.append(Piece(id=it["id"], clip=it["clip"], src=src, fkey=file_key(src),
                         t_in=float(it["in"]), t_out=float(it["out"])))
    return out


def spans(pieces: list[Piece], fps: Fraction) -> list[Span]:
    """프레임 격자에 맞춘 구간 목록. 길이 0 조각은 뺀다."""
    out, at = [], 0
    for p in pieces:
        f_in, f_out = round(p.t_in * fps), round(p.t_out * fps)
        if f_out <= f_in:
            continue
        out.append(Span(p, f_in, f_out, at))
        at += f_out - f_in
    return out


def combine_transcripts(spans_: list[Span], file_trs: dict[str, dict], fps: Fraction) -> dict:
    """조각마다 [in,out) 안에 중점이 있는 단어만 모아 이어 붙인 시간으로 옮긴 전사.
    세그먼트는 조각 경계에서 갈리고 id 는 0부터 다시 매긴다."""
    segs, pcs = [], []
    total = 0.0
    for sp in spans_:
        p = sp.piece
        lo, hi = sp.f_in / fps, sp.f_out / fps
        start = float(sp.start_f / fps)
        shift = start - float(lo)
        seg_from = len(segs)
        for seg in file_trs[p.fkey]["segments"]:
            words, w0 = [], None
            for i, w in enumerate(seg["words"]):
                if not (lo <= (w["start"] + w["end"]) / 2 < hi):
                    continue
                if w0 is None:
                    w0 = i
                nw = dict(w)
                nw["start"], nw["end"] = w["start"] + shift, w["end"] + shift
                if "whisper" in w:
                    nw["whisper"] = [t + shift for t in w["whisper"]]
                words.append(nw)
            if not words:
                continue
            segs.append({"id": len(segs), "start": words[0]["start"], "end": words[-1]["end"],
                         "text": " ".join(w["word"].strip() for w in words), "words": words,
                         "src": {"fkey": p.fkey, "sid": seg["id"], "w0": w0}})
        end = start + float((sp.f_out - sp.f_in) / fps)
        pcs.append({"id": p.id, "clip": p.clip, "start": start, "end": end,
                    "seg_from": seg_from, "seg_to": len(segs)})
        total = end
    first = spans_[0].piece.fkey if spans_ else None
    return {"language": file_trs[first].get("language") if first else None,
            "duration": total, "segments": segs, "pieces": pcs}


def slice_piece(tr: dict, p: dict) -> dict:
    """tr["pieces"] 한 항목 범위의 세그먼트만 남기고 시간을 조각 기준(0부터)으로 당긴 사본.
    세그먼트 id 는 그대로 둔다 (컷의 words_idx 가 이어 붙인 전사를 가리키게)."""
    t0 = p["start"]
    segs = []
    for seg in tr["segments"][p["seg_from"]:p["seg_to"]]:
        s = copy.deepcopy(seg)
        s["start"] -= t0
        s["end"] -= t0
        for w in s["words"]:
            w["start"] -= t0
            w["end"] -= t0
            if "whisper" in w:
                w["whisper"] = [t - t0 for t in w["whisper"]]
        segs.append(s)
    out = {k: v for k, v in tr.items() if k not in ("segments", "pieces")}
    out["segments"] = segs
    out["duration"] = p["end"] - t0
    return out


def concat_wav(parts: list[tuple[Path | None, int, int]], out: Path, rate: int, channels: int) -> int:
    """(wav, 시작 샘플, 끝 샘플) 조각들을 이어 붙여 out 에 쓴다 (16비트 PCM). 반환 = 전체 샘플 수.
    wav 가 None 이거나 원본이 모자라면 그 부분은 0 (무음). 채널 수가 다르면 모노→복제, 스테레오→평균으로 맞춘다.
    .part 에 쓴 뒤 os.replace 한다."""
    import wave
    import numpy as np
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    total = 0
    chunk = rate * 30
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        for path, a, b in parts:
            n = max(0, b - a)
            if path is None:
                done = 0
                while done < n:
                    m = min(chunk, n - done)
                    w.writeframes(bytes(m * channels * 2))
                    done += m
                total += n
                continue
            with wave.open(str(path), "rb") as r:
                ch, avail = r.getnchannels(), r.getnframes()
                r.setpos(min(a, avail))
                done = 0
                while done < n:
                    m = min(chunk, n - done)
                    raw = r.readframes(min(m, max(0, avail - min(a, avail) - done)))
                    x = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch) if raw else np.zeros((0, ch), np.int16)
                    if ch != channels:
                        if ch == 1:
                            x = np.repeat(x, channels, axis=1)
                        else:
                            x = x.astype(np.int32).mean(axis=1, keepdims=True).round().astype(np.int16)
                            x = np.repeat(x, channels, axis=1)
                    if len(x) < m:
                        x = np.concatenate([x, np.zeros((m - len(x), channels), np.int16)])
                    w.writeframes(x.astype(np.int16).tobytes())
                    done += m
            total += n
    os.replace(tmp, out)
    return total
