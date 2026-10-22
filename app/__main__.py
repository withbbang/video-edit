"""진입점. `python -m app` (개발) 과 묶은 실행 파일이 같이 쓴다.

  --pipeline ...   scripts/pipeline.py 를 이 프로세스에서 실행 (묶은 앱이 파이프라인 하위 프로세스로 쓴다)
  --probe-gpu      윈도우 GPU 시험 (models.run_gpu_probe)
  -c CODE          파이썬 코드 실행. 묶은 앱에선 sys.executable 이 이 실행 파일이라
                   pipeline.probe_whisper_windows 의 [sys.executable, "-c", ...] 가 여기로 온다
  그 외             창을 띄운다
"""
from __future__ import annotations

import os
import sys


def _use_bundled_ffmpeg() -> None:
    """묶은 앱 안의 ffmpeg/ffprobe 를 PATH 맨 앞에 둔다 (개발 실행에선 아무것도 안 함).
    runner.build_env 도 붙이지만, 파이프라인 모드를 직접 실행해도 시스템 ffmpeg 없이 돌게 한다."""
    from . import paths
    d = paths.ffmpeg_bin_dir()
    if d is None:
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(d) not in parts:
        os.environ["PATH"] = os.pathsep.join([str(d)] + [p for p in parts if p])


def main(argv: list[str] | None = None):
    argv = list(sys.argv if argv is None else argv)
    rest = argv[1:]
    from . import paths

    if rest[:1] == ["--pipeline"]:
        _use_bundled_ffmpeg()
        if getattr(sys, "frozen", False):
            sys.dont_write_bytecode = True  # 앱 폴더(.app 안) 에 __pycache__ 를 쓰지 않는다
        scripts = str(paths.bundle_dir() / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import pipeline
        sys.argv = [argv[0]] + rest[1:]
        return pipeline.main()
    if rest[:1] == ["-c"] and len(rest) >= 2:
        _use_bundled_ffmpeg()
        sys.argv = ["-c"] + rest[2:]
        exec(compile(rest[1], "<string>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
        return None
    if rest[:1] == ["--probe-gpu"]:
        from . import models
        sys.exit(models.run_gpu_probe(paths.data_dir()))
    from . import window
    return window.run()


if __name__ == "__main__":
    main()
