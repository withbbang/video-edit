# youtube-edit

유튜브 올리기 전 영상 정리: **노이즈 제거 → 한국어 필러·습관어·말 사이 무음 자동 컷 → 자막(SRT + 영상 내장)**.
전부 내 PC에서 돌아가고 외부 서비스 계정이 필요 없다.

## 처음 한 번

```bash
cd ~/Desktop/code/youtube-edit
./setup.sh                    # ffmpeg 확인 + .venv + 의존성
./setup.sh --download-model   # (선택) Whisper 모델 1.6GB·정렬 모델 1.2GB 미리 받기
```

## 윈도우에서

`setup.sh` 는 bash 용이라 윈도우에서는 쓰지 않는다. 가상환경을 활성화할 필요 없이
`.venv\Scripts\python` 을 직접 부르는 방식이라 PowerShell 과 cmd 명령이 같다.

### PowerShell

```powershell
winget install Gyan.FFmpeg          # ffmpeg 없으면
winget install Python.Python.3.11   # 파이썬 없으면 (설치 후 터미널을 새로 연다)
cd youtube-edit
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

실행:

```powershell
.venv\Scripts\python scripts\pipeline.py input\영상.mp4
```

### cmd (명령 프롬프트)

```bat
winget install Gyan.FFmpeg
REM ffmpeg 없을 때
winget install Python.Python.3.11
REM 파이썬 없을 때 (설치 후 창을 새로 연다)
cd youtube-edit
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

실행:

```bat
.venv\Scripts\python scripts\pipeline.py input\영상.mp4
```

### 맥과 다른 점

옵션은 맥과 같다 (`--help` 로 전체 목록).

- `--vcodec h264_videotoolbox` 는 맥 전용. NVIDIA 그래픽카드가 있으면 `--vcodec h264_nvenc`, 없으면 생략 (기본 libx264)
- Whisper 는 기본적으로 CPU(int8) 로 돈다. NVIDIA GPU 로 돌리려면 faster-whisper README 의 CUDA 설치 안내를 따른 뒤 `--device cuda --compute-type float16`
- 첫 실행 때 Whisper 모델(약 1.6GB)과 정렬 모델(약 1.2GB)을 내려받는 건 맥과 같다
- `python` 이 없다고 나오면 Microsoft Store 가 가로챈 것일 수 있다. `py -3 -m venv .venv` 로 바꿔 쓰거나, 설정 > 앱 > 앱 실행 별칭에서 python 항목을 끈다

## 쓰는 법

영상을 `input/` 에 넣고, 이 폴더에서 Claude Code 를 열어:

```
/youtube-clean input/영상.mp4
```

또는 직접:

```bash
.venv/bin/python scripts/pipeline.py input/영상.mp4
```

기본으로 아/어/음·습관어·무음 컷, 맥 하드웨어 인코딩, 자막 내장이 모두 켜져 있다.
빼고 싶은 것만 `off` 로 끈다 (예: `--silence off --hw off`).

결과는 `output/` 에:

| 파일 | 내용 |
|---|---|
| `영상_clean.mp4` | 노이즈 제거 + 필러·무음 컷 + 라우드니스(-14 LUFS) 적용된 영상. 끄고 켤 수 있는 한국어 자막 트랙 포함 (QuickTime: 보기 > 자막) |
| `영상_clean.srt` | 자막 (결과 영상 기준 타임코드). 영상과 이름이 같아 윈도우 기본 플레이어가 자동으로 불러온다. 유튜브엔 이 파일을 자막으로 따로 올린다 (유튜브는 영상 속 자막 트랙을 쓰지 않음) |
| `영상.review.md` | 잘라낸 구간 검토표 (시간·단어·문맥) |
| `영상.cuts.json` | 컷 목록. 항목을 지우고 `--apply-cuts` 로 재렌더 가능 |
| `영상.report.md` | 요약 리포트 |

## 자주 쓰는 옵션

```
--filler on|off        아/어/음 컷 (기본 on)
--habit on|off|aggressive
                       습관어 컷 (기본 on: 그니까/약간/이제 등은 전부, 그/저/좀 등은 뒤 멈춤일 때만.
                       aggressive: 그/저/좀까지 전부)
--silence on|off       말 사이 무음 컷 (기본 on)
--hw on|off            맥 하드웨어 인코딩 VideoToolbox (기본 on. off 면 libx264/libx265 — 느리지만 화질 조금 더 좋음)
--vbitrate auto|20M    --hw on 일 때 비트레이트 (기본 auto: 해상도·프레임 수에 맞춤)
--embed-subs on|off    mp4 에 자막 트랙 넣기 (기본 on, 번인 아님)
--review               컷 목록만 만들고 렌더링 안 함 → 검토 후 --apply-cuts
--apply-cuts output/영상.cuts.json   수정한 컷 목록으로 렌더 (전사·판정 생략)
--model medium         전사 속도 우선 (기본 large-v3-turbo)
--denoise afftdn|none  노이즈 제거 방식 변경 / 끄기
--no-loudnorm          라우드니스 정규화 끄기
```

### 오디오 정리 옵션 (`--denoise`, `--no-loudnorm`)

노이즈 제거 방식 — `--denoise` (기본 `rnnoise`)

