# youtube-edit

유튜브 올리기 전 영상 정리: **노이즈 제거 → 한국어 필러·습관어·말 사이 무음 자동 컷 → 자막(SRT + 영상 내장)**.
전부 내 PC에서 돌아가고 외부 서비스 계정이 필요 없다.

## 데스크톱 앱 (설치해서 쓰기)

터미널 없이 창에 영상을 끌어다 놓으면 됩니다. 설치 파일은 [Releases](https://github.com/withbbang/video-edit/releases) 에서 받습니다.

| 운영체제 | 파일 | 크기 |
| --- | --- | --- |
| macOS — **Apple Silicon(M1 이후) 전용** (인텔 맥 미지원) | `youtube-edit-<버전>-mac-arm64.dmg` | 약 300MB |
| Windows 10/11 (64비트) | `youtube-edit-<버전>-win-x64-setup.exe` | 약 270MB |

아직 공개된 정식 버전은 없고 시험 빌드(rc)만 있습니다. 정식 버전이 공개되면 Releases 에 올라갑니다.

### 맥에서 처음 열 때

1. dmg 를 열고 `youtube-edit` 을 `Applications`(응용 프로그램) 폴더로 끌어 놓습니다.
2. 처음 열면 "확인되지 않은 개발자" 경고가 뜹니다. **시스템 설정 › 개인정보 보호 및 보안** 으로 가서
   아래쪽 "youtube-edit 이(가) 차단되었습니다" 옆의 **그래도 열기** 를 누릅니다.
   최신 macOS 에서는 우클릭 › 열기만으로는 열리지 않으니 이 경로를 써 주세요.

### 윈도우에서 설치할 때

1. 설치 파일을 실행하면 SmartScreen "Windows의 PC 보호" 창이 뜹니다. **추가 정보** → **실행** 을 누릅니다.
2. WebView2 런타임이 없으면 설치 프로그램이 함께 설치합니다 (인터넷 연결 필요).

### 업데이트할 때

새 버전을 설치할 때마다 위의 경고(맥 "그래도 열기", 윈도우 SmartScreen)를 한 번 더 넘겨야 합니다.
앱은 켤 때와 24시간마다 새 버전이 있는지 확인해 알려 줍니다.

### 첫 실행: 모델 내려받기

처음 켜면 음성 인식 모델(약 2.8GB)을 한 번 내려받습니다 (`모델 내려받는 중`). 인터넷 연결이 필요하고,
실패하면 `모델 내려받기 실패` 와 `인터넷 연결을 확인해 주시기 바랍니다` 가 뜨니 `다시 시도` 를 누릅니다.
받은 모델은 데이터 폴더에 남아 다음부터는 필요 없습니다. 받다가 끊기면 받은 곳부터 이어 받습니다.

윈도우에서 NVIDIA 그래픽카드가 있으면 GPU 용 라이브러리(약 1.3GB)를 함께 받아 자동으로 GPU 로 처리합니다.
그래픽카드가 없거나 GPU 준비에 실패하면 알림 없이 CPU 로 처리합니다 (느리지만 결과는 같습니다).

### 쓰는 법

영상을 창에 끌어다 놓거나 `＋ 끌어다 놓거나 파일 선택` 으로 고른 뒤 왼쪽에서 옵션을 정하고 `시작` 을 누릅니다.
영상은 한 번에 하나씩 차례로 처리됩니다. 끝나면 잘린 곳을 건너뛰는 미리보기로 확인하고 `저장` 을 누릅니다.

| 옵션 | 선택지 | 뜻 |
| --- | --- | --- |
| 망설임 (아·어·음…) | 유지 / **제거** | 아·어·음 같은 소리를 자름 |
| 말버릇 (그니까·약간·이제…) | 유지 / **일부** / 모두 | 일부: "그 사람"의 "그"처럼 뜻이 있는 건 남기고, 뒤에 멈춤이 있는 것만 자름. 모두: 전부 자름 |
| 말 없는 구간 | 슬라이더 0~100 (기본 50) | 말 사이 무음을 자름. 0 끔, 클수록 더 짧은 틈·작은 소리까지 자르고 100 은 단어 없는 틈도 자름 |
| 배경 잡음 제거 | 슬라이더 0~100 (기본 50) | 배경 잡음을 줄임. 0 끔, 클수록 세게 |
| 음량 맞추기 (고급) | 유지 / **맞춤** | 유튜브 기준 크기(-14 LUFS)로 맞춤 |
| 영상 안 자막 (고급) | 빼기 / **넣기** | mp4 안에 켜고 끄는 자막 트랙. SRT 파일은 항상 만듦 |
| 화질 (고급) | 작게 / **자동** / 높게 | 자동: 1080p30 8Mbps · 4K30 약 32Mbps |
| 저장 위치 (고급) | `설정` 버튼 | 결과를 저장할 폴더 |

굵게 표시한 것이 기본값입니다. 결과가 나온 뒤 옵션을 바꾸면 몇 초 안에 잘린 곳이 다시 판정됩니다. 분석은 항상 배경 잡음과 음량을 정리한 소리로 하고,
배경 잡음 세기를 바꾸면 미리보기 소리만 다시 만들어 바로 들어볼 수 있습니다.

### 자막 고치기·구간 자르기

- 자막 탭이나 영상 아래 자막에서 어절을 누르면 글자를 고칩니다. 비우면 그 어절을 빼고, `원래대로` 로 되돌립니다.
  `찾아 바꾸기` 로 한꺼번에 바꾸고, 줄마다 시작·끝을 0.1초씩 또는 `지금 위치로` 맞춥니다. 수정은 SRT 와 영상 안 자막에 반영됩니다.
- 타임라인을 끌어 구간을 직접 자르거나 되살립니다. 시작·끝은 0.01초씩 옮기거나 시간 칸을 눌러 직접 입력합니다.
  잘린 곳 목록(재생기 오른쪽)은 `전체`·`말 없는 구간`·`망설임`·`말버릇`·`직접 자름`·`직접 되살림` 탭으로 나뉩니다.
- 되살리기·구간 자르기·자막 고치기는 `↶ 실행 취소`·`↷ 다시 실행`(⌘Z·⇧⌘Z / Ctrl+Z·Ctrl+Y)으로 한 단계씩 되돌립니다.

### 파일 위치

- 저장 결과: 맥 `~/Movies/youtube-edit`, 윈도우 `%USERPROFILE%\Videos\youtube-edit` (`설정` 에서 바꿈).
  `<이름>_clean.mp4`(오디오만인 원본은 `.m4a`)와 `<이름>_clean.srt` 만 들어가며, 같은 이름은 덮어씁니다. 원본은 건드리지 않습니다.
- 앱 데이터(모델, 영상별 작업 캐시, 설정, 오류 기록 `logs/crash.log`): 맥 `~/Library/Application Support/youtube-edit/`,
  윈도우 `%APPDATA%\youtube-edit\`. 지워도 되지만 모델은 다시 받아야 합니다. 환경 변수 `YTEDIT_DATA_DIR` 로 위치를 바꿀 수 있습니다.
- 사용자 데이터 폴더에 `fillers.json` 을 두면 앱에 든 필러 사전보다 우선합니다.

### 완전히 지우기 (모델 포함)

앱을 지워도 모델(약 2.8GB)·작업 캐시·설정이 든 앱 데이터 폴더는 남습니다. 앱을 끈 뒤 아래를 지우세요.
저장 결과 폴더(`~/Movies/youtube-edit`, `%USERPROFILE%\Videos\youtube-edit`)는 직접 만든 영상이니 필요할 때만 지웁니다.

**맥** — `응용 프로그램` 폴더의 `youtube-edit` 을 휴지통으로 옮긴 뒤 터미널에서:

```bash
rm -rf ~/Library/Application\ Support/youtube-edit          # 모델·작업 캐시·설정·오류 기록
rm -rf ~/Library/Caches/com.withbbang.youtube-edit \
       ~/Library/WebKit/com.withbbang.youtube-edit \
       ~/Library/HTTPStorages/com.withbbang.youtube-edit \
       ~/Library/Saved\ Application\ State/com.withbbang.youtube-edit.savedState
rm -f  ~/Library/Preferences/com.withbbang.youtube-edit.plist
```

**윈도우** — `설정 › 앱 › 설치된 앱` 에서 `youtube-edit` 을 제거한 뒤 PowerShell 에서:

```powershell
Remove-Item -Recurse -Force "$env:APPDATA\youtube-edit"                    # 모델·작업 캐시·설정·오류 기록
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Programs\youtube-edit" -ErrorAction SilentlyContinue   # 제거 뒤 남은 설치 폴더
```

`YTEDIT_DATA_DIR` 로 데이터 위치를 바꿨다면 그 폴더를 지웁니다. 명령줄 버전의 모델은 아래 [명령줄 버전 지우기](#명령줄-버전-지우기)를 보세요.

### 라이선스

GPL-3.0 (`LICENSE`). 설치 파일에는 정적 빌드 ffmpeg 가 들어 있고, 서드파티 고지는 `packaging/THIRD_PARTY.txt`,
ffmpeg 의 GPL 대응 소스 코드는 각 릴리스에 함께 올라갑니다.

---

# 개발자·명령줄 사용

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
winget install Microsoft.VCRedist.2015+.x64   # VC++ 런타임 (없거나 오래되면 모델 로드에서 말없이 죽는다)
winget install Gyan.FFmpeg          # ffmpeg 없으면
winget install Python.Python.3.11   # 파이썬 없으면 (설치 후 터미널을 새로 연다)
cd youtube-edit
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

실행:

```powershell
.venv\Scripts\python scripts\pipeline.py input\영상.mp4 --device cpu
```

### cmd (명령 프롬프트)

```bat
winget install Microsoft.VCRedist.2015+.x64
REM VC++ 런타임 (없거나 오래되면 모델 로드에서 말없이 죽는다)
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
.venv\Scripts\python scripts\pipeline.py input\영상.mp4 --device cpu
```

### 맥과 다른 점

옵션은 맥과 같다 (`--help` 로 전체 목록).

- `--device cpu` 를 항상 붙인다. 기본값 `auto` 는 NVIDIA 카드가 있으면 CUDA 를 고르는데, cuDNN 이 없으면 에러 메시지 없이 종료된다. GPU 로 돌리려면 faster-whisper README 의 CUDA 설치 안내를 따른 뒤 `--device cuda --compute-type float16`
- 하드웨어 인코딩(`--hw on`)은 맥 전용이라 윈도우에서는 경고 한 줄 뒤 libx264 로 자동 대체된다. 정상 동작이니 무시해도 된다
- 일부 PC 에선 ctranslate2 4.5 이상이 모델 로드 때 메시지 없이 죽는다. 처음 실행 때 파이프라인이 별도 프로세스로 미리 띄워 보고, 죽으면 멈추면서 `.venv\Scripts\python -m pip install "ctranslate2==4.4.0"` 으로 내리라고 알려 준다 (`--device cpu` 를 안 붙였으면 그걸 먼저 안내). 정상인 PC 는 최신 버전을 그대로 쓴다
- 첫 실행 때 Whisper 모델(약 1.6GB)과 정렬 모델(약 1.2GB)을 내려받는 건 맥과 같다
- `python` 이 없다고 나오면 Microsoft Store 가 가로챈 것일 수 있다. `py -3 -m venv .venv` 로 바꿔 쓰거나, 설정 > 앱 > 앱 실행 별칭에서 python 항목을 끈다

### 명령줄 버전 지우기

명령줄 버전은 모델을 Hugging Face 캐시에 받는다 (앱 데이터 폴더와 따로). 프로젝트 폴더(`.venv`·`work/`·`output/` 포함)를 지운 뒤
캐시에서 이 프로젝트가 받은 모델만 지운다. 다른 프로그램이 같은 캐시를 쓸 수 있으니 `hub` 폴더 통째로는 지우지 않는다.

```bash
# 맥
rm -rf ~/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo \
       ~/.cache/huggingface/hub/models--kresnik--wav2vec2-large-xlsr-korean
```

```powershell
# 윈도우 (PowerShell)
Remove-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
  "$env:USERPROFILE\.cache\huggingface\hub\models--kresnik--wav2vec2-large-xlsr-korean"
```

`--model medium` 등 다른 모델을 썼다면 같은 폴더의 `models--Systran--faster-whisper-<이름>` 도 지운다.
`HF_HOME` 을 바꿨다면 `~/.cache/huggingface` 대신 그 폴더다.

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
- `silence_auto` / `silence_auto_margin_db` / `silence_auto_max_db` — 무음 기준 자동 맞춤 (기본 켬). 말 사이에도 차·바람 소리가 깔린 야외 영상은 -40dB 아래로 안 내려가 무음이 거의 안 잡히므로, 영상마다 말 없는 틈의 배경 소리를 재서 배경 +5dB 를 무음 기준으로 쓴다 (-40dB ~ -30dB 사이). 조용한 실내 영상은 그대로
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

## 데스크톱 앱 개발

파이프라인(`scripts/pipeline.py`)의 CLI 동작은 그대로이고, 앱(`app/`)은 이를 별도 프로세스로 부릅니다.

```bash
.venv/bin/python -m pip install -r requirements-app.txt -r requirements-dev.txt
.venv/bin/python -m app            # 앱 실행
.venv/bin/python -m pytest         # 테스트 (실제 모델로 끝까지 도는 e2e 는 기본으로 건너뜀)
YTEDIT_E2E_MODELS=<모델 받은 데이터 폴더> .venv/bin/python -m pytest tests/test_e2e_app.py   # e2e
```

테스트는 CI 에서 윈도우로도 돕니다. 경로는 `as_posix()` 로, 출력은 `\r\n` 을 맞춰 비교하고,
`sys.platform` 에 따라 갈리는 코드는 테스트에서 플랫폼을 고정합니다. CI 의 pytest 단계엔 ffmpeg 가 없습니다.

윈도우는 `.venv\Scripts\python` 을 씁니다. 구조: `app/`(창·큐·모델·업데이트), `packaging/`(PyInstaller·dmg·Inno Setup),
`.github/workflows/release.yml`(릴리스), `tests/`.

### 설치 파일 만들기

```bash
.venv/bin/python -I packaging/fetch_ffmpeg.py --platform mac --dest packaging/bin   # win 이면 --platform win
.venv/bin/pyinstaller --noconfirm packaging/youtube-edit.spec
bash packaging/macos/make_dmg.sh dist/youtube-edit.app dist/youtube-edit.dmg        # 맥
iscc packaging/windows/installer.iss                                                 # 윈도우 (Inno Setup, CI 참고)
python packaging/smoke_frozen.py dist/youtube-edit.app/Contents/MacOS/youtube-edit  # 묶은 앱 확인
```

묶은 앱의 진입점은 `packaging/launcher.py` 입니다. ffmpeg(맥 Martin Riedl, 윈도우 BtbN 정적 GPL 빌드)는
`packaging/fetch_ffmpeg.py` 에 URL·sha256 으로 고정돼 있고, 바꾸면 `packaging/THIRD_PARTY.txt` 도 다시 만들어야 합니다
(CI 가 달라졌는지 확인합니다).

### 릴리스

`app/__init__.py` 의 `__version__` 을 올린 뒤 `v<버전>` 태그를 push 하면 CI 가 맥·윈도우 파일을 빌드해 **초안(draft)** 릴리스를 만듭니다
(`v0.1.0-rc1` 처럼 접미사를 붙이면 prerelease). GitHub 에서 본문(바뀐 점)과 파일을 확인하고 직접 공개하면 그때부터 앱의 업데이트 확인에 잡힙니다.
같은 태그로 다시 돌릴 땐 먼저 기존 초안을 지웁니다. 본문 틀은 `packaging/release_notes.md`.
CI 결과는 `gh run list`, 실패 원인은 `gh run view <id> --log-failed` 로 봅니다. 초안 릴리스 주소는 `untagged-…` 로 보이니
`gh release view <태그>` 로 찾습니다.
