#!/usr/bin/env python3
"""
youtube-edit 파이프라인

  입력 영상 → (1) 노이즈 제거 + 라우드니스 정규화
           → (2) 한국어 전사 (faster-whisper, 단어 타임스탬프)
           → (3) 필러/습관어 + 말 사이 무음 감지 → 컷 리스트
           → (4) 컷 반영된 타임코드로 SRT 생성
           → (5) 컷 적용 (프레임 격자에 맞춰 A/V 싱크 유지, 자막 트랙 함께 mux)

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
  <이름>_clean.mp4     최종 영상 (끄고 켤 수 있는 자막 트랙 포함)
  <이름>_clean.srt     자막 (최종 영상 기준 타임코드, 영상과 같은 이름)
  <이름>.cuts.json     잘라낸 구간 목록 (수정 후 --apply-cuts 로 재적용 가능)
  <이름>.review.md     사람이 보기 좋은 컷 검토표
  <이름>.transcript.json  단어 타임스탬프 포함 전사 원본
  <이름>.report.md     요약 리포트
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DEFAULT_FILLERS = ROOT / "fillers.json"
CACHE_VERSION = 2  # 캐시 형식이 바뀌면 올린다 → 예전 캐시는 자동으로 다시 만든다

PUNCT_RE = re.compile(r"[\s\.\,\!\?\~\…\·\'\"\-\…\(\)\[\]「」『』、。:;]+")


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
PROGRESS = False  # --progress-json: 데스크톱 앱이 읽는 @progress 줄을 낼지


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def progress(stage: str, frac: float) -> None:
    """앱이 진행 막대를 그리도록 stdout 에 한 줄씩 낸다. stage: audio|transcribe|align|judge|preview|render"""
    if PROGRESS:
        print("@progress " + json.dumps({"stage": stage, "frac": round(min(1.0, max(0.0, frac)), 4)}), flush=True)


def ffmpeg_with_progress(cmd: list, total_sec: float, stage: str) -> None:
    """ffmpeg 를 돌리며 out_time_us / total_sec 을 진행률로 낸다. 실패하면 CalledProcessError.
    --progress-json 이 꺼져 있으면 예전처럼 ffmpeg 출력을 터미널에 그대로 둔다."""
    cmd = [str(c) for c in cmd]
    if not PROGRESS:
        subprocess.run(cmd, check=True)
        return
    # -progress 는 전역 옵션이라 맨 앞에. -stats 는 사람이 보는 줄이라 뺀다
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *[c for c in cmd[1:] if c != "-stats"]]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        # stderr 를 파이프로 받으면 stdout 을 읽는 동안 가득 차 멈출 수 있다 → 임시 파일로
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        last = -1.0
        for line in p.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.lstrip("-").isdigit() and total_sec > 0:
                frac = min(1.0, max(0.0, int(v) / 1e6 / total_sec))
                if frac - last >= 0.01:
                    progress(stage, frac)
                    last = frac
        code = p.wait()
        if code != 0:
            err.seek(0)
            log(f"명령 실패 (exit {code}): {Path(cmd[0]).name}")
            for l in err.read().strip().splitlines()[-15:]:
                log("  " + l)
            raise subprocess.CalledProcessError(code, cmd)
    progress(stage, 1.0)


def run(cmd: list, cwd: Path | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        shown = " ".join(str(c) for c in cmd)
        log("$ " + (shown if len(shown) < 400 else shown[:400] + " …"))
    try:
        return subprocess.run([str(c) for c in cmd], cwd=cwd, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        # 캡처한 stderr 를 버리면 "moov atom not found" 같은 원인이 안 보인다
        log(f"명령 실패 (exit {e.returncode}): {Path(str(cmd[0])).name}")
        for line in (e.stderr or "").strip().splitlines()[-15:]:
            log("  " + line)
        raise


def need(binary: str) -> None:
    if shutil.which(binary) is None:
        sys.exit(f"오류: {binary} 를 찾을 수 없습니다. macOS: brew install ffmpeg")


def ffmpeg_major() -> int:
    first = run(["ffmpeg", "-version"], quiet=True).stdout.splitlines()[0]
    m = re.search(r"version n?(\d+)\.", first)
    return int(m.group(1)) if m else 99  # git 빌드(N-xxxxx)는 최신으로 본다


def ffmpeg_has(kind: str, name: str) -> bool:
    out = run(["ffmpeg", "-hide_banner", f"-{kind}"], quiet=True).stdout
    return re.search(rf"\s{re.escape(name)}\s", out) is not None


def perf_cores() -> int:
    # Apple Silicon 은 E코어까지 쓰면 오히려 느려진다 (8스레드 17s → 6스레드 12s 측정)
    try:
        n = int(subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"], capture_output=True,
                               text=True, check=True).stdout.strip())
        if n > 0:
            return n
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return max(1, os.cpu_count() or 4)


def _frac(x) -> Fraction | None:
    try:
        f = Fraction(x)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def ffprobe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", path], quiet=True).stdout
    info = json.loads(out)
    # 오디오 파일에 붙은 앨범 아트(attached_pic)는 영상이 아니다
    v = next((s for s in info["streams"] if s["codec_type"] == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if a is None:
        sys.exit("오류: 오디오 트랙이 없는 파일입니다.")

    fps, vfr = Fraction(30), False
    if v:
        avg, rate = _frac(v.get("avg_frame_rate")), _frac(v.get("r_frame_rate"))
        # 가변 프레임(화면 녹화 등)은 평균이 실제 촬영 속도보다 훨씬 낮다 → 최대 속도 격자를 쓴다
        if avg and rate and rate > avg * Fraction(105, 100) and rate <= 120:
            fps, vfr = rate, True
        else:
            fps = avg or (rate if rate and rate <= 120 else None) or Fraction(30)

    fmt_dur = float(info["format"].get("duration") or 0)
    duration = float((v or {}).get("duration") or fmt_dur or a.get("duration") or 0)
    pix_fmt = (v or {}).get("pix_fmt", "")
    a_start = float(a.get("start_time") or 0)
    v_start = float(v.get("start_time") or 0) if v else a_start
    return {
        "has_video": v is not None,
        "fps": fps,
        "vfr": vfr,
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "duration": duration,
        "sample_rate": int(a.get("sample_rate", 48000)),
        "channels": int(a.get("channels", 2)),
        "audio_offset": a_start - v_start,  # >0: 오디오가 영상보다 늦게 시작
        "pix_fmt": pix_fmt,
        "high_bit": bool(re.search(r"p1[0-6]|p010|p016", pix_fmt)),
        "color": {k: (v or {}).get(k) for k in ("color_primaries", "color_transfer", "color_space")},
    }


def source_sig(src: Path) -> dict:
    st = src.stat()
    return {"path": str(src), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def fmt_srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))  # 반올림 후에 자리 올림 → "00:00:60,000" 이 생기지 않는다
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_clock(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    m, cs = divmod(cs, 6000)
    return f"{m:02d}:{cs / 100:05.2f}"


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # Windows 의 os.kill(pid, 0) 은 확인이 아니라 TerminateProcess 라 실행 중인 파이프라인을 죽인다
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: 다른 사용자의 살아 있는 프로세스
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """같은 영상을 두 파이프라인이 동시에 처리하면 work/ 와 output/ 파일을 서로 덮어쓴다."""
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
            except ValueError:
                pid = None  # 깨진 잠금 파일
            if pid is not None and pid != os.getpid() and pid_alive(pid):
                sys.exit(f"오류: 같은 영상을 이미 처리 중입니다 (pid {pid}). 끝날 때까지 기다리세요.")
            # 아니면 비정상 종료로 남은 잠금
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1) 오디오 정리: 노이즈 제거 + 라우드니스
# ---------------------------------------------------------------------------
def audio_filter_chain(denoise: str, loudnorm: bool, model_rel: str, offset: float) -> str:
    chain = ["highpass=f=70"]
    if denoise == "rnnoise":
        chain.append(f"arnndn=m={model_rel}:mix=0.85")
    elif denoise == "afftdn":
        chain.append("afftdn=nf=-25:nr=12:tn=1")
    if loudnorm:
        chain.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    # 오디오/영상 시작 시점 차이를 메워야 영상(setpts=PTS-STARTPTS)과 싱크가 맞는다.
    # 맨 끝에 둔다: 노이즈 제거·라우드니스가 앞에 붙인 무음까지 분석하지 않게 (adelay→arnndn→loudnorm 순서에서 출력이 무음이 되는 경우를 봄)
    if offset > 0.001:
        chain.append(f"adelay=delays={offset * 1000:.1f}:all=1")
    elif offset < -0.001:
        chain.append(f"asetpts=PTS-STARTPTS,atrim=start={-offset:.6f},asetpts=PTS-STARTPTS")
    return ",".join(chain)


def audio_settings(info: dict, denoise: str, loudnorm: bool) -> tuple[str, list]:
    """(ffmpeg 오디오 필터 체인, 다운믹스 인자). 분석용·렌더용 오디오가 같은 규칙을 쓴다."""
    if denoise == "rnnoise" and not (MODELS_DIR / "sh.rnnn").exists():
        log("경고: models/sh.rnnn 이 없어 afftdn 으로 대체합니다")
        denoise = "afftdn"
    if denoise == "rnnoise" and not ffmpeg_has("filters", "arnndn"):
        log("경고: 이 ffmpeg 에 arnndn 필터가 없어 afftdn 으로 대체합니다")
        denoise = "afftdn"
    offset = info["audio_offset"] if info["has_video"] else 0.0
    # ffmpeg 필터 인자에서 백슬래시는 이스케이프 문자라 Windows 에서도 슬래시 경로를 쓴다 (macOS 에선 변화 없음)
    model_rel = os.path.relpath(MODELS_DIR / "sh.rnnn", ROOT).replace(os.sep, "/")
    af = audio_filter_chain(denoise, loudnorm, model_rel, offset)
    # 3채널 이상 WAV 는 WAVE_FORMAT_EXTENSIBLE 헤더라 파이썬 wave 모듈이 못 읽는다 → 스테레오로 다운믹스
    ac = ["-ac", "2"] if info["channels"] > 2 else []
    return af, ac


def cached_meta(meta: Path, key: dict, outputs: list[Path], force: bool) -> bool:
    if force or not meta.exists() or not all(p.exists() for p in outputs):
        return False
    try:
        return json.loads(meta.read_text(encoding="utf-8")) == key
    except json.JSONDecodeError:
        return False


def prepare_audio(src: Path, work: Path, info: dict, denoise: str, loudnorm: bool,
                  force: bool) -> tuple[Path, Path, dict]:
    clean = work / "clean.wav"   # 48k, 스테레오 이하 — 최종 영상에 들어갈 오디오
    asr = work / "asr.wav"       # 16k mono — 전사/무음 분석용
    meta = work / "audio.meta.json"
    af, ac = audio_settings(info, denoise, loudnorm)
    offset = info["audio_offset"] if info["has_video"] else 0.0
    # 필터 체인 문자열 자체를 키에 넣어 처리 방식이 바뀌면 캐시도 자동으로 무효가 되게 한다
    key = {"v": CACHE_VERSION, "src": source_sig(src), "filter": af, "ac": ac}

    if cached_meta(meta, key, [clean, asr], force):
        log("오디오 정리 결과 재사용 (work/ 에 이미 있음)")
        progress("audio", 1.0)
        return clean, asr, key
    if meta.exists() and not force:
        log("원본이나 오디오 설정이 바뀌어 오디오를 다시 정리합니다")

    if abs(offset) > 0.001:
        log(f"오디오/영상 시작 차이 {offset * 1000:+.0f}ms 보정")
    log(f"오디오 정리: {af}")
    if ac:
        log(f"{info['channels']}채널 오디오를 스테레오로 다운믹스합니다")
    meta.unlink(missing_ok=True)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-vn", "-af", af, "-ar", "48000", *ac, "-c:a", "pcm_s16le", clean], cwd=ROOT)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", clean,
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", asr], cwd=ROOT)
    meta.write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")
    progress("audio", 1.0)
    return clean, asr, key


def prepare_render_audio(src: Path, work: Path, info: dict, denoise: str, loudnorm: bool,
                         force: bool) -> Path:
    """렌더에만 쓰는 오디오. 분석(전사·무음)은 기본 설정 오디오로 해서 캐시를 살리고,
    사용자가 고른 배경 잡음/음량 설정은 여기서만 적용한다. 캐시 방식은 prepare_audio 와 같다."""
    out, meta = work / "render.wav", work / "render.meta.json"
    af, ac = audio_settings(info, denoise, loudnorm)
    key = {"v": CACHE_VERSION, "src": source_sig(src), "filter": af, "ac": ac}
    if cached_meta(meta, key, [out], force):
        log("렌더용 오디오 재사용 (work/render.wav)")
        return out
    log(f"렌더용 오디오 정리: {af}")
    meta.unlink(missing_ok=True)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-vn", "-af", af, "-ar", "48000", *ac, "-c:a", "pcm_s16le", out], cwd=ROOT)
    meta.write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")
    return out


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


# ---------------------------------------------------------------------------
# 2) 전사
# ---------------------------------------------------------------------------
def whisper_options(prompt: str) -> dict:
    return dict(
        language="ko",
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
        condition_on_previous_text=False,
        # initial_prompt 는 condition_on_previous_text=False 면 첫 30초 창에만 들어간다.
        # hotwords 는 창마다 다시 붙으므로 영상 끝까지 "어/음"이 전사에 남는다.
        hotwords=prompt or None,
        hallucination_silence_threshold=2.0,
    )


WIN_CRASH_CODES = {3221225477, -1073741819,   # 0xC0000005 접근 위반
                   3221226505, -1073740791}   # 0xC0000409 (CUDA/cuDNN 로드 실패 때 흔함)


def probe_whisper_windows(model_name: str, device: str, compute_type: str, threads: int, marker: Path) -> None:
    """일부 Windows PC 에선 ctranslate2 4.5+ 가 모델 로드·첫 전사에서 파이썬 오류 없이 프로세스째 죽는다.
    본 프로세스에서 죽으면 사용자는 원인을 알 수 없으니, 처음 한 번 별도 프로세스로 로드+짧은 전사를 해 보고
    죽으면 고치는 방법을 알려 준다. 성공하면 work/ 에 기록해 ctranslate2 버전·설정이 같으면 다시 하지 않는다."""
    from importlib.metadata import version
    key = {"ctranslate2": version("ctranslate2"), "model": model_name, "device": device, "compute_type": compute_type}
    try:
        if json.loads(marker.read_text(encoding="utf-8")) == key:
            return
    except (OSError, json.JSONDecodeError):
        pass
    log("Windows 호환성 확인: 별도 프로세스에서 Whisper 모델을 한 번 띄워 봅니다 "
        "(처음 한 번. 모델을 아직 안 받았으면 다운로드까지 해서 몇 분 걸릴 수 있음)")
    code = ("import numpy as np; from faster_whisper import WhisperModel; "
            f"m = WhisperModel({model_name!r}, device={device!r}, compute_type={compute_type!r}, cpu_threads={threads}); "
            "list(m.transcribe(np.zeros(16000, dtype=np.float32), language='ko', vad_filter=False)[0])")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if res.returncode == 0:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(key), encoding="utf-8")
        return
    if res.returncode == 1 and "Traceback" in res.stderr:
        return  # 보통의 파이썬 오류(네트워크 등)는 본 실행에서 그대로 보여 준다
    if res.returncode in WIN_CRASH_CODES or res.returncode < 0 or res.returncode > 255:
        if device != "cpu":
            sys.exit(f"오류: Whisper 모델을 띄우다 프로세스가 죽었습니다 (종료 코드 {res.returncode}, device={device}).\n"
                     "  GPU(CUDA/cuDNN) 설정 문제일 가능성이 큽니다. --device cpu 를 붙여 다시 실행하세요.")
        head = (f"오류: Whisper 모델을 띄우다 프로세스가 죽었습니다 (종료 코드 {res.returncode}, "
                f"ctranslate2 {key['ctranslate2']}).\n")
        if tuple(int(x) for x in re.findall(r"\d+", key["ctranslate2"])[:2]) >= (4, 5):
            sys.exit(head + "  이 PC 는 ctranslate2 4.5 이상과 맞지 않는 경우입니다. 아래로 내린 뒤 다시 실행하세요:\n"
                     '  .venv\\Scripts\\python -m pip install "ctranslate2==4.4.0"\n'
                     "  (그래도 같으면 VC++ 런타임: winget install Microsoft.VCRedist.2015+.x64)")
        sys.exit(head + "  VC++ 런타임이 없거나 오래된 경우가 많습니다. 설치 후 터미널을 새로 열고 다시 실행하세요:\n"
                 "  winget install Microsoft.VCRedist.2015+.x64")


def use_cuda_dir(is_windows: bool = os.name == "nt") -> None:
    """앱이 받아 둔 CUDA DLL 폴더(환경변수 YTEDIT_CUDA_DIR)를 윈도우 DLL 검색 경로에 더한다.
    PATH 에도 앞에 붙여 probe 하위 프로세스가 같은 DLL 을 찾게 한다. 변수가 없으면 아무것도 안 한다 (CLI)."""
    d = os.environ.get("YTEDIT_CUDA_DIR")
    if not (is_windows and d and os.path.isdir(d)):
        return
    os.add_dll_directory(d)
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def load_whisper(model_name: str, device: str, compute_type: str, probe_marker: Path):
    use_cuda_dir()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("오류: faster-whisper 가 설치되지 않았습니다. ./setup.sh 를 먼저 실행하세요.")
    threads = perf_cores()
    if os.name == "nt":
        probe_whisper_windows(model_name, device, compute_type, threads, probe_marker)
    log(f"Whisper 모델 로드: {model_name} (device={device}, compute={compute_type}, threads={threads})")
    return WhisperModel(model_name, device=device, compute_type=compute_type, cpu_threads=threads)


def transcribe(asr_wav: Path, out_json: Path, audio_key: dict, model_name: str, device: str,
               compute_type: str, prompt: str, force: bool, probe_marker: Path) -> dict:
    key = {"audio": audio_key, "model": model_name, "compute_type": compute_type, "hotwords": prompt}
    if out_json.exists() and not force:
        try:
            cached = json.loads(out_json.read_text(encoding="utf-8"))
            if cached.get("cache_key") == key:
                log(f"전사 결과 재사용: {out_json.name}")
                progress("transcribe", 1.0)
                return cached
        except json.JSONDecodeError:
            pass
        log("원본·오디오·모델·프롬프트 중 바뀐 것이 있어 전사를 다시 합니다")

    log("  처음 한 번은 모델 다운로드(1~3GB)로 시간이 걸립니다")
    model = load_whisper(model_name, device, compute_type, probe_marker)

    log("전사 시작 (한국어, 단어 타임스탬프, 필러 보존 프롬프트)")
    segments_iter, info = model.transcribe(str(asr_wav), **whisper_options(prompt))
    segments = []
    t0 = time.time()
    for i, seg in enumerate(segments_iter):
        words = [{"word": w.word, "start": float(w.start), "end": float(w.end),
                  "prob": float(w.probability)} for w in (seg.words or [])]
        segments.append({"id": i, "start": float(seg.start), "end": float(seg.end),
                         "text": seg.text.strip(), "words": words})
        if info.duration:
            progress("transcribe", seg.end / info.duration)
        if i % 20 == 0:
            log(f"  … {fmt_clock(seg.end)} 까지 전사 ({time.time()-t0:.0f}s)")
    result = {"language": info.language, "duration": float(info.duration), "model": model_name,
              "cache_key": key, "segments": segments}
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"전사 완료: {len(segments)} 세그먼트, {sum(len(s['words']) for s in segments)} 단어")
    progress("transcribe", 1.0)
    return result


def transcript_sha1(tr: dict) -> str:
    """cuts.json 의 words_idx 가 가리키는 전사가 그대로인지 확인하는 지문"""
    words = [[w["word"], round(w["start"], 2)] for seg in tr["segments"] for w in seg["words"]]
    return hashlib.sha1(json.dumps(words, ensure_ascii=False).encode("utf-8")).hexdigest()


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
    lists = {"_always": "always", "_habit": "habit", "_pause_after": "habit_pause_after",
             "_never": "never_cut"}
    for k, name in lists.items():
        cfg[k] = {normalize(w) for w in cfg.get(name, [])}
    # "어떻게 보면" 처럼 띄어 쓴 항목은 전사에서 단어 여러 개로 나뉜다 → 이어 붙여 비교할 길이들
    cfg["_single_only"] = {normalize(w) for w in cfg.get("single_only", [])}
    cfg["_phrase_lens"] = sorted({len(w.split()) for name in lists.values()
                                  for w in cfg.get(name, []) if len(w.split()) > 1}, reverse=True)
    return cfg


def all_words(tr: dict) -> list[tuple[int, int, dict]]:
    return [(seg["id"], i, w) for seg in tr["segments"] for i, w in enumerate(seg["words"])]


def is_repeated_sound(words: list, norms: list[str], k: int, single_only: set[str]) -> bool:
    """'하하', '허 허' 처럼 반복된 single_only 단어는 웃음이라 필러로 보지 않는다"""
    nw = norms[k]
    if nw not in single_only:
        return False
    raw = PUNCT_RE.sub("", words[k][2]["word"].strip())
    return (len(raw) >= 2 or (k > 0 and norms[k - 1] == nw)
            or (k + 1 < len(norms) and norms[k + 1] == nw))


def detect_fillers(tr: dict, cfg: dict, habit_mode: str, filler: bool = True) -> tuple[list[Cut], list[dict]]:
    min_gap = float(cfg.get("min_gap_habit_sec", 0.22))
    max_dur = float(cfg.get("max_filler_duration_sec", 1.3))
    known = cfg["_always"] | cfg["_habit"] | cfg["_pause_after"]
    words = all_words(tr)  # 세그먼트 경계를 넘어 앞뒤 단어를 본다
    norms = [normalize(w["word"]) for _, _, w in words]
    cuts: list[Cut] = []
    skipped: list[dict] = []

    k = 0
    while k < len(words):
        n = next((m for m in cfg["_phrase_lens"]
                  if k + m <= len(words) and "".join(norms[k:k + m]) in known), 1)
        nw = "".join(norms[k:k + n])
        span = words[k:k + n]
        first, last = span[0][2], span[-1][2]
        k_next = k + n
        k_cur, k = k, k + n
        if not nw or nw in cfg["_never"] or nw not in known:
            continue
        if n == 1 and is_repeated_sound(words, norms, k_cur, cfg["_single_only"]):
            continue
        is_always = nw in cfg["_always"]
        is_pause_after = nw in cfg["_pause_after"]
        gap_after = (words[k_next][2]["start"] - last["end"]) if k_next < len(words) else 9.0

        reason = None
        if is_always:
            if not filler:
                continue
            reason = "filler"
        elif habit_mode == "off":
            continue
        elif habit_mode == "aggressive":
            reason = "habit"
        elif is_pause_after:  # conservative: 뜻이 있을 수 있는 단어(그/저/좀 …)는 뒤 멈춤일 때만
            if gap_after >= min_gap:
                reason = "habit(뒤 멈춤)"
        else:                 # conservative: 순수 습관어(그니까/약간/이제 …)는 전부
            reason = "habit"
        if reason is None:
            continue

        text = " ".join(x[2]["word"].strip() for x in span)
        ctx_prev = " ".join(x[2]["word"].strip() for x in words[max(0, k_cur - 3):k_cur])
        ctx_next = " ".join(x[2]["word"].strip() for x in words[k_next:k_next + 3])
        context = f"{ctx_prev} [{text}] {ctx_next}".strip()
        dur = last["end"] - first["start"]
        if dur > max_dur * n or dur <= 0:
            skipped.append({"start": first["start"], "end": last["end"], "word": text,
                            "why": f"단어 길이 {dur:.2f}s 가 비정상 (타임스탬프 불안정)", "context": context})
            continue
        cuts.append(Cut(first["start"], last["end"], text, reason, context, span[0][0],
                        words_idx=[[s, i] for s, i, _ in span]))
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


def quiet_mask(rms: list[float], hop: float, cfg: dict):
    import numpy as np
    thr = 10 ** (float(cfg.get("silence_threshold_db", -40)) / 20)
    k = max(1, int(round(0.05 / hop)))  # 50ms 이동평균 — 짧은 잡음 스파이크로 무음이 쪼개지는 것 방지
    return np.convolve(np.asarray(rms), np.ones(k) / k, mode="same") < thr


def auto_silence(tr: dict, rms: list[float], hop: float, cfg: dict) -> dict:
    """영상마다 무음 기준을 배경 소리에 맞춘다. 말 없는 틈(단어 앞뒤 0.1초 밖)의 하위 20% 크기를 배경 바닥으로 보고
    바닥 + silence_auto_margin_db 를 무음 기준으로 쓴다. 단 silence_threshold_db 보다 내리지 않고(조용한 실내는 그대로),
    silence_auto_max_db 와 말소리 중간값 - 6dB 보다 올리지 않는다. 무음 잇기 기준도 무음 기준 + 10dB 까지 같이 올린다.
    틈이 2초 미만이면 바닥을 믿을 수 없어 그대로 둔다. → cfg 에 덮어쓸 값 (silence_noise_floor_db 는 기록용)"""
    import numpy as np
    base = float(cfg.get("silence_threshold_db", -40))
    bridge = float(cfg.get("silence_bridge_max_db", -30))
    out = {"silence_threshold_db": base, "silence_bridge_max_db": bridge}
    if not cfg.get("silence_auto", False) or not rms:
        return out
    k = max(1, int(round(0.05 / hop)))  # quiet_mask 와 같은 50ms 이동평균
    db = 20 * np.log10(np.maximum(np.convolve(np.asarray(rms), np.ones(k) / k, mode="same"), 1e-9))
    gap = np.ones(len(db), dtype=bool)
    for _, _, w in all_words(tr):
        gap[max(0, int((w["start"] - 0.1) / hop)):max(0, int(math.ceil((w["end"] + 0.1) / hop)))] = False
    if gap.sum() * hop < 2.0 or gap.all():
        return out
    floor = float(np.percentile(db[gap], 20))
    cap = min(float(cfg.get("silence_auto_max_db", -30)),
              float(np.median(db[~gap])) - float(cfg.get("silence_auto_speech_margin_db", 6)))
    thr = round(max(base, min(floor + float(cfg.get("silence_auto_margin_db", 5)), cap)), 1)
    return {"silence_threshold_db": thr, "silence_bridge_max_db": max(bridge, thr + 10),
            "silence_noise_floor_db": round(floor, 1)}


def runs(mask) -> list[tuple[int, int]]:
    """True 가 이어지는 [시작, 끝) 프레임 구간들"""
    import numpy as np
    edges = np.flatnonzero(np.diff(np.concatenate(([0], np.asarray(mask, dtype=np.int8), [0]))))
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist()))


def find_suspect_words(tr: dict, quiet, hop: float, cfg: dict) -> dict[tuple[int, int], float]:
    """단어 안에 무음(silence_min_sec 이상)이 끼어 있고 그 앞뒤로 소리가 있으면 Whisper 가 단어를
    엉뚱한 자리에 놓은 것이다 (앞 단어 꼬리 + 무음 + 숨소리를 한 단어로 묶음). → {(seg, i): 끼인 무음 길이}
    끝에만 무음이 붙은 단어는 흔한 단어 시간 늘어짐이라 제외한다."""
    min_len = float(cfg.get("silence_min_sec", 0.3))
    out = {}
    for seg, i, w in all_words(tr):
        f0, f1 = int(w["start"] / hop), int(math.ceil(w["end"] / hop))
        sub = quiet[f0:f1]
        inner = [(a, b) for a, b in runs(sub) if a > 0 and b < len(sub) and (b - a) * hop >= min_len]
        if inner:
            out[(seg, i)] = max(b - a for a, b in inner) * hop
    return out


ALIGN_VERSION = 2  # 정렬 방식이 바뀌면 올린다 → 저장된 보정 시간을 되돌리고 다시 정렬
CTC_MODEL = "kresnik/wav2vec2-large-xlsr-korean"  # 한글 음절 단위 CTC 모델 (WhisperX 가 한국어에 쓰는 것)


class CtcAligner:
    """wav2vec2 CTC 강제 정렬. 텍스트를 알 때 글자마다 소리가 난 프레임을 찾는다.
    긴 구간에선 이 모델도 헤매므로 필러 주변 몇 초짜리 창에만 쓴다."""
    def __init__(self, model_id: str):
        import torch
        from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
        log(f"정렬 모델 로드: {model_id} (처음 한 번은 약 1.2GB 다운로드)")
        self.torch = torch
        self.proc = Wav2Vec2Processor.from_pretrained(model_id)
        self.model = Wav2Vec2ForCTC.from_pretrained(model_id).eval()
        self.vocab = self.proc.tokenizer.get_vocab()
        self.blank = self.proc.tokenizer.pad_token_id

    def align(self, pcm, sr: int, texts: list[str]) -> list[list[float]] | None:
        """texts(단어들)의 글자마다 정렬된 시각(창 시작 기준 초). 모델이 모르는 글자가 있으면 None"""
        import numpy as np
        chars = [[ch for ch in PUNCT_RE.sub("", t)] for t in texts]
        if any(not c or any(ch not in self.vocab for ch in c) for c in chars):
            return None
        inp = self.proc(pcm, sampling_rate=sr, return_tensors="pt").input_values
        with self.torch.no_grad():
            em = self.torch.log_softmax(self.model(inp).logits[0], -1).numpy()
        T = em.shape[0]
        fr = len(pcm) / sr / T
        seq = [self.blank]
        for c in chars:
            for ch in c:
                seq += [self.vocab[ch], self.blank]
        L = len(seq)
        if T < (L - 1) // 2:
            return None
        # CTC 비터비: 상태 s 는 blank 를 끼운 토큰열의 위치
        dp = np.full((T, L), -np.inf)
        bp = np.zeros((T, L), dtype=np.int64)
        tok = np.asarray(seq)
        skip_ok = np.zeros(L, dtype=bool)
        skip_ok[2:] = (tok[2:] != self.blank) & (tok[2:] != tok[:-2])
        dp[0, :2] = em[0, tok[:2]]
        for t in range(1, T):
            stay = dp[t - 1]
            step = np.concatenate(([-np.inf], dp[t - 1, :-1]))
            jump = np.where(skip_ok, np.concatenate(([-np.inf, -np.inf], dp[t - 1, :-2])), -np.inf)
            best = np.stack([stay, step, jump])
            arg = best.argmax(0)
            dp[t] = best[arg, np.arange(L)] + em[t, tok]
            bp[t] = np.arange(L) - arg
        s = L - 1 if dp[T - 1, L - 1] >= dp[T - 1, L - 2] else L - 2
        first: dict[int, int] = {}
        for t in range(T - 1, -1, -1):
            if s % 2 == 1:
                first[(s - 1) // 2] = t  # 거꾸로 따라가므로 마지막에 남는 값이 그 글자의 첫 프레임
            s = bp[t, s]
        times = [(first[i] + 0.5) * fr for i in range((L - 1) // 2)]
        out, i = [], 0
        for c in chars:
            out.append(times[i:i + len(c)])
            i += len(c)
        return out


def filler_needs_ctc(k: int, words: list, cfg: dict, quiet, hop: float, suspects: dict) -> tuple[float, float] | None:
    """필러 단어의 Whisper 시간이 믿을 만한지 본다. 필러와 끊김 없이 이어진 소리 중에 다른 단어가 설명하지 못하는
    부분이 있으면 ("앞으로좀" 에서 "앞으로" 가 엉뚱한 곳에 놓임) 필러 시간도 틀렸을 수 있다 → 그 소리 덩어리 구간을 돌려준다."""
    w = words[k][2]
    f0, f1 = int(w["start"] / hop), int(math.ceil(w["end"] / hop))
    voiced_runs = [(a, b) for a, b in runs(~quiet[max(0, f0 - 1):f1 + 1])]
    if not voiced_runs:
        return None
    base = max(0, f0 - 1)
    # 필러와 겹치는 소리 덩어리를 양옆으로 끝까지 넓힌다
    rs, re_ = base + voiced_runs[0][0], base + voiced_runs[-1][1]
    while rs > 0 and not quiet[rs - 1]:
        rs -= 1
    while re_ < len(quiet) and not quiet[re_]:
        re_ += 1
    others = [x[2] for j, x in enumerate(words) if j != k and (x[0], x[1]) not in suspects]

    def unexplained(t0: float, t1: float) -> bool:
        if t1 - t0 < 0.1:
            return False
        cover = sum(max(0.0, min(t1, o["end"]) - max(t0, o["start"])) for o in others)
        return cover < 0.7 * (t1 - t0)
    if unexplained(rs * hop, w["start"]) or unexplained(w["end"], re_ * hop):
        return rs * hop, re_ * hop
    return None


def ctc_fix_fillers(tr: dict, asr_wav: Path, rms: list[float], hop: float, cfg: dict,
                    ctc_model: str = CTC_MODEL) -> tuple[int, list[dict]]:
    """시간이 의심스러운 필러만 주변 몇 초를 CTC 로 정렬해 필러와 이웃 단어의 시간을 바로잡는다 (tr 을 직접 고침).
    정렬이 그럴듯할 때만 쓴다: 모든 글자가 소리 나는 곳에 있고, 필러가 Whisper 위치에서 0.3초 안에 있어야 한다."""
    import numpy as np
    quiet = quiet_mask(rms, hop, cfg)
    words = all_words(tr)
    suspects = find_suspect_words(tr, quiet, hop, cfg)
    known = cfg["_always"] | cfg["_habit"] | cfg["_pause_after"]
    todo = []
    for k, (_, _, w) in enumerate(words):
        nw = normalize(w["word"])
        if nw in known and nw not in cfg["_never"]:
            run_ = filler_needs_ctc(k, words, cfg, quiet, hop, suspects)
            if run_:
                todo.append((k, run_))
    if not todo:
        return 0, []
    try:
        aligner = CtcAligner(ctc_model)
    except ImportError:
        log("경고: torch/transformers 가 없어 필러 시간 보정을 건너뜁니다 (./setup.sh 로 설치). 해당 필러는 review.md 참고")
        return 0, [{"start": words[k][2]["start"], "end": words[k][2]["end"], "word": words[k][2]["word"].strip(),
                    "why": "필러 시간 불확실 (정렬 모델 없음) — 앞뒤 단어가 같이 잘렸는지 확인", "context": ""}
                   for k, _ in todo]
    with wave.open(str(asr_wav), "rb") as wf:
        sr = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    r = np.asarray(rms)
    margin, tol, reach = 0.3, 0.3, 0.3  # 창 여유 / 필러가 Whisper 위치에서 벗어나도 되는 폭 / 글자 앞뒤로 소리를 넓히는 한도
    fixed, failed = 0, []
    n_frames = len(quiet)

    def voiced_near(t: float) -> bool:  # CTC 는 글자를 소리 시작보다 한두 프레임 일찍 찍기도 한다
        i = int(t / hop)
        return not quiet[max(0, i - 6):min(n_frames, i + 7)].all()

    def edge(t: float, direction: int) -> float:
        """글자 시각에서 바깥쪽 reach 초 안의 무음 경계, 없으면 가장 조용한 지점 (붙어 있는 다른 소리까지 먹지 않게)"""
        i = min(n_frames - 1, int(t / hop))
        lo, hi = (max(0, i - int(reach / hop)), i) if direction < 0 else (i + 1, min(n_frames, i + 1 + int(reach / hop)))
        if hi <= lo:
            return t
        rng = range(hi - 1, lo - 1, -1) if direction < 0 else range(lo, hi)
        for j in rng:
            if quiet[j]:
                return (j + 1) * hop if direction < 0 else j * hop
        return (lo + int(np.argmin(r[lo:hi]))) * hop
    for k, (rs, re_) in todo:
        w = words[k][2]
        t0, t1 = max(0.0, min(rs, w["start"]) - margin), min(len(pcm) / sr, max(re_, w["end"]) + margin)
        # 창 안에 들어오는 이웃 단어(자리가 의심스러운 단어는 어디 있든 후보)
        cand = [j for j in (k - 1, k + 1) if 0 <= j < len(words)
                and ((words[j][0], words[j][1]) in suspects
                     or (words[j][2]["end"] > t0 and words[j][2]["start"] < t1))]
        ok = None
        for drop in ([], [j for j in cand if (words[j][0], words[j][1]) in suspects], cand):
            ids = sorted([k] + [j for j in cand if j not in drop])
            times = aligner.align(pcm[int(t0 * sr):int(t1 * sr)], sr, [words[j][2]["word"] for j in ids])
            if times is None:
                continue
            times = [[t0 + x for x in ts] for ts in times]
            flat = [x for ts in times for x in ts]
            in_voice = all(voiced_near(x) for x in flat)
            mine = times[ids.index(k)]
            near = w["start"] - tol <= mine[0] and mine[-1] <= w["end"] + tol
            if in_voice and near:
                ok = (ids, times)
                break
        if not ok:
            failed.append({"start": w["start"], "end": w["end"], "word": w["word"].strip(),
                           "why": "필러 시간 불확실 (정렬 실패, Whisper 시간으로 컷) — 앞뒤 단어가 같이 잘렸는지 확인",
                           "context": ""})
            continue
        ids, times = ok
        # 단어 경계: 앞 단어 마지막 글자와 다음 단어 첫 글자 사이에 무음이 있으면 무음 양끝, 없으면 다음 단어 첫 글자
        # 바로 앞(0.1초 안)의 가장 조용한 지점. 이 모델은 글자를 음절 첫머리에 찍으므로 그 직전이 자음 막힘(경계)이다
        bounds = []
        for (ja, ta), (jb, tb) in zip(zip(ids, times), list(zip(ids, times))[1:]):
            a_, b_ = int(ta[-1] / hop) + 1, int(tb[0] / hop)
            q = [(x0 + a_, x1 + a_) for x0, x1 in runs(quiet[a_:b_])] if b_ > a_ else []
            if q:
                bounds.append((q[0][0] * hop, q[-1][1] * hop))
            else:
                lo = max(a_, b_ - int(0.1 / hop))
                hi = min(n_frames, b_ + 3)
                m = (lo + int(np.argmin(r[lo:hi]))) * hop if hi > lo else tb[0]
                bounds.append((m, m))
        starts = [edge(times[0][0], -1)] + [b for _, b in bounds]
        ends = [a for a, _ in bounds] + [edge(times[-1][-1], +1)]
        for j, s_, e_ in zip(ids, starts, ends):
            ww = words[j][2]
            if j != k and (words[j][0], words[j][1]) not in suspects:
                # 제자리에 있던 이웃 단어는 필러와 맞닿은 쪽 경계만 옮긴다
                s_, e_ = (ww["start"], e_) if j < k else (s_, ww["end"])
            if e_ - s_ < hop:
                continue
            ww.setdefault("whisper", [ww["start"], ww["end"]])  # 원래 시간 (설정을 바꿔 다시 정렬할 때 되돌림)
            ww["start"], ww["end"] = round(s_, 3), round(e_, 3)
        fixed += 1
    return fixed, failed


def detect_silences(tr: dict, rms: list[float], hop: float, duration: float,
                    cfg: dict, suspects: dict = None) -> tuple[list[Cut], list[dict]]:
    """말 사이 빈 구간 감지. 앞뒤로 silence_pad_sec 만 남기고 나머지를 컷한다."""
    import numpy as np
    min_len = float(cfg.get("silence_min_sec", 0.3))
    pad = float(cfg.get("silence_pad_sec", 0.08))
    bridge = float(cfg.get("silence_bridge_sec", 0.3))
    bridge_amp = 10 ** (float(cfg.get("silence_bridge_max_db", -30)) / 20)
    r = np.asarray(rms)
    quiet = quiet_mask(rms, hop, cfg)
    words = [w for _, _, w in all_words(tr)]
    word_starts = [w["start"] for w in words]
    real = [w for (s, i, w) in all_words(tr) if (s, i) not in (suspects or {})]
    # 무음 사이에 낀 짧고 작은 소리(숨소리·입소리)는 무음으로 친다. 쪼개진 무음마다 여유를 남기면 조각이 생긴다
    q = runs(quiet)
    for (_, b0), (a1, _) in zip(q, q[1:]):
        gs, ge = b0 * hop, a1 * hop
        if (ge - gs <= bridge and r[b0:a1].max() < bridge_amp
                and not any(w["start"] < ge and w["end"] > gs for w in real)):
            quiet[b0:a1] = True

    cuts: list[Cut] = []
    skipped: list[dict] = []
    for a, b in runs(quiet):
        s, e = a * hop, min(duration, b * hop)
        if e - s < min_len:
            continue
        i = bisect.bisect_left(word_starts, s)
        ctx_prev = " ".join(x["word"].strip() for x in words[max(0, i - 3):i])
        ctx_next = " ".join(x["word"].strip() for x in words[i:i + 3])
        context = f"{ctx_prev} [{e-s:.1f}s 무음] {ctx_next}".strip()
        pieces = [(s if a == 0 else s + pad,            # 영상 맨 앞/맨 뒤 무음은 여유 없이 전부 컷
                   e if b >= len(r) else e - pad)]
        # 무음 안에 단어가 통째로 들어 있으면 작은 목소리일 수 있다 → 그 단어 주변만 남기고 나머지는 컷
        j = i
        while j < len(words) and words[j]["start"] < e:
            w = words[j]
            if w["end"] <= e:
                ps, pe = w["start"] - pad, w["end"] + pad
                pieces = [q for x0, x1 in pieces for q in ((x0, min(x1, ps)), (max(x0, pe), x1))]
                skipped.append({"start": w["start"], "end": w["end"], "word": w["word"].strip(),
                                "why": "무음 안의 작은 소리 (단어 주변만 남기고 나머지 무음은 컷)",
                                "context": context})
            j += 1
        for cs, ce in pieces:
            if ce - cs >= 0.05:
                cuts.append(Cut(cs, ce, "(무음)", "silence", context, -1, words_idx=[]))
    return cuts, skipped


def silence_level_cfg(cfg: dict, level: int) -> dict:
    """--silence-level 0~100 → cfg 에 덮어쓸 무음 값. 1~50: 최소 길이 1.0초 → 0.3초 (소리 크기 기준은 그대로).
    50~100: 최소 길이 0.3초, 자동 무음 기준의 상한(-30dB → -6dB)과 "말소리 중간값 − 여유"의 여유(6 → 2dB)를 풀어
    기준을 말소리 쪽으로 올린다. 50 은 지금 값(0.3, -30, 6)과 같다. 0(끔)·100(단어 없는 틈)은 silence_cuts_for_level."""
    level = max(0, min(100, int(level)))
    if level <= 50:
        return {"silence_min_sec": round(1.0 - 0.7 * level / 50, 4)}
    f = (level - 50) / 50
    return {"silence_min_sec": 0.3, "silence_auto_max_db": round(-30 + 24 * f, 4),
            "silence_auto_speech_margin_db": round(6 - 4 * f, 4)}


def detect_word_gaps(tr: dict, duration: float, cfg: dict, suspects: dict = None) -> list[Cut]:
    """전사된 단어가 없는 틈(처음·끝 포함)은 소리가 있어도 컷 (풍경 장면, 전사 안 된 목소리).
    자리가 의심스러운 단어(find_suspect_words)는 틈을 막지 않는다. 앞뒤 silence_pad_sec 남기고, 영상 맨 앞/뒤는 여유 없이."""
    min_len = float(cfg.get("silence_min_sec", 0.3))
    pad = float(cfg.get("silence_pad_sec", 0.08))
    words = sorted((w for s, i, w in all_words(tr) if (s, i) not in (suspects or {})), key=lambda w: w["start"])
    cuts: list[Cut] = []
    prev = 0.0
    for j in range(len(words) + 1):
        last = j == len(words)
        gs, ge = prev, (duration if last else min(duration, words[j]["start"]))
        if ge - gs >= min_len:
            cs, ce = (gs if j == 0 else gs + pad), (ge if last else ge - pad)
            if ce - cs >= 0.05:
                ctx_prev = " ".join(x["word"].strip() for x in words[max(0, j - 3):j])
                ctx_next = " ".join(x["word"].strip() for x in words[j:j + 3])
                cuts.append(Cut(cs, ce, "(무음)", "gap", f"{ctx_prev} [{ge-gs:.1f}s 단어 없는 틈] {ctx_next}".strip(),
                                -1, words_idx=[]))
        if not last:
            prev = max(prev, words[j]["end"])
    return cuts


def _union_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def silence_cuts_for_level(tr: dict, rms: list[float], hop: float, duration: float, cfg: dict, level: int,
                           suspects: dict = None) -> tuple[list[Cut], list[dict], dict]:
    """--silence-level 의 무음 컷. cfg 는 auto_silence 를 적용하기 전 설정(fillers.json 그대로)이다.
    → (컷, 건너뜀, 무음 판정에 쓴 cfg). 0 은 컷 없음, 50 은 지금의 --silence on 과 같다.
    50 을 넘으면 50 과 그 사이 10 단위 값들의 무음 컷을 합친다 — 기준을 올리면 무음이 길어져 그 안의 단어 주변을
    새로 남기는 일이 생겨 드물게 잘린 곳이 줄 수 있기 때문 (값이 커지면 잘린 곳이 줄지 않게). 100 은 단어 없는 틈도 컷."""
    level = max(0, min(100, int(level)))
    if level <= 0:
        return [], [], {}

    def at(lv: int):
        c = {**cfg, **silence_level_cfg(cfg, lv)}
        c.update(auto_silence(tr, rms, hop, c))
        return (c, *detect_silences(tr, rms, hop, duration, c, suspects))

    if level <= 50:  # 최소 길이만 달라진다 → 값이 작을수록 50 의 컷 중 일부
        c, cuts, skipped = at(level)
        return cuts, skipped, c
    found, skipped, seen = [], [], set()
    for lv in sorted({50, *range(60, level, 10), level}):
        c, cuts, sk = at(lv)
        found += cuts
        for x in sk:
            if (x["start"], x["end"], x["word"]) not in seen:
                seen.add((x["start"], x["end"], x["word"]))
                skipped.append(x)
    out = []
    for s, e in _union_spans([(x.start, x.end) for x in found]):
        first = min((x for x in found if s - 1e-9 <= x.start and x.end <= e + 1e-9), key=lambda x: x.start)
        out.append(Cut(s, e, "(무음)", "silence", first.context, -1, words_idx=[]))
    if level >= 100:
        covered = _union_spans([(x.start, x.end) for x in out])
        for g in detect_word_gaps(tr, duration, c, suspects):
            pieces = [(g.start, g.end)]
            for s, e in covered:
                pieces = [q for x0, x1 in pieces for q in ((x0, min(x1, s)), (max(x0, e), x1))]
            out += [Cut(x0, x1, g.word, g.reason, g.context, -1, words_idx=[]) for x0, x1 in pieces if x1 - x0 >= 0.05]
    return sorted(out, key=lambda x: x.start), skipped, c


UNHEARD = "(전사 누락)"


def detect_untranscribed(tr: dict, rms: list[float], hop: float, filler_cuts: list[Cut],
                         cfg: dict, suspects: dict = None) -> tuple[list[Cut], list[dict]]:
    """Whisper 가 전사하지 않은 목소리 구간. 필러 컷 바로 옆이면 늘인 필러("이제 에~")나 필러 꼬리로 보고 컷,
    그 밖은 진짜 말을 빼먹었을 수도 있어 review.md 에만 표시한다."""
    import numpy as np
    min_len = float(cfg.get("untranscribed_min_sec", 0.2))
    if min_len <= 0:
        return [], []
    suspects = suspects or {}
    max_dur = float(cfg.get("max_filler_duration_sec", 1.3))
    bridge_amp = 10 ** (float(cfg.get("silence_bridge_max_db", -30)) / 20)
    touch, near = 0.05, 0.15  # 단어 경계에 붙음 / 필러 컷과 이웃함 (사이에 짧은 무음 허용)
    frag = 0.35               # 자리가 의심스러운 단어 안에서 필러에 붙은 소리는 이 길이까지만 꼬리로 본다
    r = np.asarray(rms)
    voiced = ~quiet_mask(rms, hop, cfg)  # detect_silences 와 같은 기준이라 무음 컷과 겹치지 않는다
    words = [w for _, _, w in all_words(tr)]
    in_suspect = np.zeros(len(voiced), dtype=bool)
    for seg, i, w in all_words(tr):
        f0, f1 = int(w["start"] / hop), int(np.ceil(w["end"] / hop))
        if (seg, i) in suspects:
            in_suspect[f0:f1] = True  # 엉뚱한 자리에 놓인 단어는 그 구간의 소리를 덮지 못한다
        else:
            voiced[f0:f1] = False     # 단어가 덮는 구간은 전사된 소리
    cut_spans = [(c.start, c.end) for c in filler_cuts]
    cut_idx = {tuple(i) for c in filler_cuts for i in c.words_idx or []}
    kept_bounds = [t for (s, i, w) in all_words(tr) if (s, i) not in cut_idx and (s, i) not in suspects
                   for t in (w["start"], w["end"])]
    word_starts = [w["start"] for w in words]

    cuts: list[Cut] = []
    skipped: list[dict] = []
    for a, b in runs(voiced):
        s, e = a * hop, b * hop
        by_filler = any(-touch <= s - ce <= near or -touch <= cs - e <= near for cs, ce in cut_spans)
        by_kept = any(abs(t - s) <= touch or abs(t - e) <= touch for t in kept_bounds)
        hidden = in_suspect[a:b].any()
        i = bisect.bisect_left(word_starts, s)
        ctx_prev = " ".join(x["word"].strip() for x in words[max(0, i - 3):i])
        ctx_next = " ".join(x["word"].strip() for x in words[i:i + 3])
        context = f"{ctx_prev} [{e-s:.1f}s 전사 안 된 소리] {ctx_next}".strip()
        if by_filler and not by_kept:
            # 필러에 붙은 소리는 짧아도 남기면 '…고' 같은 조각이 들린다
            if e - s >= 0.05 and e - s <= (frag if hidden else max_dur):
                cuts.append(Cut(s, e, UNHEARD, "filler(전사 누락)", context, -1, words_idx=[]))
            continue
        # 남긴 단어에 붙은 소리는 대개 Whisper 단어 시간이 짧게 잡힌 꼬리, 아주 작은 소리는 숨소리라 표시하지 않는다
        if e - s >= min_len and not by_kept and r[a:b].max() >= bridge_amp:
            skipped.append({"start": s, "end": e, "word": UNHEARD,
                            "why": "전사 안 된 목소리 (놓친 필러·웃음·말일 수 있음, 직접 확인)",
                            "context": context})
    return cuts, skipped


def snap_to_quiet(t: float, rms: list[float], hop: float, win: float, direction: int) -> float:
    """컷 경계를 바깥쪽(direction=-1: 앞, +1: 뒤)으로만 가장 조용한 지점까지 옮긴다.
    안쪽으로 옮기면 자음과 모음 사이의 짧은 골짜기에 걸려 필러 소리 일부가 남는다."""
    it = min(len(rms) - 1, max(0, int(t / hop)))
    if direction < 0:
        i0, i1 = max(0, int((t - win) / hop)), it
    else:
        i0, i1 = it, min(len(rms) - 1, int((t + win) / hop))
    if i1 <= i0:
        return t
    j = min(range(i0, i1 + 1), key=lambda k: rms[k])
    # 원래 지점보다 확실히 조용한 곳이 있을 때만 옮긴다 (균일한 소리에서 불필요하게 밀리는 것 방지)
    if rms[j] < 0.6 * rms[it]:
        return j * hop + hop / 2
    return t


def merge_cuts(cuts: list[Cut], fps: Fraction, min_keep: int) -> list[Cut]:
    f = float(fps)
    cuts = sorted(cuts, key=lambda c: c.start_frame)
    merged: list[Cut] = []
    for c in cuts:
        if merged and c.start_frame <= merged[-1].end_frame + min_keep:
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


def refine_cuts(cuts: list[Cut], rms: list[float], hop: float, fps: Fraction,
                total_frames: int, cfg: dict) -> list[Cut]:
    win = float(cfg.get("snap_window_sec", 0.12))
    min_keep = int(cfg.get("min_keep_frames", 3))
    f = float(fps)

    refined: list[Cut] = []
    for c in cuts:
        if c.reason in ("silence", "gap"):  # 이미 무음 안쪽 경계 — 옮기면 남겨 둔 여유가 줄어든다
            s, e = c.start, c.end
        else:
            s = snap_to_quiet(c.start, rms, hop, win, -1)
            e = snap_to_quiet(c.end, rms, hop, win, +1)
        sf = max(0, int(round(s * f)))
        ef = min(total_frames, int(round(e * f)))
        if ef <= sf:
            ef = min(total_frames, sf + 1)
        if ef <= sf:
            continue
        refined.append(Cut(sf / f, ef / f, c.word, c.reason, c.context, c.seg, sf, ef,
                           list(c.words_idx or [])))
    return merge_cuts(refined, fps, min_keep)


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
# 4) SRT
# ---------------------------------------------------------------------------
class TimeRemap:
    """원본 시간 → 컷 적용 후 시간"""
    def __init__(self, cuts: list[Cut]):
        self.starts = [c.start for c in cuts]
        self.ends = [c.end for c in cuts]
        # 자막에서 빼는 단어는 필러로 판정된 단어뿐이다. 시간으로 겹침을 따지면
        # Whisper 단어 시간이 무음 컷 쪽으로 번진 진짜 단어까지 지워진다.
        self.cut_words = {(s, i) for c in cuts for s, i in (c.words_idx or [])}
        self.removed_before = []
        acc = 0.0
        for c in cuts:
            self.removed_before.append(acc)
            acc += c.end - c.start

    def __call__(self, t: float) -> float:
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return t
        if t < self.ends[i]:          # 잘려나간 구간 안 → 그 구간의 시작점으로
            return self.starts[i] - self.removed_before[i]
        return t - self.removed_before[i] - (self.ends[i] - self.starts[i])


def build_captions(tr: dict, cut_words: set, remap: TimeRemap, always_fillers: set[str] = frozenset(),
                   single_only: set[str] = frozenset(), word_edits: dict[str, str] | None = None,
                   max_chars: int = 28, max_dur: float = 5.0, gap_break: float = 0.7) -> list[dict]:
    """자막 줄 계산. 줄 나눔은 원래 Whisper 글자로 하고(고쳐도 줄이 다시 나뉘지 않게),
    고친 글자는 words[].text 에만 반영한다. 시간(start/end)은 원본 영상 기준 초."""
    word_edits = word_edits or {}
    groups: list[list[dict]] = []
    cur: list[dict] = []
    cur_r: list[tuple[float, float, str]] = []   # (결과 영상 기준 시작, 끝, 원래 글자)

    def flush():
        if cur:
            groups.append(list(cur))
        cur.clear()
        cur_r.clear()

    words = all_words(tr)
    norms = [normalize(w["word"]) for _, _, w in words]
    for k, (seg_id, wi, w) in enumerate(words):
        if (seg_id, wi) in cut_words:
            continue  # 잘려나간 필러
        if norms[k] in always_fillers and not is_repeated_sound(words, norms, k, single_only):
            continue  # 오디오엔 남았더라도(건너뛴 항목) 자막에는 '어/음'을 넣지 않는다
        s, e, orig = remap(w["start"]), remap(w["end"]), w["word"].strip()
        if cur_r:
            # 멈춤·길이는 결과 영상 기준으로 잰다 — 무음 컷으로 사라진 멈춤에서 끊으면 한 단어 자막이 생긴다
            length = sum(len(x[2]) + 1 for x in cur_r) + len(orig)
            if (s - cur_r[-1][1] > gap_break or length > max_chars or e - cur_r[0][0] > max_dur
                    or cur_r[-1][2].endswith((".", "?", "!"))):
                flush()
        key = f"{seg_id}:{wi}"
        cur_r.append((s, e, orig))
        cur.append({"key": key, "text": word_edits.get(key, orig).strip(), "orig": orig,
                    "start": w["start"], "end": w["end"]})
    flush()

    caps = []
    for g in groups:
        ws = [x for x in g if x["text"]]   # 글자를 비운 어절은 빠진다 (줄 key 는 원래 첫 어절이라 안 바뀐다)
        if not ws or not " ".join(x["text"] for x in ws).strip():
            continue
        caps.append({"key": g[0]["key"], "start": ws[0]["start"], "end": ws[-1]["end"], "words": ws})
    return caps


def captions_to_srt(caps: list[dict], remap: TimeRemap, final_dur: float,
                    line_times: dict[str, list[float]] | None = None) -> str:
    """줄 → SRT. line_times[key] = [시작, 끝](원본초)인 줄은 그 시간을 쓰고 뒷처리에서 움직이지 않는다."""
    line_times = line_times or {}
    # 자막끼리 겹치지 않게, 영상 길이를 넘지 않게
    out: list[list] = []   # [시작, 끝, 글자, 고정 여부]
    for c in caps:
        text = " ".join(w["text"] for w in c["words"]).strip()
        fixed = c["key"] in line_times
        if fixed:
            s, e = remap(line_times[c["key"]][0]), remap(line_times[c["key"]][1])
        else:
            s = remap(c["start"])
            e = max(remap(c["end"]), s + 0.4)
        if out:
            prev = out[-1]
            if not prev[3] and prev[1] > s - 0.05:
                prev[1] = max(prev[0] + 0.3, s - 0.05)
            if not fixed:
                s = max(s, prev[1] + 0.05)
        if not fixed:
            e = max(e, s + 0.3)
        out.append([s, e, text, fixed])
    lines = []
    for i, (s, e, text, fixed) in enumerate(out):
        if not fixed:
            e = min(e, final_dur)
            s = min(s, max(0.0, e - 0.3))
        lines.append(f"{i+1}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{text}\n")
    return "\n".join(lines) + ("\n" if lines else "")


def load_subs_edits(path: Path | None, tr: dict) -> tuple[dict, dict]:
    """앱이 넘긴 자막 수정 파일 → (어절 글자, 줄 시간). 받아쓰기가 달라졌으면 버린다."""
    if not path:
        return {}, {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        log("자막 수정 파일을 읽지 못해 무시합니다")
        return {}, {}
    if data.get("transcript_sha1") != transcript_sha1(tr):
        log("자막 수정이 지금 받아쓰기와 맞지 않아 무시합니다")
        return {}, {}
    raw_w, raw_l = data.get("words"), data.get("lines")
    words = {k: v for k, v in raw_w.items() if isinstance(v, str)} if isinstance(raw_w, dict) else {}
    lines = {}
    if isinstance(raw_l, dict):
        for k, v in raw_l.items():
            if (isinstance(v, (list, tuple)) and len(v) == 2
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
                lines[k] = [float(v[0]), float(v[1])]
    return words, lines


def build_srt(tr: dict, remap: TimeRemap, final_dur: float, always_fillers: set[str] = frozenset(),
              single_only: set[str] = frozenset(),
              max_chars: int = 28, max_dur: float = 5.0, gap_break: float = 0.7) -> str:
    caps = build_captions(tr, remap.cut_words, remap, always_fillers, single_only,
                          max_chars=max_chars, max_dur=max_dur, gap_break=gap_break)
    return captions_to_srt(caps, remap, final_dur)


# ---------------------------------------------------------------------------
# 5) 컷 적용
# ---------------------------------------------------------------------------
def cut_audio(clean_wav: Path, out_wav: Path, keeps: list[tuple[int, int]], fps: Fraction,
              fade_ms: float = 3.0) -> tuple[int, int]:
    """구간 단위로 읽고 써서 메모리를 아낀다 (1시간 스테레오 전체 로드 ≈ 4GB).
    오디오가 영상보다 짧으면 0으로 채워 영상 프레임 수와 정확히 맞춘다."""
    import numpy as np
    total = 0
    with wave.open(str(clean_wav), "rb") as wf, wave.open(str(out_wav), "wb") as wo:
        sr, ch, n = wf.getframerate(), wf.getnchannels(), wf.getnframes()
        wo.setnchannels(ch); wo.setsampwidth(2); wo.setframerate(sr)
        fade = int(sr * fade_ms / 1000)
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None] if fade > 0 else None
        for sf, ef in keeps:
            a = int(round(Fraction(sf) / fps * sr))
            b = int(round(Fraction(ef) / fps * sr))
            if b <= a:
                continue
            piece = np.zeros((b - a, ch), dtype=np.float32)
            if a < n:
                wf.setpos(a)
                got = np.frombuffer(wf.readframes(min(b, n) - a), dtype=np.int16).reshape(-1, ch)
                piece[:len(got)] = got
            if ramp is not None and len(piece) > 2 * fade:
                piece[:fade] *= ramp
                piece[-fade:] *= ramp[::-1]
            wo.writeframes(np.clip(piece, -32768, 32767).astype(np.int16).tobytes())
            total += b - a
    return total, sr


def sum_tree(terms: list[str]) -> str:
    """ffmpeg 식 파서는 평평한 a+b+c… 를 100항까지만 받는다 → 괄호로 묶어 깊이를 log2(n)로"""
    if len(terms) == 1:
        return terms[0]
    mid = len(terms) // 2
    return f"({sum_tree(terms[:mid])}+{sum_tree(terms[mid:])})"


def auto_bitrate(info: dict, scale: float = 1.0) -> str:
    # 유튜브 권장치 기준: 1080p30 8M, 1080p60 12M, 4K30 ≈32M, 4K60 ≈48M. scale: 앱의 화질 단계(--vbitrate-scale)
    bps = 8e6 * (info["width"] * info["height"]) / (1920 * 1080) * scale
    if float(info["fps"]) > 31:
        bps *= 1.5
    if info["high_bit"]:
        bps *= 1.25
    return f"{int(min(max(bps, 4e6), 150e6) / 1e6)}M"


def video_encoder_args(info: dict, hw: bool, encoders: str, crf: int, vbitrate: str,
                       preset: str, vbitrate_scale: float = 1.0) -> tuple[str, list]:
    def has(name: str) -> bool:
        return re.search(rf"\s{name}\s", encoders) is not None

    bitrate = auto_bitrate(info, vbitrate_scale) if vbitrate == "auto" else vbitrate
    if vbitrate == "auto" and vbitrate_scale > 0 and vbitrate_scale != 1.0:
        # 소프트웨어 인코더는 비트레이트 대신 CRF 를 쓴다 → 배율을 CRF 차이로 (CRF 6 ≈ 비트레이트 2배)
        crf -= round(6 * math.log2(vbitrate_scale))
    color = []
    for opt, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                     ("-colorspace", "color_space")):
        if info["color"].get(key) and info["color"][key] != "unknown":
            color += [opt, info["color"][key]]
    if info["high_bit"]:
        # 10비트(아이폰 HDR 등)를 8비트 H.264 로 줄이면 계조가 뭉개진다 → HEVC 10비트로 색 정보 유지
        if hw and has("hevc_videotoolbox"):
            return "hevc_videotoolbox", ["-c:v", "hevc_videotoolbox", "-profile:v", "main10",
                                         "-pix_fmt", "p010le", "-b:v", bitrate, "-allow_sw", "1",
                                         "-tag:v", "hvc1", *color]
        if has("libx265"):
            return "libx265", ["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", preset,
                               "-crf", str(crf), "-tag:v", "hvc1", *color]
        log("경고: HEVC 인코더가 없어 10비트 원본을 8비트 H.264 로 인코딩합니다")
    if hw and has("h264_videotoolbox"):
        # -allow_sw: 하드웨어 세션이 거부되는 해상도(5K 이상 등)에서도 VideoToolbox 소프트웨어 경로로 인코딩
        return "h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p",
                                     "-b:v", bitrate, "-allow_sw", "1"]
    if hw:
        log("경고: h264_videotoolbox 를 쓸 수 없어 libx264 로 인코딩합니다")
    return "libx264", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", preset, "-crf", str(crf)]


def render_video(src: Path, cut_wav: Path, srt: Path | None, out_mp4: Path,
                 keeps: list[tuple[int, int]], info: dict, work: Path, enc_args: list) -> None:
    fps = info["fps"]
    f = float(fps)
    terms = [f"between(t,{(sf-0.5)/f:.6f},{(ef-0.5)/f:.6f})" for sf, ef in keeps]
    # 홀수 해상도는 4:2:0 인코더가 거부(libx264)하거나 조용히 잘라낸다(VideoToolbox) → 짝수로 1px 크롭
    crop = ",crop=trunc(iw/2)*2:trunc(ih/2)*2" if info["width"] % 2 or info["height"] % 2 else ""
    script = work / "video_filter.txt"
    script.write_text(
        f"[0:v]setpts=PTS-STARTPTS,fps={fps.numerator}/{fps.denominator},"
        f"select='{sum_tree(terms)}',setpts=N/FRAME_RATE/TB{crop}[v]",
        encoding="utf-8")
    # ffmpeg 7 부터 파일에서 옵션 값을 읽는 문법이 -/옵션 이고, 9.0 에서 -filter_complex_script 가 제거됐다
    fc = ["-/filter_complex", script] if ffmpeg_major() >= 7 else ["-filter_complex_script", script]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
           "-i", src, "-i", cut_wav]
    if srt:
        cmd += ["-i", srt]
    cmd += [*fc, "-map", "[v]", "-map", "1:a"]
    if srt:
        # 끄고 켤 수 있는 자막 트랙(번인 아님). 렌더하면서 같이 넣어 파일을 한 번 더 쓰지 않는다
        cmd += ["-map", "2:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=kor"]
    # 원본 챕터는 컷 전 시간이라 맞지 않는다
    cmd += ["-map_chapters", "-1", *enc_args]
    # -shortest 는 마지막 프레임을 떨어뜨리므로 쓰지 않는다 (오디오/영상 길이는 이미 프레임 격자로 맞춤)
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_mp4]
    log("렌더링 시작 (영상 길이에 따라 몇 분 걸립니다)")
    ffmpeg_with_progress(cmd, sum(ef - sf for sf, ef in keeps) / f, "render")


def make_preview(src: Path, clean_wav: Path, audio_key: dict, info: dict, out: Path, work: Path,
                 hw: bool, force: bool) -> None:
    """앱 플레이어용 미리보기 사본. 컷을 적용하지 않은 원본 타임라인 그대로라 앱이 컷 구간을 직접 건너뛴다.
    원본·clean.wav 가 같으면 다시 만들지 않는다 (옵션을 바꿔 다시 판정할 때 몇 초 안에 끝나야 함)."""
    meta = work / "preview.meta.json"
    key = {"v": 1, "src": source_sig(src), "audio": audio_key, "out": str(out)}
    if cached_meta(meta, key, [out], force):
        log(f"미리보기 사본 재사용: {out}")
        progress("preview", 1.0)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    meta.unlink(missing_ok=True)
    tmp = out.with_name(out.stem + ".part" + out.suffix)  # 중간에 끊긴 파일이 완성본으로 보이지 않게
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    audio = ["-c:a", "aac", "-b:a", "128k", "-map_chapters", "-1", "-movflags", "+faststart"]
    log(f"미리보기 사본 만들기: {out}")
    if not info["has_video"]:
        ffmpeg_with_progress([*base, "-i", clean_wav, "-map", "0:a", *audio, tmp], info["duration"], "preview")
    else:
        # 긴 변 1280 이하, 짝수 크기. 회전 메타데이터는 ffmpeg 가 먼저 적용하므로 식으로 계산한다
        vf = ("setpts=PTS-STARTPTS,"
              "scale=w='if(gte(iw,ih),trunc(min(1280,iw)/2)*2,-2)':h='if(gte(iw,ih),-2,trunc(min(1280,ih)/2)*2)'")
        encoders = run(["ffmpeg", "-hide_banner", "-encoders"], quiet=True).stdout
        choices = []
        if hw and re.search(r"\sh264_videotoolbox\s", encoders):
            choices.append(["-c:v", "h264_videotoolbox", "-b:v", "3M", "-allow_sw", "1"])
        choices.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"])
        for i, venc in enumerate(choices):
            cmd = [*base, "-i", src, "-i", clean_wav, "-map", "0:v:0", "-map", "1:a",
                   "-vf", vf, *venc, "-pix_fmt", "yuv420p", *audio, tmp]
            try:
                ffmpeg_with_progress(cmd, info["duration"], "preview")
                break
            except subprocess.CalledProcessError:
                if i == len(choices) - 1:
                    tmp.unlink(missing_ok=True)
                    raise
                log("미리보기 하드웨어 인코딩 실패 → libx264 로 다시 만듭니다")
    os.replace(tmp, out)
    meta.write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")


def try_preview(src: Path, clean_wav: Path, audio_key: dict, info: dict, out: Path, work: Path,
                hw: bool, force: bool) -> bool:
    """--preview 용. 미리보기는 앱 재생기용 덤이라, 못 만들어도 컷 리스트·전사는 쓸 수 있으니 분석을 실패로
    만들지 않는다. 실패하면 (옛 것과 섞이지 않게) 미리보기 파일을 지우고 False."""
    try:
        make_preview(src, clean_wav, audio_key, info, out, work, hw, force)
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"미리보기를 만들지 못했습니다 (분석 결과는 그대로 씀): {e}")
        for f in (out, out.with_name(out.stem + ".part" + out.suffix)):
            f.unlink(missing_ok=True)
        progress("preview", 1.0)
        return False


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


def render_audio_note(args: argparse.Namespace) -> str:
    d = getattr(args, "render_denoise", "same")
    l = getattr(args, "render_loudnorm", "same")
    if d == "same" and l == "same":
        return ""
    loud = (not args.no_loudnorm) if l == "same" else l == "on"
    return f" (결과 영상 오디오: 노이즈 제거 {args.denoise if d == 'same' else d}, 라우드니스 {'켬' if loud else '끔'})"


def write_report(path: Path, src: Path, info: dict, cuts: list[Cut], skipped: list[dict],
                 final_dur: float, modes: dict, args: argparse.Namespace, encoder: str,
                 elapsed: float) -> None:
    from collections import Counter
    by_word = Counter(normalize(w) for c in cuts for w in c.word.split(" + ") if w not in ("(무음)", UNHEARD))
    removed = sum(c.end - c.start for c in cuts)
    n_silence = sum(1 for c in cuts if "silence" in c.reason or "gap" in c.reason)
    n_unheard = sum(1 for c in cuts if UNHEARD in c.word)
    out = [f"# {src.name} 처리 리포트", "",
           f"- 원본 길이: {fmt_clock(info['duration'])} → 결과: {fmt_clock(final_dur)} "
           f"(-{removed:.1f}s, {len(cuts)}곳 컷)",
           f"- 노이즈 제거: {args.denoise}, 라우드니스 정규화: {'켬' if not args.no_loudnorm else '끔'}"
           + render_audio_note(args),
           f"- 전사 모델: {args.model}, 아/어/음 컷: {modes['filler']}, 습관어 컷: {modes['habit']}, "
           f"무음 컷: {modes['silence']}, 인코더: {encoder}",
           f"- 무음이 포함된 컷: {n_silence}곳",
           f"- 전사 안 된 소리(필러 옆)가 포함된 컷: {n_unheard}곳",
           f"- 건너뛴 감지: {len(skipped)}건 (review.md 참고)",
           f"- 소요 시간: {elapsed/60:.1f}분", "",
           "## 잘라낸 단어별 횟수", ""]
    if info.get("vfr"):
        out.insert(3, f"- 가변 프레임 원본 → {float(info['fps']):.3f}fps 고정으로 출력")
    for w, n in by_word.most_common():
        out.append(f"- {w}: {n}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def cuts_from_dicts(items: list[dict], fps: Fraction, total_frames: int) -> list[Cut]:
    """cuts.json 의 컷 목록 → 프레임 격자에 맞춘 Cut (길이 0 은 버리고 겹치는 컷은 합침)"""
    f = float(fps)
    cuts = []
    for c in items:
        cut = Cut(**{k: v for k, v in c.items() if k in Cut.__dataclass_fields__})
        # 사람이 고치는 건 초 값이다 → 프레임을 초에서 다시 계산해 렌더와 SRT 가 같은 값을 쓰게 한다
        cut.start_frame = max(0, int(round(cut.start * f)))
        cut.end_frame = min(total_frames, int(round(cut.end * f)))
        if cut.end_frame <= cut.start_frame:
            continue
        cut.start, cut.end = cut.start_frame / f, cut.end_frame / f
        cuts.append(cut)
    return merge_cuts(cuts, fps, 0)


def load_cut_list(path: Path, tr: dict, fps: Fraction,
                  total_frames: int) -> tuple[list[Cut], list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("transcript_sha1") and data["transcript_sha1"] != transcript_sha1(tr):
        sys.exit("오류: 전사가 컷 리스트를 만들 때와 달라 단어 위치가 맞지 않습니다. "
                 "--apply-cuts 없이 다시 실행해 컷 리스트를 새로 만드세요.")
    cuts = cuts_from_dicts(data["cuts"], fps, total_frames)
    modes = {k: data.get(k, "?") for k in ("filler", "habit", "silence")}
    return cuts, data.get("skipped", []), modes


def level_0_100(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        n = -1
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(f"0~100 사이 정수여야 합니다: {v}")
    return n


def build_parser() -> argparse.ArgumentParser:
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
    ap.add_argument("--silence-level", type=level_0_100, default=None,
                    help="말 없는 구간 자르기 세기 0~100 (데스크톱 앱용, 주면 --silence 대신). "
                         "0 끔, 50 = --silence on, 100 = 단어 없는 틈은 소리가 있어도 전부")
    ap.add_argument("--hw", choices=["on", "off"], default="on",
                    help="맥 하드웨어 인코딩 VideoToolbox (기본 on, off 면 libx264/libx265)")
    ap.add_argument("--review", action="store_true", help="컷 리스트/검토표까지만 만들고 렌더링은 하지 않음")
    ap.add_argument("--apply-cuts", type=Path, help="기존 .cuts.json 으로 렌더 (전사·판정 생략)")
    ap.add_argument("--embed-subs", choices=["on", "off"], default="on",
                    help="mp4 에 끄고 켤 수 있는 자막 트랙 넣기 (기본 on, 번인 아님)")
    ap.add_argument("--crf", type=int, default=18, help="--hw off 일 때 화질")
    ap.add_argument("--vbitrate", default="auto",
                    help="--hw on 일 때 비트레이트 (기본 auto: 해상도·프레임 수에 맞춤, 예: 20M)")
    ap.add_argument("--preset", default="medium", help="--hw off 일 때 인코딩 속도")
    ap.add_argument("--force", action="store_true", help="work/ 캐시 무시하고 전부 다시")
    ap.add_argument("--work-root", type=Path, default=ROOT / "work", help="작업 캐시 폴더 (기본 work/)")
    ap.add_argument("--media-out", type=Path, default=None,
                    help="_clean.mp4/.m4a 와 _clean.srt 를 둘 폴더 (기본 --out). 나머지 산출물은 --out")
    ap.add_argument("--ctc-model", default=CTC_MODEL, help="필러 시간 보정용 정렬 모델 (허브 ID 또는 로컬 폴더)")
    ap.add_argument("--progress-json", action="store_true",
                    help='stdout 에 @progress {"stage": …, "frac": 0~1} 줄을 함께 냄 (데스크톱 앱용)')
    ap.add_argument("--render-denoise", choices=["same", "rnnoise", "afftdn", "none"], default="same",
                    help="결과 영상 오디오의 노이즈 제거 (기본 same: --denoise 와 같음). 분석은 --denoise 오디오로")
    ap.add_argument("--render-loudnorm", choices=["same", "on", "off"], default="same",
                    help="결과 영상 오디오의 라우드니스 정규화 (기본 same: --no-loudnorm 를 따름)")
    ap.add_argument("--preview", type=Path, default=None,
                    help="판정 뒤 컷 적용 전 원본 타임라인의 미리보기 사본(긴 변 1280, H.264)을 이 경로에 만듦")
    ap.add_argument("--vbitrate-scale", type=float, default=1.0,
                    help="--vbitrate auto 일 때 자동 비트레이트에 곱할 배율 (기본 1.0)")
    ap.add_argument("--subs-edits", type=Path, default=None,
                    help='자막 수정 파일 {"transcript_sha1","words","lines"} (데스크톱 앱용). 받아쓰기가 다르면 무시')
    return ap


def main() -> None:
    global PROGRESS
    args = build_parser().parse_args()
    PROGRESS = args.progress_json
    if args.review and args.apply_cuts:
        sys.exit("오류: --review 와 --apply-cuts 는 함께 쓸 수 없습니다 (--apply-cuts 는 바로 렌더합니다)")

    need("ffmpeg"); need("ffprobe")
    src = args.input.resolve()
    if not src.exists():
        sys.exit(f"오류: 입력 파일이 없습니다: {src}")
    t_start = time.time()

    stem = src.stem
    out_dir = args.out.resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    work = args.work_root.resolve() / stem; work.mkdir(parents=True, exist_ok=True)
    with RunLock(work / "run.lock"):
        process(args, src, stem, out_dir, work, t_start)


def process(args: argparse.Namespace, src: Path, stem: str, out_dir: Path, work: Path,
            t_start: float) -> None:
    cfg = load_fillers(args.fillers)
    info = ffprobe(src)
    fps = info["fps"]
    log(f"입력: {src.name}  {fmt_clock(info['duration'])}  {info['width']}x{info['height']}  "
        f"{float(fps):.3f}fps{' (가변)' if info['vfr'] else ''}  "
        f"{info['sample_rate']}Hz/{info['channels']}ch  {info['pix_fmt']}")

    # 1) 오디오
    clean_wav, asr_wav, audio_key = prepare_audio(src, work, info, args.denoise,
                                                  not args.no_loudnorm, args.force)
    if info["duration"] <= 0:  # 길이 정보가 없는 컨테이너(일부 webm 등)
        info["duration"] = wav_duration(clean_wav)
    total_frames = int(round(info["duration"] * float(fps)))

    # 2) 전사
    tr_path = out_dir / f"{stem}.transcript.json"
    tr = transcribe(asr_wav, tr_path, audio_key, args.model, args.device, args.compute_type,
                    cfg.get("prompt", ""), args.force, args.work_root / ".whisper_probe.json")
    rms, hop = load_rms(asr_wav)
    sil_base = dict(cfg)  # --silence-level 은 자동 무음 기준 전 설정에서 무음 판정만 따로 한다 (정렬·필러 판정은 그대로)
    if not args.apply_cuts:
        base_db = float(cfg.get("silence_threshold_db", -40))
        cfg.update(auto_silence(tr, rms, hop, cfg))
        if cfg["silence_threshold_db"] != base_db:
            log(f"무음 기준: 배경 소리 {cfg['silence_noise_floor_db']:.1f}dB → {cfg['silence_threshold_db']:.1f}dB "
                f"(기본 {base_db:.0f}dB)")
    # 필러 시간 보정은 사전(단어 목록)에 따라 대상이 달라진다 → 사전이 바뀌면 원래 시간으로 되돌려 다시 한다
    align_key = {"v": ALIGN_VERSION, "words": sorted(cfg["_always"] | cfg["_habit"] | cfg["_pause_after"]),
                 "never": sorted(cfg["_never"]), "silence": [cfg.get("silence_threshold_db", -40),
                                                            cfg.get("silence_min_sec", 0.3)],
                 "ctc": all(importlib.util.find_spec(m) for m in ("torch", "transformers"))}  # 나중에 설치하면 다시 보정
    if not args.apply_cuts and tr.get("align") != align_key:
        for _, _, w in all_words(tr):
            if "whisper" in w:
                w["start"], w["end"] = w.pop("whisper")
        n, align_failed = ctc_fix_fillers(tr, asr_wav, rms, hop, cfg, args.ctc_model)
        if n or align_failed:
            log(f"필러 시간 보정: {n}곳 (실패 {len(align_failed)}곳)")
        tr["align"], tr["align_failed"] = align_key, align_failed
        tr_path.write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
    progress("align", 1.0)

    # 3) 컷 리스트
    cuts_path = out_dir / f"{stem}.cuts.json"
    if args.apply_cuts:
        cuts, skipped, modes = load_cut_list(args.apply_cuts, tr, fps, total_frames)
        log(f"컷 리스트 불러옴: {args.apply_cuts.name} ({len(cuts)}곳)")
    else:
        level = args.silence_level
        modes = {"filler": args.filler, "habit": args.habit,
                 "silence": args.silence if level is None else f"level:{level}"}
        habit_mode = {"on": "conservative", "off": "off", "aggressive": "aggressive"}[args.habit]
        raw_cuts, skipped = detect_fillers(tr, cfg, habit_mode, args.filler == "on")
        suspects = find_suspect_words(tr, quiet_mask(rms, hop, cfg), hop, cfg)
        skipped += tr.get("align_failed", [])
        aw = all_words(tr)
        for k, (seg, wi, w) in enumerate(aw):
            if (seg, wi) in suspects:
                ctx = " ".join(x[2]["word"].strip() for x in aw[max(0, k - 3):k]) + f" [{w['word'].strip()}] " \
                    + " ".join(x[2]["word"].strip() for x in aw[k + 1:k + 4])
                skipped.append({"start": w["start"], "end": w["end"], "word": w["word"].strip(),
                                "why": f"단어 시간 의심: 단어 안에 {suspects[(seg, wi)]:.1f}s 무음 "
                                       "(실제 위치와 어긋났을 수 있음, 주변 컷·자막 확인)", "context": ctx.strip()})
        un_cuts, un_skipped = detect_untranscribed(tr, rms, hop, raw_cuts, cfg, suspects)
        raw_cuts += un_cuts
        skipped += un_skipped
        if un_cuts or un_skipped:
            log(f"전사 안 된 목소리: 필러 옆이라 컷 {len(un_cuts)}곳, 확인 필요 {len(un_skipped)}곳")
        n_fillers = len(raw_cuts)
        if level is not None and level > 0:
            sil_cuts, sil_skipped, sil_cfg = silence_cuts_for_level(tr, rms, hop, info["duration"], sil_base,
                                                                    level, suspects)
            log(f"무음 세기 {level}: 기준 {sil_cfg['silence_threshold_db']:.1f}dB, "
                f"{sil_cfg['silence_min_sec']:.2f}초 이상{', 단어 없는 틈 전부' if level >= 100 else ''}")
        elif level is None and args.silence == "on":
            sil_cuts, sil_skipped = detect_silences(tr, rms, hop, info["duration"], cfg, suspects)
        else:
            sil_cuts = None
        if sil_cuts is not None:
            raw_cuts += sil_cuts
            skipped += sil_skipped
            log(f"무음 감지: {len(sil_cuts)}곳, {sum(c.end - c.start for c in sil_cuts):.1f}s "
                f"(건너뜀 {len(sil_skipped)}건)")
        cuts = refine_cuts(raw_cuts, rms, hop, fps, total_frames, cfg)
        cuts_path.write_text(json.dumps(
            {"source": str(src), "fps": f"{fps.numerator}/{fps.denominator}", **modes,
             "transcript_sha1": transcript_sha1(tr),
             "cuts": [asdict(c) for c in cuts], "skipped": skipped},
            ensure_ascii=False, indent=1), encoding="utf-8")
        write_review(cuts, skipped, out_dir / f"{stem}.review.md")
        removed = sum(c.end - c.start for c in cuts)
        log(f"필러 {n_fillers}개 + 무음 {len(raw_cuts) - n_fillers}곳 → 병합 후 {len(cuts)}곳, "
            f"총 {removed:.1f}s 제거 예정 (건너뜀 {len(skipped)}건)")
    progress("judge", 1.0)
    if args.preview:
        try_preview(src, clean_wav, audio_key, info, args.preview.resolve(), work, args.hw == "on", args.force)
    if args.review:
        log(f"--review: 검토표 {out_dir / (stem + '.review.md')} 를 확인한 뒤 "
            f"--apply-cuts {cuts_path} 로 렌더하세요")
        return

    keeps = keep_segments(cuts, total_frames)
    if not keeps:
        sys.exit("오류: 남길 구간이 없습니다 (전부 컷 대상). --silence off 등으로 다시 실행해 보세요.")

    # 4) SRT — 영상과 같은 이름이어야 윈도우 기본 플레이어 등이 자동으로 불러온다
    media_dir = (args.media_out or out_dir).resolve(); media_dir.mkdir(parents=True, exist_ok=True)
    out_media = media_dir / (f"{stem}_clean.mp4" if info["has_video"] else f"{stem}_clean.m4a")
    final_dur = sum(ef - sf for sf, ef in keeps) / float(fps)
    srt_path = out_media.with_suffix(".srt")
    sub_words, sub_lines = load_subs_edits(args.subs_edits, tr)
    remap_ = TimeRemap(cuts)
    caps = build_captions(tr, remap_.cut_words, remap_, cfg["_always"], cfg["_single_only"], word_edits=sub_words)
    srt_text = captions_to_srt(caps, remap_, final_dur, line_times=sub_lines)
    srt_path.write_text(srt_text, encoding="utf-8")
    if not srt_text:
        log("인식된 말소리가 없어 자막이 비어 있습니다 (자막 트랙은 넣지 않음)")

    # 5) 렌더
    render_wav = clean_wav
    r_denoise = args.denoise if args.render_denoise == "same" else args.render_denoise
    r_loud = (not args.no_loudnorm) if args.render_loudnorm == "same" else args.render_loudnorm == "on"
    if (r_denoise, r_loud) != (args.denoise, not args.no_loudnorm):
        render_wav = prepare_render_audio(src, work, info, r_denoise, r_loud, args.force)
    cut_wav = work / "cut.wav"
    n_samples, sr = cut_audio(render_wav, cut_wav, keeps, fps)
    final_dur = n_samples / sr
    encoder = "aac"
    if info["has_video"]:
        embed = srt_path if (args.embed_subs == "on" and srt_text) else None
        encoders = run(["ffmpeg", "-hide_banner", "-encoders"], quiet=True).stdout
        encoder, enc_args = video_encoder_args(info, args.hw == "on", encoders, args.crf,
                                               args.vbitrate, args.preset, args.vbitrate_scale)
        try:
            render_video(src, cut_wav, embed, out_media, keeps, info, work, enc_args)
        except subprocess.CalledProcessError:
            if "videotoolbox" not in encoder:
                raise
            log(f"{encoder} 인코딩 실패 → 소프트웨어 인코더로 다시 렌더합니다")
            encoder, enc_args = video_encoder_args(info, False, encoders, args.crf,
                                                   args.vbitrate, args.preset, args.vbitrate_scale)
            render_video(src, cut_wav, embed, out_media, keeps, info, work, enc_args)
        if embed:
            log("자막 트랙을 영상에 넣음 (번인 아님, 플레이어에서 켜고 끄기)")
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", cut_wav,
               "-c:a", "aac", "-b:a", "192k", out_media]
        if PROGRESS:
            ffmpeg_with_progress(cmd, final_dur, "render")
        else:
            run(cmd)

    write_report(out_dir / f"{stem}.report.md", src, info, cuts, skipped, final_dur, modes, args,
                 encoder, time.time() - t_start)
    log("완료")
    log(f"  영상: {out_media}")
    log(f"  자막: {srt_path}")
    log(f"  리포트: {out_dir / (stem + '.report.md')}")


if __name__ == "__main__":
    main()
