#!/usr/bin/env python3
"""
youtube-edit 파이프라인

  입력 영상 → (1) 노이즈 제거 + 라우드니스 정규화
           → (2) 한국어 전사 (faster-whisper, 단어 타임스탬프)
           → (3) 필러/습관어 + 말 사이 무음 감지 → 컷 리스트
           → (4) 컷 적용 (프레임 격자에 맞춰 A/V 싱크 유지)
           → (5) 컷 반영된 타임코드로 SRT 생성

사용 예:
  python scripts/pipeline.py input/my.mp4
  python scripts/pipeline.py input/my.mp4 --review            # 컷 리스트까지만 만들고 멈춤
  python scripts/pipeline.py input/my.mp4 --apply-cuts output/my.cuts.json   # (수정한) 컷 리스트로 렌더
  기본: 아/어/음 + 습관어 + 무음 컷, 맥 하드웨어 인코딩. 각각 on/off 로 끄고 켠다.
  python scripts/pipeline.py input/my.mp4 --filler off        # 아/어/음 남김
  python scripts/pipeline.py input/my.mp4 --habit off         # 습관어 남김 (aggressive: 전부 컷)
  python scripts/pipeline.py input/my.mp4 --silence off       # 말 사이 무음 남김
  python scripts/pipeline.py input/my.mp4 --hw off            # 소프트웨어 인코딩(libx264, 느리지만 고화질)

산출물 (output/<이름>.*):
  <이름>_clean.mp4     최종 영상
  <이름>_clean.srt     자막 (최종 영상 기준 타임코드, 영상과 같은 이름). mp4 안에도 자막 트랙으로 들어감
  <이름>.cuts.json     잘라낸 구간 목록 (수정 후 --apply-cuts 로 재적용 가능)
  <이름>.review.md     사람이 보기 좋은 컷 검토표
  <이름>.transcript.json  단어 타임스탬프 포함 전사 원본
  <이름>.report.md     요약 리포트
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DEFAULT_FILLERS = ROOT / "fillers.json"

PUNCT_RE = re.compile(r"[\s\.\,\!\?\~\…\·\'\"\-\…\(\)\[\]「」『』、。:;]+")


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list, cwd: Path | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        shown = " ".join(str(c) for c in cmd)
        log("$ " + (shown if len(shown) < 400 else shown[:400] + " …"))
    return subprocess.run([str(c) for c in cmd], cwd=cwd, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def need(binary: str) -> None:
    if shutil.which(binary) is None:
        sys.exit(f"오류: {binary} 를 찾을 수 없습니다. macOS: brew install ffmpeg")


def ffprobe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", path], quiet=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if a is None:
        sys.exit("오류: 오디오 트랙이 없는 파일입니다.")
    fps = Fraction(v["avg_frame_rate"]) if v and v.get("avg_frame_rate") not in (None, "0/0") else Fraction(30)
    if v and v.get("r_frame_rate") and fps == 0:
        fps = Fraction(v["r_frame_rate"])
    duration = float(info["format"].get("duration") or a.get("duration") or 0)
    return {
        "has_video": v is not None,
        "fps": fps,
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "duration": duration,
        "sample_rate": int(a.get("sample_rate", 48000)),
        "channels": int(a.get("channels", 2)),
    }


def fmt_srt_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t); ms = int(round((t - s) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_clock(t: float) -> str:
    m = int(t // 60); s = t - m * 60
    return f"{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------------------
# 1) 오디오 정리: 노이즈 제거 + 라우드니스
# ---------------------------------------------------------------------------
def audio_filter_chain(denoise: str, loudnorm: bool, model_rel: str) -> str:
    chain = ["highpass=f=70"]
    if denoise == "rnnoise":
        chain.append(f"arnndn=m={model_rel}:mix=0.85")
    elif denoise == "afftdn":
        chain.append("afftdn=nf=-25:nr=12:tn=1")
    if loudnorm:
        chain.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    return ",".join(chain)


def prepare_audio(src: Path, work: Path, denoise: str, loudnorm: bool, force: bool) -> tuple[Path, Path]:
    clean = work / "clean.wav"   # 48k, 원본 채널 수 — 최종 영상에 들어갈 오디오
    asr = work / "asr.wav"       # 16k mono — 전사/무음 분석용
    if clean.exists() and asr.exists() and not force:
        log("오디오 정리 결과 재사용 (work/ 에 이미 있음)")
        return clean, asr

    model_rel = os.path.relpath(MODELS_DIR / "sh.rnnn", ROOT)
    if denoise == "rnnoise" and not (MODELS_DIR / "sh.rnnn").exists():
        log("경고: models/sh.rnnn 이 없어 afftdn 으로 대체합니다")
        denoise = "afftdn"
    af = audio_filter_chain(denoise, loudnorm, model_rel)
    log(f"오디오 정리: {af}")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-vn", "-af", af, "-ar", "48000", "-c:a", "pcm_s16le", clean], cwd=ROOT)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", clean,
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", asr], cwd=ROOT)
    return clean, asr


# ---------------------------------------------------------------------------
# 2) 전사
# ---------------------------------------------------------------------------
def transcribe(asr_wav: Path, out_json: Path, model_name: str, device: str,
               compute_type: str, prompt: str, force: bool) -> dict:
    if out_json.exists() and not force:
        log(f"전사 결과 재사용: {out_json.name}")
        return json.loads(out_json.read_text(encoding="utf-8"))

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("오류: faster-whisper 가 설치되지 않았습니다. ./setup.sh 를 먼저 실행하세요.")

    threads = max(1, (os.cpu_count() or 4))
    log(f"Whisper 모델 로드: {model_name} (device={device}, compute={compute_type}, threads={threads})")
    log("  처음 한 번은 모델 다운로드(1~3GB)로 시간이 걸립니다")
    model = WhisperModel(model_name, device=device, compute_type=compute_type, cpu_threads=threads)

    log("전사 시작 (한국어, 단어 타임스탬프, 필러 보존 프롬프트)")
    segments_iter, info = model.transcribe(
        str(asr_wav),
        language="ko",
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        condition_on_previous_text=False,
        initial_prompt=prompt,
        hallucination_silence_threshold=2.0,
    )
    segments = []
    t0 = time.time()
    for i, seg in enumerate(segments_iter):
        words = [{"word": w.word, "start": float(w.start), "end": float(w.end),
                  "prob": float(w.probability)} for w in (seg.words or [])]
        segments.append({"id": i, "start": float(seg.start), "end": float(seg.end),
                         "text": seg.text.strip(), "words": words})
        if i % 20 == 0:
            log(f"  … {fmt_clock(seg.end)} 까지 전사 ({time.time()-t0:.0f}s)")
    result = {"language": info.language, "duration": float(info.duration), "model": model_name,
              "segments": segments}
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"전사 완료: {len(segments)} 세그먼트, {sum(len(s['words']) for s in segments)} 단어")
    return result


# ---------------------------------------------------------------------------
# 3) 필러 감지
# ---------------------------------------------------------------------------
def normalize(word: str) -> str:
    w = PUNCT_RE.sub("", word.strip())
    # '어어어', '음음' 같은 같은 글자 반복 → 한 글자로
    if len(w) >= 2 and len(set(w)) == 1:
        w = w[0]
    return w


@dataclass
class Cut:
    start: float
    end: float
    word: str
    reason: str
    context: str
    seg: int
    start_frame: int = 0
    end_frame: int = 0
    words_idx: list = None  # [[seg, word_index], ...] — SRT에서 해당 단어를 빼기 위해


def load_fillers(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["_always"] = {normalize(w) for w in cfg.get("always", [])}
    cfg["_habit"] = {normalize(w) for w in cfg.get("habit", [])}
    cfg["_pause_after"] = {normalize(w) for w in cfg.get("habit_pause_after", [])}
    cfg["_never"] = {normalize(w) for w in cfg.get("never_cut", [])}
    return cfg


def detect_fillers(tr: dict, cfg: dict, habit_mode: str, filler: bool = True) -> tuple[list[Cut], list[dict]]:
    min_gap = float(cfg.get("min_gap_habit_sec", 0.22))
    max_dur = float(cfg.get("max_filler_duration_sec", 1.3))
    cuts: list[Cut] = []
    skipped: list[dict] = []

    for seg in tr["segments"]:
        words = seg["words"]
        for i, w in enumerate(words):
            nw = normalize(w["word"])
            if not nw or nw in cfg["_never"]:
                continue
            is_always = nw in cfg["_always"]
            is_habit = nw in cfg["_habit"]
            is_pause_after = nw in cfg["_pause_after"]
            if not (is_always or is_habit or is_pause_after):
                continue

            prev_end = words[i - 1]["end"] if i > 0 else None
            next_start = words[i + 1]["start"] if i + 1 < len(words) else None
            gap_before = (w["start"] - prev_end) if prev_end is not None else 9.0
            gap_after = (next_start - w["end"]) if next_start is not None else 9.0
            prev_norm = normalize(words[i - 1]["word"]) if i > 0 else ""
            next_norm = normalize(words[i + 1]["word"]) if i + 1 < len(words) else ""
            neighbor_filler = (next_norm in cfg["_always"] or next_norm in cfg["_habit"]
                               or prev_norm == nw)

            reason = None
            if is_always:
                if not filler:
                    continue
                reason = "filler"
            elif habit_mode == "off":
                continue
            elif habit_mode == "aggressive":
                reason = "habit"
            else:  # conservative: 순수 습관어는 전부, 뜻이 있을 수 있는 단어(그/저/좀 …)는 뒤 멈춤일 때만
                if is_pause_after:
                    if gap_after >= min_gap or neighbor_filler:
                        reason = "habit(뒤 멈춤)"
                elif is_habit:
                    reason = "habit"
            if reason is None:
                continue

            ctx_prev = " ".join(x["word"].strip() for x in words[max(0, i - 3):i])
            ctx_next = " ".join(x["word"].strip() for x in words[i + 1:i + 4])
            context = f"{ctx_prev} [{w['word'].strip()}] {ctx_next}".strip()
            dur = w["end"] - w["start"]
            if dur > max_dur or dur <= 0:
                skipped.append({"start": w["start"], "end": w["end"], "word": w["word"].strip(),
                                "why": f"단어 길이 {dur:.2f}s 가 비정상 (타임스탬프 불안정)", "context": context})
                continue
            cuts.append(Cut(w["start"], w["end"], w["word"].strip(), reason, context, seg["id"],
                            words_idx=[[seg["id"], i]]))
    return cuts, skipped


# ---------------------------------------------------------------------------
# 무음 지점으로 경계 보정 + 프레임 격자 정렬 + 병합
# ---------------------------------------------------------------------------
def load_rms(asr_wav: Path, hop_sec: float = 0.01) -> tuple[list[float], float]:
    import numpy as np
    with wave.open(str(asr_wav), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(sr * hop_sec)
    frames = len(pcm) // hop
    x = pcm[:frames * hop].reshape(frames, hop)
    rms = np.sqrt((x ** 2).mean(axis=1))
    return rms.tolist(), hop_sec


def detect_silences(tr: dict, rms: list[float], hop: float, duration: float,
                    cfg: dict) -> tuple[list[Cut], list[dict]]:
    """말 사이 빈 구간 감지. 앞뒤로 silence_pad_sec 만 남기고 나머지를 컷한다."""
    import numpy as np
    thr = 10 ** (float(cfg.get("silence_threshold_db", -40)) / 20)
    min_len = float(cfg.get("silence_min_sec", 0.4))
    pad = float(cfg.get("silence_pad_sec", 0.12))
    r = np.asarray(rms)
    k = max(1, int(round(0.05 / hop)))  # 50ms 이동평균 — 짧은 잡음 스파이크로 무음이 쪼개지는 것 방지
    quiet = np.convolve(r, np.ones(k) / k, mode="same") < thr
    edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.astype(np.int8), [0]))))
    words = [w for seg in tr["segments"] for w in seg["words"]]
    word_starts = [w["start"] for w in words]

    cuts: list[Cut] = []
    skipped: list[dict] = []
    for a, b in zip(edges[0::2], edges[1::2]):
        s, e = a * hop, min(duration, b * hop)
        if e - s < min_len:
            continue
        i = bisect.bisect_left(word_starts, s)
        ctx_prev = " ".join(x["word"].strip() for x in words[max(0, i - 3):i])
        ctx_next = " ".join(x["word"].strip() for x in words[i:i + 3])
        context = f"{ctx_prev} [{e-s:.1f}s 무음] {ctx_next}".strip()
        # 무음 안에 단어가 통째로 들어 있으면 작은 목소리를 무음으로 오인했을 수 있다
        if i < len(words) and words[i]["end"] <= e:
            skipped.append({"start": s, "end": e, "word": "(무음)",
                            "why": f"무음 안에 단어 '{words[i]['word'].strip()}' 가 있음 (작은 목소리일 수 있음)",
                            "context": context})
            continue
        cs = s if a == 0 else s + pad            # 영상 맨 앞/맨 뒤 무음은 여유 없이 전부 컷
        ce = e if b >= len(r) else e - pad
        if ce - cs < 0.05:
            continue
        cuts.append(Cut(cs, ce, "(무음)", "silence", context, -1, words_idx=[]))
    return cuts, skipped


def snap_to_quiet(t: float, rms: list[float], hop: float, win: float) -> float:
    i0 = max(0, int((t - win) / hop))
    i1 = min(len(rms) - 1, int((t + win) / hop))
    it = min(len(rms) - 1, max(0, int(t / hop)))
    if i1 <= i0:
        return t
    j = min(range(i0, i1 + 1), key=lambda k: rms[k])
    # 원래 지점보다 확실히 조용한 곳이 있을 때만 옮긴다 (균일한 소리에서 불필요하게 밀리는 것 방지)
    if rms[j] < 0.6 * rms[it]:
        return j * hop + hop / 2
    return t


def refine_cuts(cuts: list[Cut], rms: list[float], hop: float, fps: Fraction,
                duration: float, cfg: dict) -> list[Cut]:
    win = float(cfg.get("snap_window_sec", 0.12))
    merge_gap = int(cfg.get("merge_gap_frames", 2))
    min_keep = int(cfg.get("min_keep_frames", 3))
    f = float(fps)
    total_frames = int(math.floor(duration * f))

    refined: list[Cut] = []
    for c in cuts:
        if c.reason == "silence":  # 이미 무음 안쪽 경계 — 옮기면 남겨 둔 여유가 줄어든다
            s, e = c.start, c.end
        else:
            s = snap_to_quiet(c.start, rms, hop, win)
            e = snap_to_quiet(c.end, rms, hop, win)
        if e - s < 0.04:  # 보정이 구간을 뭉갰으면 원래 값 사용
            s, e = c.start, c.end
        sf = max(0, int(round(s * f)))
        ef = min(total_frames, int(round(e * f)))
        if ef <= sf:
            ef = min(total_frames, sf + 1)
        if ef <= sf:
            continue
        refined.append(Cut(sf / f, ef / f, c.word, c.reason, c.context, c.seg, sf, ef,
                           list(c.words_idx or [])))

    refined.sort(key=lambda c: c.start_frame)
    merged: list[Cut] = []
    for c in refined:
        if merged and c.start_frame <= merged[-1].end_frame + max(merge_gap, min_keep):
            m = merged[-1]
            m.end_frame = max(m.end_frame, c.end_frame)
            m.end = m.end_frame / f
            m.word = f"{m.word} + {c.word}"
            if c.reason not in m.reason.split(" + "):
                m.reason = f"{m.reason} + {c.reason}"
            m.context = m.context if c.context == m.context else f"{m.context} / {c.context}"
            m.words_idx = (m.words_idx or []) + (c.words_idx or [])
        else:
            merged.append(c)
    return merged


def keep_segments(cuts: list[Cut], total_frames: int) -> list[tuple[int, int]]:
    keeps = []
    pos = 0
    for c in cuts:
        if c.start_frame > pos:
            keeps.append((pos, c.start_frame))
        pos = max(pos, c.end_frame)
    if pos < total_frames:
        keeps.append((pos, total_frames))
    return keeps


# ---------------------------------------------------------------------------
# 4) 컷 적용
# ---------------------------------------------------------------------------
def cut_audio_numpy(clean_wav: Path, out_wav: Path, keeps: list[tuple[int, int]], fps: Fraction,
                    fade_ms: float = 3.0) -> int:
    import numpy as np
    with wave.open(str(clean_wav), "rb") as wf:
        sr, ch, n = wf.getframerate(), wf.getnchannels(), wf.getnframes()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).reshape(-1, ch).astype(np.float32)
    fade = int(sr * fade_ms / 1000)
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None] if fade > 0 else None
    pieces = []
    for sf, ef in keeps:
        a = int(round(sf / fps * sr))
        b = int(round(ef / fps * sr))
        a, b = max(0, min(a, n)), max(0, min(b, n))
        if b <= a:
            continue
        piece = pcm[a:b].copy()
        if ramp is not None and len(piece) > 2 * fade:
            piece[:fade] *= ramp
            piece[-fade:] *= ramp[::-1]
        pieces.append(piece)
    out = np.concatenate(pieces) if pieces else pcm[:0]
    out = np.clip(out, -32768, 32767).astype(np.int16)
    with wave.open(str(out_wav), "wb") as wo:
        wo.setnchannels(ch); wo.setsampwidth(2); wo.setframerate(sr)
        wo.writeframes(out.tobytes())
    return len(out)


def render_video(src: Path, cut_wav: Path, out_mp4: Path, keeps: list[tuple[int, int]],
                 fps: Fraction, work: Path, vcodec: str, crf: int, vbitrate: str, preset: str) -> None:
    f = float(fps)
    terms = [f"between(t,{(sf-0.5)/f:.6f},{(ef-0.5)/f:.6f})" for sf, ef in keeps]
    script = work / "video_filter.txt"
    script.write_text(
        f"[0:v]setpts=PTS-STARTPTS,fps={fps.numerator}/{fps.denominator},"
        f"select='{'+'.join(terms)}',setpts=N/FRAME_RATE/TB[v]",
        encoding="utf-8")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
           "-i", src, "-i", cut_wav,
           "-/filter_complex", script,  # ffmpeg 7+ 문법 (-filter_complex_script 는 9.0 에서 제거됨)
           "-map", "[v]", "-map", "1:a",
           "-c:v", vcodec, "-pix_fmt", "yuv420p"]
    if vcodec == "libx264":
        cmd += ["-preset", preset, "-crf", str(crf)]
    else:  # h264_videotoolbox 등
        cmd += ["-b:v", vbitrate]
    # -shortest 는 마지막 프레임을 떨어뜨리므로 쓰지 않는다 (오디오/영상 길이는 이미 프레임 격자로 맞춤)
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_mp4]
    log("렌더링 시작 (영상 길이에 따라 몇 분 걸립니다)")
    subprocess.run([str(c) for c in cmd], check=True)


# ---------------------------------------------------------------------------
# 5) SRT
# ---------------------------------------------------------------------------
class TimeRemap:
    """원본 시간 → 컷 적용 후 시간"""
    def __init__(self, cuts: list[Cut]):
        self.starts = [c.start for c in cuts]
        self.ends = [c.end for c in cuts]
        self.cut_words = {(s, i) for c in cuts for s, i in (c.words_idx or [])}
        # 무음만 잘라낸 구간은 단어를 지우지 않는다 (Whisper 단어 시간이 무음 쪽으로 번지는 일이 잦음)
        self.word_cut = [c.reason != "silence" for c in cuts]
        self.removed_before = []
        acc = 0.0
        for c in cuts:
            self.removed_before.append(acc)
            acc += c.end - c.start
        self.total_removed = acc

    def __call__(self, t: float) -> float:
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return t
        if t < self.ends[i]:          # 잘려나간 구간 안 → 그 구간의 시작점으로
            return self.starts[i] - self.removed_before[i]
        return t - self.removed_before[i] - (self.ends[i] - self.starts[i])

    def inside_cut(self, s: float, e: float) -> bool:
        mid = (s + e) / 2
        i = bisect.bisect_right(self.starts, mid) - 1
        return i >= 0 and mid < self.ends[i] and self.word_cut[i]


def build_srt(tr: dict, remap: TimeRemap, always_fillers: set[str] = frozenset(),
              max_chars: int = 28, max_dur: float = 5.0, gap_break: float = 0.7) -> str:
    captions: list[tuple[float, float, str]] = []
    cur_words: list[dict] = []

    def flush():
        if not cur_words:
            return
        s = remap(cur_words[0]["start"])
        e = remap(cur_words[-1]["end"])
        text = " ".join(w["word"].strip() for w in cur_words).strip()
        if text:
            captions.append((s, max(e, s + 0.4), text))
        cur_words.clear()

    for seg in tr["segments"]:
        for wi, w in enumerate(seg["words"]):
            if (seg["id"], wi) in remap.cut_words or remap.inside_cut(w["start"], w["end"]):
                continue  # 잘려나간 필러
            if normalize(w["word"]) in always_fillers:
                continue  # 오디오엔 남았더라도(건너뛴 항목) 자막에는 '어/음'을 넣지 않는다
            if cur_words:
                gap = w["start"] - cur_words[-1]["end"]
                length = sum(len(x["word"].strip()) + 1 for x in cur_words) + len(w["word"].strip())
                span = w["end"] - cur_words[0]["start"]
                prev_text = cur_words[-1]["word"].strip()
                if (gap > gap_break or length > max_chars or span > max_dur
                        or prev_text.endswith((".", "?", "!"))):
                    flush()
            cur_words.append(w)
        flush()  # 세그먼트 경계에서는 항상 끊음

    # 자막끼리 겹치지 않게
    lines = []
    for i, (s, e, text) in enumerate(captions):
        if i + 1 < len(captions):
            e = min(e, captions[i + 1][0] - 0.05)
        if e <= s:
            e = s + 0.3
        lines.append(f"{i+1}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{text}\n")
    return "\n".join(lines) + ("\n" if lines else "")


def embed_subtitles(mp4: Path, srt: Path, work: Path) -> None:
    """mp4 에 끄고 켤 수 있는 자막 트랙(mov_text)을 넣는다. 재인코딩 없음, 번인 아님.
    QuickTime 은 보기 > 자막 으로 켠다. 유튜브는 이 트랙을 쓰지 않으므로 SRT 는 따로 올린다."""
    tmp = work / "with_subs.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", mp4, "-i", srt,
         "-map", "0:v", "-map", "0:a", "-map", "1:s", "-c", "copy", "-c:s", "mov_text",
         "-metadata:s:s:0", "language=kor", "-movflags", "+faststart", tmp], quiet=True)
    shutil.move(str(tmp), str(mp4))
    log("자막 트랙을 영상에 넣음 (번인 아님, 플레이어에서 켜고 끄기)")


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def write_review(cuts: list[Cut], skipped: list[dict], path: Path) -> None:
    out = ["# 컷 검토표", "",
           "잘못 잡힌 항목이 있으면 같은 이름의 .cuts.json 에서 해당 항목을 지운 뒤",
           "`--apply-cuts output/<이름>.cuts.json` 으로 다시 렌더하세요.", "",
           "| # | 시작 | 끝 | 길이 | 단어 | 이유 | 문맥 |", "|---|---|---|---|---|---|---|"]
    for i, c in enumerate(cuts, 1):
        out.append(f"| {i} | {fmt_clock(c.start)} | {fmt_clock(c.end)} | {c.end-c.start:.2f}s "
                   f"| {c.word} | {c.reason} | {c.context} |")
    if skipped:
        out += ["", "## 감지됐지만 건너뛴 항목", "", "| 시작 | 단어 | 이유 | 문맥 |", "|---|---|---|---|"]
        for s in skipped:
            out.append(f"| {fmt_clock(s['start'])} | {s['word']} | {s['why']} | {s['context']} |")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_report(path: Path, src: Path, info: dict, cuts: list[Cut], skipped: list[dict],
                 final_dur: float, args: argparse.Namespace, elapsed: float) -> None:
    from collections import Counter
    by_word = Counter(normalize(w) for c in cuts for w in c.word.split(" + ") if w != "(무음)")
    removed = sum(c.end - c.start for c in cuts)
    n_silence = sum(1 for c in cuts if "silence" in c.reason)
    out = [f"# {src.name} 처리 리포트", "",
           f"- 원본 길이: {fmt_clock(info['duration'])} → 결과: {fmt_clock(final_dur)} "
           f"(-{removed:.1f}s, {len(cuts)}곳 컷)",
           f"- 노이즈 제거: {args.denoise}, 라우드니스 정규화: {'켬' if not args.no_loudnorm else '끔'}",
           f"- 전사 모델: {args.model}, 아/어/음 컷: {args.filler}, 습관어 컷: {args.habit}, "
           f"무음 컷: {args.silence}, 하드웨어 인코딩: {args.hw}",
           f"- 무음이 포함된 컷: {n_silence}곳",
           f"- 건너뛴 감지: {len(skipped)}건 (review.md 참고)",
           f"- 소요 시간: {elapsed/60:.1f}분", "",
           "## 잘라낸 단어별 횟수", ""]
    for w, n in by_word.most_common():
        out.append(f"- {w}: {n}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="유튜브 영상 정리: 노이즈 제거 → 필러 컷 → SRT")
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "output")
    ap.add_argument("--fillers", type=Path, default=DEFAULT_FILLERS)
    ap.add_argument("--model", default="large-v3-turbo",
                    help="faster-whisper 모델 (large-v3-turbo | large-v3 | medium | small)")
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda")
    ap.add_argument("--compute-type", default="int8", help="int8 | int8_float16 | float16 | float32")
    ap.add_argument("--denoise", choices=["rnnoise", "afftdn", "none"], default="rnnoise")
    ap.add_argument("--no-loudnorm", action="store_true", help="유튜브 기준(-14 LUFS) 라우드니스 정규화 끄기")
    ap.add_argument("--filler", choices=["on", "off"], default="on", help="아/어/음 컷 (기본 on)")
    ap.add_argument("--habit", choices=["on", "off", "aggressive"], default="on",
                    help="습관어 컷 (기본 on: 그니까/약간/이제 등은 전부, 그/저/좀 등은 뒤 멈춤일 때만. "
                         "aggressive: 둘 다 전부)")
    ap.add_argument("--silence", choices=["on", "off"], default="on",
                    help="말 사이 무음 컷 (기본 on, 파라미터는 fillers.json 의 silence_*)")
    ap.add_argument("--hw", choices=["on", "off"], default="on",
                    help="맥 하드웨어 인코딩 h264_videotoolbox (기본 on, off 면 libx264)")
    ap.add_argument("--review", action="store_true", help="컷 리스트/검토표까지만 만들고 렌더링은 하지 않음")
    ap.add_argument("--apply-cuts", type=Path, help="기존 .cuts.json 으로 렌더 (전사 생략)")
    ap.add_argument("--embed-subs", choices=["on", "off"], default="on",
                    help="mp4 에 끄고 켤 수 있는 자막 트랙 넣기 (기본 on, 번인 아님)")
    ap.add_argument("--crf", type=int, default=18, help="--hw off 일 때 화질")
    ap.add_argument("--vbitrate", default="12M", help="--hw on 일 때 비트레이트")
    ap.add_argument("--preset", default="medium", help="--hw off 일 때 인코딩 속도")
    ap.add_argument("--force", action="store_true", help="work/ 캐시 무시하고 전부 다시")
    args = ap.parse_args()

    need("ffmpeg"); need("ffprobe")
    src = args.input.resolve()
    if not src.exists():
        sys.exit(f"오류: 입력 파일이 없습니다: {src}")
    t_start = time.time()

    stem = src.stem
    out_dir = args.out.resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    work = ROOT / "work" / stem; work.mkdir(parents=True, exist_ok=True)
    cfg = load_fillers(args.fillers)
    info = ffprobe(src)
    fps = info["fps"]
    total_frames = int(math.floor(info["duration"] * float(fps)))
    log(f"입력: {src.name}  {fmt_clock(info['duration'])}  {info['width']}x{info['height']}  "
        f"{float(fps):.3f}fps  {info['sample_rate']}Hz/{info['channels']}ch")

    # 1) 오디오
    clean_wav, asr_wav = prepare_audio(src, work, args.denoise, not args.no_loudnorm, args.force)

    # 2) 전사
    tr_path = out_dir / f"{stem}.transcript.json"
    tr = transcribe(asr_wav, tr_path, args.model, args.device, args.compute_type,
                    cfg.get("prompt", ""), args.force)

    # 3) 컷 리스트
    cuts_path = out_dir / f"{stem}.cuts.json"
    skipped: list[dict] = []
    if args.apply_cuts:
        data = json.loads(args.apply_cuts.read_text(encoding="utf-8"))
        cuts = [Cut(**{k: v for k, v in c.items() if k in Cut.__dataclass_fields__}) for c in data["cuts"]]
        cuts.sort(key=lambda c: c.start_frame)
        log(f"컷 리스트 불러옴: {args.apply_cuts.name} ({len(cuts)}곳)")
    else:
        habit_mode = {"on": "conservative", "off": "off", "aggressive": "aggressive"}[args.habit]
        raw_cuts, skipped = detect_fillers(tr, cfg, habit_mode, args.filler == "on")
        rms, hop = load_rms(asr_wav)
        n_fillers = len(raw_cuts)
        if args.silence == "on":
            sil_cuts, sil_skipped = detect_silences(tr, rms, hop, info["duration"], cfg)
            raw_cuts += sil_cuts
            skipped += sil_skipped
            log(f"무음 감지: {len(sil_cuts)}곳, {sum(c.end - c.start for c in sil_cuts):.1f}s "
                f"(건너뜀 {len(sil_skipped)}건)")
        cuts = refine_cuts(raw_cuts, rms, hop, fps, info["duration"], cfg)
        cuts_path.write_text(json.dumps(
            {"source": str(src), "fps": f"{fps.numerator}/{fps.denominator}",
             "filler": args.filler, "habit": args.habit, "silence": args.silence,
             "cuts": [asdict(c) for c in cuts]}, ensure_ascii=False, indent=1), encoding="utf-8")
        write_review(cuts, skipped, out_dir / f"{stem}.review.md")
        removed = sum(c.end - c.start for c in cuts)
        log(f"필러 {n_fillers}개 + 무음 {len(raw_cuts) - n_fillers}곳 → 병합 후 {len(cuts)}곳, 총 {removed:.1f}s 제거 예정 "
            f"(건너뜀 {len(skipped)}건)")
        if args.review:
            log(f"--review: 검토표 {out_dir / (stem + '.review.md')} 를 확인한 뒤 "
                f"--apply-cuts {cuts_path} 로 렌더하세요")
            return

    # 4) 렌더
    keeps = keep_segments(cuts, total_frames)
    cut_wav = work / "cut.wav"
    n_samples = cut_audio_numpy(clean_wav, cut_wav, keeps, fps)
    final_dur = n_samples / 48000.0
    out_mp4 = out_dir / f"{stem}_clean.mp4"
    if info["has_video"]:
        vcodec = "libx264"
        if args.hw == "on":
            encoders = run(["ffmpeg", "-hide_banner", "-encoders"], quiet=True).stdout
            if "h264_videotoolbox" in encoders:
                vcodec = "h264_videotoolbox"
            else:
                log("경고: h264_videotoolbox 를 쓸 수 없어 libx264 로 인코딩합니다")
        render_video(src, cut_wav, out_mp4, keeps, fps, work, vcodec, args.crf, args.vbitrate, args.preset)
    else:
        out_mp4 = out_dir / f"{stem}_clean.m4a"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", cut_wav,
             "-c:a", "aac", "-b:a", "192k", out_mp4])

    # 5) SRT — 영상과 같은 이름이어야 윈도우 기본 플레이어 등이 자동으로 불러온다
    remap = TimeRemap(cuts)
    srt_path = out_mp4.with_suffix(".srt")
    srt_path.write_text(build_srt(tr, remap, cfg["_always"]), encoding="utf-8")
    if info["has_video"] and args.embed_subs == "on":
        embed_subtitles(out_mp4, srt_path, work)

    write_report(out_dir / f"{stem}.report.md", src, info, cuts, skipped, final_dur, args, time.time() - t_start)
    log("완료")
    log(f"  영상: {out_mp4}")
    log(f"  자막: {srt_path}")
    log(f"  리포트: {out_dir / (stem + '.report.md')}")


if __name__ == "__main__":
    main()