| 값 | 동작 | 언제 |
|---|---|---|
| `rnnoise` | RNNoise 신경망 모델(`models/sh.rnnn`)로 노이즈 제거. 원음을 15% 섞어 목소리가 뭉개지지 않게 함 | 에어컨·팬·방 울림 같은 배경 잡음이 있을 때 (대부분) |
| `afftdn` | ffmpeg 기본 FFT 노이즈 감소. 더 약하지만 자연스러울 때가 있음 | RNNoise 가 목소리를 너무 깎을 때. 모델 파일이나 `arnndn` 필터가 없으면 자동으로 이걸 씀 |
| `none` | 노이즈 제거 안 함 | 이미 깨끗하게 녹음했을 때, 사인파 같은 합성 신호로 테스트할 때 (RNNoise 는 일정한 음을 잡음으로 보고 지움) |

어느 값이든 70Hz 하이패스(저음 웅웅거림 제거)는 항상 들어간다.

라우드니스 정규화 — `--no-loudnorm`

기본으로 전체 음량을 유튜브 기준 -14 LUFS 로 맞추고 순간 최고치를 -1.5dBTP 아래로 묶는다.
`--no-loudnorm` 을 주면 원본 음량을 그대로 둔다. 다른 프로그램에서 이미 마스터링했거나 음량을 직접 맞추고 싶을 때 쓴다.

주의할 점

- 두 옵션 중 하나라도 바꾸면 오디오를 다시 정리하고 **전사도 처음부터 다시 한다** (긴 영상은 오래 걸림).
- 전사와 무음 판정도 정리된 오디오로 한다. 그래서 `--no-loudnorm` 이면 작게 녹음된 영상이 -40dB 무음 기준에
  더 많이 걸려 무음 컷이 늘 수 있고, `--denoise none` 이면 배경 잡음 때문에 무음이 덜 잡힐 수 있다.

## 입력별로 알아서 처리하는 것

- 오디오와 영상의 시작 시점이 다른 파일 → 싱크를 맞춰서 처리
- 가변 프레임 영상(맥 화면 녹화 등) → 최대 프레임 속도로 출력해 움직임이 끊기지 않게
- 아이폰 HDR 등 10비트 영상 → HEVC 10비트로 색 정보 유지 (8비트는 H.264)
- 3채널 이상 오디오 → 스테레오로 다운믹스 / 앨범 아트가 붙은 m4a·mp3 → 오디오(m4a)로 출력
- 하드웨어 인코딩이 실패하는 해상도 → 소프트웨어 인코더로 자동 재시도
- 원본을 같은 이름으로 바꿔 넣거나 노이즈·모델 설정을 바꾸면 캐시를 자동으로 다시 만든다

## 필러 사전

`fillers.json` 에서 고친다.

- `always` — 무조건 컷 (아, 어, 음 …)
- `habit` — 습관어. 기본(on)에서 전부 컷 (그니까, 약간, 뭔가, 이제 …)
- `habit_pause_after` — 기본(on)에선 뒤에 멈춤이 있을 때만 컷 (그, 저, 뭐, 좀 …). "그 사람"의 "그"는 안 잘림. aggressive 면 전부 컷
- `never_cut` — 절대 안 자를 단어
- `single_only` — 한 번만 나올 때만 컷할 단어 (기본 하, 허). "하하"·"허 허"처럼 반복되면 웃음으로 보고 남긴다
- `silence_min_sec` / `silence_pad_sec` / `silence_threshold_db` — 무음 컷 기준 (기본 0.3초 이상 무음을, 말 앞뒤로 0.08초씩 남기고 컷. -40dB 미만을 무음으로 봄)
- `untranscribed_min_sec` — Whisper 가 전사하지 않은 목소리 기준 (기본 0.2초). 필러 컷 바로 옆이면 늘인 필러("이제 에~")로 보고 같이 컷, 그 밖은 review.md 에 "(전사 누락)"으로 표시만 한다. 0 이면 끔
- `silence_bridge_sec` / `silence_bridge_max_db` — 무음 사이에 낀 짧고 작은 소리(기본 0.3초 이하, -30dB 미만: 숨소리·입소리)는 무음으로 쳐서 조각이 남지 않게 한다
- `prompt` — Whisper 가 필러를 전사에서 빼먹지 않도록 주는 예시 문장 (30초 구간마다 다시 적용). 필러를 추가하면 여기에도 넣어주면 좋다. 바꾸면 다음 실행 때 전사를 다시 한다
- 띄어 쓴 항목(`"어떻게 보면"`)도 쓸 수 있다 — 이어진 단어들을 붙여서 비교한다

## 동작 원리

1. ffmpeg `arnndn`(RNNoise) 으로 배경 노이즈 제거 → `loudnorm` 으로 유튜브 권장 라우드니스
2. faster-whisper(한국어, 단어 타임스탬프)로 전사. 필러가 담긴 프롬프트(hotwords)를 줘서 "어/음"이 전사에 남게 함
3. 사전과 대조해 필러 컷 후보 추출 + 오디오 에너지로 말 사이 무음 구간 추출 → 필러 경계를 바깥쪽 120ms 안의 가장 조용한 지점까지 넓힘 → 프레임 격자에 정렬
   Whisper 단어 시간이 1~2초씩 밀린 필러는 주변 몇 초만 wav2vec2(CTC)로 다시 정렬해, 붙어 있는 진짜 단어("앞으로좀"의 "앞으로")는 살리고 필러만 자른다
4. 잘린 시간을 빼서 SRT 타임코드 재계산 (줄 나눔도 결과 영상 기준)
5. 오디오는 샘플 단위로, 영상은 프레임 단위로 같은 구간을 잘라 A/V 싱크 유지. 렌더하면서 자막을 mov_text 트랙으로 함께 넣음
