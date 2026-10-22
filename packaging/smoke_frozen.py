"""PyInstaller 로 묶은 실행 파일을 창 없이 확인한다 (CI 릴리스 잡과 로컬 빌드 확인용).

    python packaging/smoke_frozen.py dist/youtube-edit.app/Contents/MacOS/youtube-edit
    python packaging/smoke_frozen.py dist/youtube-edit/youtube-edit.exe

1. `-c "raise ValueError(...)"` → 종료 코드 1 + stderr 에 Traceback
   (pipeline.probe_whisper_windows 가 기대하는 형식. 윈도우 창 없는 실행 파일에서 특히 확인할 것)
2. `--pipeline --help` → 종료 코드 0 + 사용법 (묶인 pipeline.py 와 그 import 가 됨)
3. `-c` 로 묶인 ffmpeg 폴더를 물어 fetch_ffmpeg.verify_by_running 으로 필터·인코더 확인,
   그리고 실행 파일 안에서 PATH 맨 앞의 ffmpeg 가 그 폴더의 것인지 확인
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ff():
    spec = importlib.util.spec_from_file_location("fetch_ffmpeg", HERE / "fetch_ffmpeg.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run(exe: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # runner.build_env 와 같게 (윈도우 cp 코드페이지 대비)
    return subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, env=env)


def _fail(msg: str, r: subprocess.CompletedProcess | None = None) -> None:
    if r is not None:
        msg += f"\n  종료 코드 {r.returncode}\n  stdout: {r.stdout[-1500:]}\n  stderr: {r.stderr[-1500:]}"
    raise SystemExit(f"실패: {msg}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    exe = Path(args[0]).resolve()
    if not exe.is_file():
        _fail(f"{exe} 가 없습니다")
    ff = _ff()
    plat = ff.host_platform()
    if plat is None:
        _fail("맥(arm64)이나 윈도우에서만 확인할 수 있습니다")

    r = _run(exe, "-c", "raise ValueError('smoke-x')")
    if r.returncode != 1 or "Traceback" not in r.stderr or "ValueError: smoke-x" not in r.stderr:
        _fail("-c 예외가 종료 코드 1 + Traceback 으로 나오지 않음", r)
    print("확인: -c 예외 → 종료 코드 1 + Traceback")

    r = _run(exe, "--pipeline", "--help")
    if r.returncode != 0 or "usage" not in r.stdout.lower() or "--apply-cuts" not in r.stdout:
        _fail("--pipeline --help 실패", r)
    print("확인: --pipeline --help")

    r = _run(exe, "-c", "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])")
    if r.returncode != 0 or not r.stdout.strip().isdigit() or int(r.stdout.strip()) == 0:
        _fail("HTTPS 인증서 목록이 비어 있음 (모델·업데이트 받기가 실패함)", r)
    print(f"확인: HTTPS 인증서 {r.stdout.strip()}개")

    code = ("import shutil; from app import paths; "
            "print(paths.ffmpeg_bin_dir()); print(shutil.which('ffmpeg')); print(shutil.which('ffprobe'))")
    r = _run(exe, "-c", code)
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) != 3 or lines[0] == "None":
        _fail("묶인 ffmpeg 폴더를 찾지 못함", r)
    bin_dir = Path(lines[0])
    for found in lines[1:]:
        if found == "None" or Path(found).resolve().parent != bin_dir.resolve():
            _fail(f"PATH 의 ffmpeg/ffprobe 가 묶인 것이 아님: {found} (묶인 폴더 {bin_dir})", r)
    try:
        ff.verify_by_running(bin_dir, plat)
    except ff.FetchError as e:
        _fail(f"묶인 ffmpeg 확인 실패: {e}")
    print(f"확인: 묶인 ffmpeg ({bin_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
