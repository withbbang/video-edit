"""진입점. `python -m app` (개발) 과 묶은 실행 파일이 같이 쓴다.

  --pipeline ...   scripts/pipeline.py 를 이 프로세스에서 실행 (묶은 앱이 파이프라인 하위 프로세스로 쓴다)
  --probe-gpu      윈도우 GPU 시험 (models.run_gpu_probe)
  그 외             창을 띄운다
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None):
    argv = list(sys.argv if argv is None else argv)
    rest = argv[1:]
    from . import paths

    if rest[:1] == ["--pipeline"]:
        scripts = str(paths.bundle_dir() / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import pipeline
        sys.argv = [argv[0]] + rest[1:]
        return pipeline.main()
    if rest[:1] == ["--probe-gpu"]:
        from . import models
        sys.exit(models.run_gpu_probe(paths.data_dir()))
    from . import window
    return window.run()


if __name__ == "__main__":
    main()
