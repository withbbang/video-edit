"""PyInstaller 진입 스크립트. app/__main__.py 는 상대 import 를 써서 직접 진입점으로 쓸 수 없으므로
패키지로 불러 main() 을 부른다 (`python -m app` 과 같은 동작)."""
import multiprocessing

if __name__ == "__main__":
    # torch 가 띄우는 multiprocessing 보조 프로세스(resource_tracker 등)는 이 실행 파일을
    # `-B -S -I -c ...` 로 다시 부른다. freeze_support 가 그걸 처리하고 끝내야 창이 또 뜨지 않는다.
    multiprocessing.freeze_support()
    from app.__main__ import main
    main()
