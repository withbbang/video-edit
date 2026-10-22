"""PyInstaller 진입 스크립트. app/__main__.py 는 상대 import 를 써서 직접 진입점으로 쓸 수 없으므로
패키지로 불러 main() 을 부른다 (`python -m app` 과 같은 동작)."""
import multiprocessing
import sys
import traceback


def run() -> None:
    """처리 안 된 예외는 traceback 을 stderr 에 쓰고 종료 코드 1 로 끝낸다 (보통 파이썬과 같게).
    창 없는 윈도우 실행 파일은 기본으로 메시지 창을 띄우는데(spec 에서 끔), 그러면 숨은 하위 프로세스가 멈추고
    pipeline.probe_whisper_windows 가 기대하는 "종료 코드 1 + Traceback" 도 안 나온다."""
    from app.__main__ import main
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException:  # noqa: BLE001
        if sys.stderr is not None:
            traceback.print_exc()
            sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    # torch 가 띄우는 multiprocessing 보조 프로세스(resource_tracker 등)는 이 실행 파일을
    # `-B -S -I -c ...` 로 다시 부른다. freeze_support 가 그걸 처리하고 끝내야 창이 또 뜨지 않는다.
    multiprocessing.freeze_support()
    run()
