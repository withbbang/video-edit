"""runner 테스트용 가짜 파이프라인: 인자 = 출력할 줄들, 환경변수/옵션으로 동작 지정.

`--sequence <json>` 이 있으면 분석 흉내만 낸다: 조각 파일마다 옆의 `<파일>.fake_tr.json`(없으면 빈 전사)을
scripts/sequence.combine_transcripts 로 이어 붙여 `--out` 폴더에 `<첫 입력 이름>.transcript.json`, 빈 cuts.json 을 쓴다 (30fps).
"""
import sys
import time

code = 0
args = sys.argv[1:]


def _sequence_mode(args: list) -> None:
    import json
    from fractions import Fraction
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import sequence as seq
    src = Path(args[0])
    out = Path(args[args.index("--out") + 1])
    fps = Fraction(30)
    pieces = seq.load_sequence(Path(args[args.index("--sequence") + 1]))
    trs = {}
    for p in pieces:
        side = Path(str(p.src) + ".fake_tr.json")
        trs[p.fkey] = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {"segments": []}
    tr = seq.combine_transcripts(seq.spans(pieces, fps), trs, fps)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{src.stem}.transcript.json").write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
    (out / f"{src.stem}.cuts.json").write_text(json.dumps({"fps": "30/1", "cuts": []}), encoding="utf-8")


if "--sequence" in args:
    _sequence_mode(args)
    sys.exit(0)
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
