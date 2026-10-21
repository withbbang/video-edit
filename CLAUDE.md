# youtube-edit

유튜브 업로드 전 영상 정리 파이프라인. 입력 영상 하나를 받아
**노이즈 제거 → 한국어 필러·습관어·말 사이 무음 자동 컷 → SRT 자막 생성**을 한 번에 처리한다.
자막은 번인하지 않는다. 영상과 같은 이름의 SRT(유튜브 업로드·윈도우 플레이어용)를 만들고,
mp4 안에도 끄고 켤 수 있는 자막 트랙(QuickTime 용)을 넣는다 (`--embed-subs off` 로 끔).

## 구조

```
scripts/pipeline.py   파이프라인 본체 (ffmpeg + faster-whisper + numpy)
fillers.json          필러/습관어 사전과 감지 파라미터 — 사용자가 직접 고치는 파일
models/sh.rnnn        RNNoise 노이즈 모델 (ffmpeg arnndn 필터용)
setup.sh              ffmpeg 확인, .venv 생성, 의존성 설치
input/                원본 영상 넣는 곳 (절대 수정하지 않는다)
output/               결과물: <이름>_clean.mp4, <이름>_clean.srt, .cuts.json, .review.md, .report.md, .transcript.json
work/<이름>/          캐시 (정리된 오디오 등). 지워도 됨
.claude/skills/youtube-clean/   /youtube-clean 스킬
```

## 실행

항상 가상환경의 파이썬을 쓴다. `.venv` 가 없으면 먼저 `./setup.sh`.

```
.venv/bin/python scripts/pipeline.py input/영상.mp4      # 기본: 아/어/음·습관어·무음 컷 + 맥 하드웨어 인코딩 전부 on
.venv/bin/python scripts/pipeline.py input/영상.mp4 --review                       # 컷 리스트까지만
.venv/bin/python scripts/pipeline.py input/영상.mp4 --apply-cuts output/영상.cuts.json  # 수정한 컷으로 렌더
.venv/bin/python scripts/pipeline.py input/영상.mp4 --filler off                   # 아/어/음 남김
.venv/bin/python scripts/pipeline.py input/영상.mp4 --habit off|aggressive         # 습관어 남김 / 전부 컷
.venv/bin/python scripts/pipeline.py input/영상.mp4 --silence off                  # 무음 남김
.venv/bin/python scripts/pipeline.py input/영상.mp4 --hw off                       # libx264 소프트웨어 인코딩
.venv/bin/python scripts/pipeline.py input/영상.mp4 --model medium                 # 느린 맥에서 전사 시간 단축
```

전사·오디오 정리 결과는 `work/`, `output/*.transcript.json` 에 캐시되므로 같은 영상을 다시 돌리면
전사를 건너뛴다. 처음부터 다시 하려면 `--force`.

## 작업 규칙

- 영상 처리 요청이 오면 `/youtube-clean` 스킬 절차를 따른다.
- 전사와 렌더링은 영상 길이에 따라 수 분~수십 분 걸린다. 그냥 포그라운드로 실행하고 기다린다.
  타임아웃이 걸리면 `timeout` 을 늘려 다시 실행하되, 캐시 덕분에 전사는 반복되지 않는다.
- 끝나면 `output/<이름>.report.md` 를 읽어 **결과를 3~4줄로 요약**한다:
  원본/결과 길이, 컷 수, 제거된 초, 가장 많이 잘린 단어. 그리고 `review.md` 를 보고
  오탐이 의심되는 항목(문맥상 의미 있는 "그", "이제", "약간" 등)이 있으면 몇 개 짚어준다.
- 사용자가 특정 컷을 되돌리고 싶어 하면 `output/<이름>.cuts.json` 에서 해당 항목을 지우고
  `--apply-cuts` 로 다시 렌더한다. 전사는 다시 하지 않는다.
- 필러 사전(`fillers.json`)은 사용자가 요청할 때만 수정한다. 수정 후엔 캐시된 전사를 그대로 쓰므로
  빠르게 재실행할 수 있다 (`--force` 불필요).
- `input/` 의 원본은 건드리지 않는다. `output/` 의 같은 이름 결과물은 덮어쓴다.
- 설정 비교·실험은 `output/` 을 덮어쓰지 않게 `--out <scratch 폴더> --review` 로 돌린다.
  전사 캐시는 out 폴더에서 찾으므로 `<이름>.transcript.json` 을 먼저 복사하고,
  파라미터는 `--fillers <임시 json>` 으로 바꾼다 (`--review` 도 cuts.json·review.md 를 덮어씀).
- 출력 형식만 바꿔 다시 뽑을 땐 `--apply-cuts output/<이름>.cuts.json` 으로 렌더한다
  (그냥 다시 돌리면 기본 옵션으로 컷을 새로 판정해 사용자가 고른 모드가 사라진다).
- Whisper 단어 타임스탬프는 실제 소리와 ±0.5초 어긋나곤 한다. 컷·자막 문제를 볼 땐
  `pipeline.load_rms(work/<이름>/asr.wav)` 로 실제 에너지(dB)와 대조한다.
- 렌더 검증: `ffprobe -v error -show_entries stream=codec_type,duration:stream_tags=encoder,language -of csv output/<이름>_clean.mp4`
  → 영상·오디오 길이 차가 1프레임 이내인지, 인코더·자막 트랙(kor)이 있는지 본다.
- 로컬 ffmpeg 는 9.x. `-filter_complex_script` 같은 `*_script` 옵션은 없어졌으니 `-/filter_complex <파일>` 처럼 쓴다.
- 처음 실행 시 Whisper 모델(기본 large-v3-turbo, 약 1.6GB)을 Hugging Face 에서 내려받는다.
  네트워크 오류가 나면 그 사실을 알리고 재시도한다.

## 컷 판정 로직 (요약)

- `always` 목록(아/어/음 …)은 무조건 컷.
- `habit` 목록(그니까/약간/뭔가/이제 …)은 기본(`--habit on`)에서 무조건 컷.
- `habit_pause_after` 목록(그/저/뭐/좀 …)은 기본에선 **뒤에** 0.22초 이상 멈춤이 있을 때만 컷
  ("그 사람"의 "그"는 안 잘림). `--habit aggressive` 면 이것도 무조건 컷.
- 컷 경계는 ±120ms 안의 가장 조용한 지점으로 옮긴 뒤 프레임 격자에 맞춘다 (A/V 싱크 유지).
- 무음: 오디오 에너지가 `silence_threshold_db`(-40dB) 미만으로 `silence_min_sec`(0.3초) 이상 이어지면
  앞뒤로 `silence_pad_sec`(0.08초)씩 남기고 컷. 영상 맨 앞/뒤 무음은 전부 컷. 무음 안에 단어가 통째로 있으면
  작은 목소리일 수 있어 건너뛰고 review.md 에 표시. 무음 컷은 자막 단어를 지우지 않는다.
- 1.3초보다 긴 "필러"는 타임스탬프 오류로 보고 건너뛰며 review.md 에 따로 표시한다.
