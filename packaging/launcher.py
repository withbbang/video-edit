"""PyInstaller 진입 스크립트. app/__main__.py 는 상대 import 를 써서 직접 진입점으로 쓸 수 없으므로
패키지로 불러 main() 을 부른다 (`python -m app` 과 같은 동작)."""
import datetime
import multiprocessing
import sys
import traceback

CRASH_LOG = ("logs", "crash.log")   # <paths.data_dir()>/logs/crash.log


def _is_gui_mode(argv: list[str]) -> bool:
    """--pipeline / -c 는 앱이 띄우는 하위 프로세스(stderr 를 앱이 읽음). 그 밖은 창 모드."""
    return argv[1:2] not in (["--pipeline"], ["-c"])


def _write_crash_log(text: str) -> None:
    """창 모드에선 stderr 를 볼 사람이 없으므로(윈도우 창 없는 실행 파일은 아예 없음) 사용자 데이터 폴더에 남긴다.
    로그를 못 써도 원래 오류 처리(종료 코드 1)는 그대로 한다."""
    try:
        from app import __version__, paths
        d = paths.data_dir().joinpath(*CRASH_LOG[:-1])
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with open(d / CRASH_LOG[-1], "a", encoding="utf-8") as f:
            f.write(f"=== {stamp} youtube-edit {__version__} ===\n{text.rstrip()}\n\n")
    except Exception:  # noqa: BLE001
        pass


def run() -> None:
    """처리 안 된 예외는 traceback 을 stderr 에 쓰고 종료 코드 1 로 끝낸다 (보통 파이썬과 같게).
    창 없는 윈도우 실행 파일은 기본으로 메시지 창을 띄우는데(spec 에서 끔), 그러면 숨은 하위 프로세스가 멈추고
    pipeline.probe_whisper_windows 가 기대하는 "종료 코드 1 + Traceback" 도 안 나온다.
    창 모드(--pipeline / -c 가 아님)에선 traceback 을 <사용자 데이터 폴더>/logs/crash.log 에도 덧붙인다."""
    gui = _is_gui_mode(sys.argv)  # main() 이 sys.argv 를 바꾸므로 먼저 본다
    try:
        from app.__main__ import main
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException:  # noqa: BLE001
        if gui:
            _write_crash_log(traceback.format_exc())
        if sys.stderr is not None:
            traceback.print_exc()
            sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    # torch 가 띄우는 multiprocessing 보조 프로세스(resource_tracker 등)는 이 실행 파일을
    # `-B -S -I -c ...` 로 다시 부른다. freeze_support 가 그걸 처리하고 끝내야 창이 또 뜨지 않는다.
    multiprocessing.freeze_support()
    run()
