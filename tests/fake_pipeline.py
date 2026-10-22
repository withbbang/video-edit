"""runner 테스트용 가짜 파이프라인: 인자 = 출력할 줄들, 환경변수/옵션으로 동작 지정."""
import sys
import time

code = 0
args = sys.argv[1:]
if args and args[0].startswith("--code="):
    code = int(args.pop(0).split("=")[1])
    if code < 0 and sys.platform != "win32":  # 시그널로 죽는 크래시 재현
        import os
        os.kill(os.getpid(), -code)
if args and args[0].startswith("--sleep="):
    time.sleep(float(args.pop(0).split("=")[1]))
for a in args:
    stream = sys.stderr if a.startswith("오류:") else sys.stdout
    print(a, file=stream, flush=True)
sys.exit(code)
