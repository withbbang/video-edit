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
app/                  데스크톱 앱 (pywebview + app/ui 웹 화면). 파이프라인을 서브프로세스로 호출
packaging/            PyInstaller spec, ffmpeg 받기, dmg(macos/), Inno Setup(windows/), 릴리스 노트 틀, THIRD_PARTY.txt
.github/workflows/release.yml   태그 push → 맥·윈도우 빌드 → 릴리스 초안
tests/                pytest (requirements-dev.txt)
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
전사를 건너뛴다. 캐시는 원본(경로·크기·수정 시각)과 설정(노이즈 필터 체인, 모델, `prompt`)으로 확인하므로
같은 이름으로 다시 찍은 영상이나 설정을 바꾼 실행은 자동으로 다시 만든다. 강제로 처음부터 하려면 `--force`.

## 작업 규칙

- 영상 처리 요청이 오면 `/youtube-clean` 스킬 절차를 따른다.
- 전사는 실제 길이의 약 0.3배(M1 Pro 기준), 렌더링도 수 분 걸린다. 영상이 약 5분 이상이면 Bash 의
  `run_in_background` 로 실행하고 완료 알림을 기다린다 (포그라운드 최대 10분을 넘기면 어차피 백그라운드로 넘어간다).
  전사는 중간 저장이 없어 끊기면 처음부터 다시 하므로, 실행 중인 것이 있으면 같은 명령을 또 실행하지 않는다
  (같은 영상은 `work/<이름>/run.lock` 으로 동시 실행이 막힌다).
- 끝나면 `output/<이름>.report.md` 를 읽어 **결과를 3~4줄로 요약**한다:
  원본/결과 길이, 컷 수, 제거된 초, 가장 많이 잘린 단어. 그리고 `review.md` 를 보고
  오탐이 의심되는 항목(문맥상 의미 있는 "그", "이제", "약간" 등)이 있으면 몇 개 짚어준다.
- 사용자가 특정 컷을 되돌리고 싶어 하면 `output/<이름>.cuts.json` 에서 해당 항목을 지우고
  `--apply-cuts` 로 다시 렌더한다. 전사는 다시 하지 않는다.
- 필러 사전(`fillers.json`)은 사용자가 요청할 때만 수정한다. 단어 목록·파라미터를 고친 뒤엔 캐시된 전사를
  그대로 쓰므로 빠르다. 단 `prompt` 를 바꾸면 전사를 다시 한다.
- `input/` 의 원본은 건드리지 않는다. `output/` 의 같은 이름 결과물은 덮어쓴다.
- 설정 비교·실험은 `output/` 을 덮어쓰지 않게 `--out <scratch 폴더> --review` 로 돌린다.
  전사 캐시는 out 폴더에서 찾으므로 `<이름>.transcript.json` 을 먼저 복사하고,
  파라미터는 `--fillers <임시 json>` 으로 바꾼다 (`--review` 도 cuts.json·review.md 를 덮어씀).
- 출력 형식만 바꿔 다시 뽑을 땐 `--apply-cuts output/<이름>.cuts.json` 으로 렌더한다
  (그냥 다시 돌리면 기본 옵션으로 컷을 새로 판정해 사용자가 고른 모드가 사라진다).
  cuts.json 은 `start`/`end`(초)가 기준이고 프레임은 불러올 때 다시 계산한다. 전사가 바뀌었으면(지문 불일치)
  `--apply-cuts` 는 오류로 멈추니 컷 리스트를 새로 만든다. `--review` 와 함께 쓸 수 없다.
- Whisper 단어 타임스탬프는 실제 소리와 ±0.5초 어긋나곤 한다. 컷·자막 문제를 볼 땐
  `pipeline.load_rms(work/<이름>/asr.wav)` 로 실제 에너지(dB)와 대조한다.
- 렌더 검증: `ffprobe -v error -show_entries stream=codec_type,duration:stream_tags=encoder,language -of csv output/<이름>_clean.mp4`
  → 영상·오디오 길이 차가 1프레임 이내인지, 인코더·자막 트랙(kor)이 있는지 본다.
- 로컬 ffmpeg 는 9.x. `-filter_complex_script` 같은 `*_script` 옵션은 없어졌으니 `-/filter_complex <파일>` 처럼 쓴다
  (`render_video` 는 버전을 보고 고른다). ffmpeg 식 파서는 평평한 `a+b+…` 를 100항까지만 받으므로 긴 식은 `sum_tree` 로 묶는다.
- Windows: 일부 PC 에서 ctranslate2 4.5+ 가 모델 로드 때 메시지 없이 죽는다. requirements 로 버전을 묶지 않고,
  `probe_whisper_windows` 가 처음 한 번 별도 프로세스로 로드+짧은 전사를 해 보고 죽으면 고칠 방법(`--device cpu` /
  `ctranslate2==4.4.0` / VC++ 런타임)을 알려 주며 멈춘다. 성공 기록은 `work/.whisper_probe.json`.
- 합성 신호로 테스트할 땐 `--denoise none` 을 쓴다. RNNoise 는 사인파 같은 일정한 음을 잡음으로 보고 지운다.
- 처음 실행 시 Whisper 모델(기본 large-v3-turbo, 약 1.6GB)과 정렬 모델(kresnik/wav2vec2-large-xlsr-korean, 약 1.2GB)을
  Hugging Face 에서 내려받는다.
  네트워크 오류가 나면 그 사실을 알리고 재시도한다.

## 컷 판정 로직 (요약)

- `always` 목록(아/어/음 …)은 무조건 컷. 단 `single_only`(하/허)는 "하하"·"허 허"처럼 반복되면 웃음으로 보고 영상·자막에 남긴다.
- `habit` 목록(그니까/약간/뭔가/이제 …)은 기본(`--habit on`)에서 무조건 컷.
- `habit_pause_after` 목록(그/저/뭐/좀 …)은 기본에선 **뒤에** 0.22초 이상 멈춤이 있을 때만 컷
  ("그 사람"의 "그"는 안 잘림). `--habit aggressive` 면 이것도 무조건 컷.
- 필러 컷 경계는 **바깥쪽으로만** 120ms 안의 가장 조용한 지점까지 넓힌 뒤 프레임 격자에 맞춘다 (A/V 싱크 유지).
  안쪽으로 옮기면 자음과 모음 사이 골짜기에 걸려 필러 소리 일부가 남는다.
- `"어떻게 보면"` 처럼 띄어 쓴 항목은 이어진 단어를 붙여서 비교한다.
- 컷 사유 `gap`: `--silence-level` 100 에서 단어 없는 틈을 소리가 있어도 컷 (가장자리는 조용한 프레임까지 되물림).
- 무음: 오디오 에너지가 `silence_threshold_db`(-40dB) 미만으로 `silence_min_sec`(0.3초) 이상 이어지면
  앞뒤로 `silence_pad_sec`(0.08초)씩 남기고 컷. 영상 맨 앞/뒤 무음은 전부 컷. 무음 안에 단어가 통째로 있으면
  작은 목소리일 수 있어 그 단어 주변만 남기고 나머지 무음은 컷하며 review.md 에 표시.
- 무음 기준 자동(`silence_auto`): 말 없는 틈(단어 앞뒤 0.1초 밖) 소리의 하위 20% 를 배경으로 보고 배경 +5dB 를 무음 기준으로
  쓴다. -40dB 아래로는 안 내리고(조용한 실내는 그대로) -30dB·말소리 중간값-6dB 위로는 안 올림. 틈이 2초 미만이면 그대로.
  무음 잇기 기준도 무음 기준+10dB 까지 같이 올린다. 아래 "-40dB" 는 모두 이 기준을 뜻한다. 로그에 `무음 기준:` 으로 남는다.
- 전사 안 된 목소리: 단어가 덮지 않는 구간에 에너지가 -40dB 이상으로 `untranscribed_min_sec`(0.2초) 이상 있으면,
  필러 컷 바로 옆(0.15초 이내)이고 남긴 단어에 붙어 있지 않으며 1.3초 이하일 때만 `(전사 누락)` 으로 컷.
  남긴 단어 경계에 붙은 소리는 Whisper 단어 시간 오차로 보고 무시, 나머지는 review.md 에 표시만 한다.
- 단어 시간 의심: 단어 안쪽에 0.3초 이상 무음이 끼어 있고 앞뒤로 소리가 있으면 Whisper 가 단어를 엉뚱한 자리에 놓은 것으로 본다
  (앞 단어 꼬리 + 무음 + 숨소리를 한 단어로 묶음). 이 단어는 무음 잇기를 막지 않고 전사 누락 감지에서 소리를 덮지 않으며,
  그 안에서 필러에 붙은 0.35초 이하 소리는 필러 꼬리로 컷. review.md 에 표시. 끝에만 무음이 붙은 단어는 흔한 늘어짐이라 제외.
- 필러 컷에 붙은 전사 안 된 소리는 0.05초 이상이면 컷 (남기면 '…고' 같은 조각이 들림).
- 무음 잇기: 무음 사이 `silence_bridge_sec`(0.3초) 이하, `silence_bridge_max_db`(-30dB) 미만 소리는 무음으로 친다.
- 필러 시간 보정(CTC): 필러와 끊김 없이 이어진 소리 중 다른 단어가 설명 못 하는 부분이 있으면 필러 시간도 틀린 것으로 보고
  (예: "앞으로"가 1.8초 앞에 놓여 "좀"이 "앞으로" 소리를 덮음) 그 소리 덩어리 ±0.3초만 wav2vec2 로 강제 정렬한다.
  모든 글자가 소리 나는 곳(±60ms)에 있고 필러가 Whisper 위치 ±0.3초 안일 때만 채택. 경계는 무음 양끝, 없으면 다음 단어 첫 글자
  0.1초 앞 안의 가장 조용한 지점 (이 모델은 글자를 음절 첫머리에 찍음). 제자리 이웃 단어는 맞닿은 쪽 경계만, 자리가 의심스러운
  이웃은 양쪽 다 옮긴다. 이 모델은 긴 구간 전체 정렬에선 헤매므로 전사 전체에 쓰지 않는다. 보정 시간은 transcript.json 에 저장
  (원래 시간은 단어의 `whisper` 키), 사전·정렬 방식(`ALIGN_VERSION`)이 바뀌면 되돌리고 다시 한다. torch 가 없으면 건너뛰고 review.md 에 표시.
 필러로 판정된 단어(`words_idx`)뿐이다. 시간 겹침으로 지우지 않는다
  (Whisper 단어 시간이 무음 컷 쪽으로 번진 진짜 단어가 지워지기 때문). 자막 줄 나눔은 결과 영상 기준 시간으로 잰다.

## 입력별 처리

- 오디오/영상 시작 시점이 다르면 오디오 필터 체인 끝에서 `adelay`/`atrim` 으로 맞춘다.
- 가변 프레임(화면 녹화 등)은 `r_frame_rate`(최대 속도) 격자로 출력한다.
- 10비트/HDR 원본은 HEVC Main 10 으로 색 정보(bt2020/HLG/PQ)를 유지한다. 8비트는 H.264.
- 3채널 이상 오디오는 스테레오로 다운믹스, 앨범 아트가 붙은 오디오 파일은 m4a 로 출력, 홀수 해상도는 1px 크롭.
- `--hw on` 이면 VideoToolbox(`-allow_sw 1`), 실패하면 자동으로 libx264/libx265 로 다시 렌더한다.
  비트레이트는 기본 auto (1080p30 8M · 1080p60 12M · 4K30 ≈32M · 4K60 ≈48M, 10비트는 ×1.25).
- 1.3초보다 긴 "필러"는 타임스탬프 오류로 보고 건너뛰며 review.md 에 따로 표시한다.

## 데스크톱 앱

- 실행: `.venv/bin/python -m pip install -r requirements-app.txt` 후 `.venv/bin/python -m app`.
  테스트: `.venv/bin/python -m pytest` (`pip install -r requirements-dev.txt`). 파이프라인이나 앱을 고친 뒤엔 전체를 돌린다.
- 화면의 순수 계산(보기 범위·따라가기 등)은 `app/ui/timeline.js` 에 두고 `tests/test_timeline_js.py` 에서 node 로 테스트한다.
- `pipeline.py` 의 CLI 기본 동작은 그대로다. 앱은 이를 서브프로세스로 부르며 앱 전용 숨은 옵션만 추가돼 있다
  (기본값은 기존 동작): `--progress-json`(@progress 줄 출력), `--render-denoise`/`--render-loudnorm`(렌더 때만 배경 잡음·음량; 앱은 지금 `--denoise-level` 을 쓴다),
  `--preview`(미리보기 폴더: video.mp4/video.index.json/audio_full.m4a/audio_full.index.json), `--preview-cut-audio <m4a>`(`--apply-cuts` 와만: 컷 적용한 미리보기 소리 + index 만 만들고 끝. 캐시된 전사·분석 오디오만 쓰고 없거나 맞지 않으면 오류로 멈춤), `--vbitrate-scale`(화질 배율 0.6/1/1.5), `--work-root`(작업 캐시 폴더),
  `--media-out`(결과 mp4/m4a·srt 저장 폴더), `--ctc-model`(정렬 모델 ID 또는 로컬 폴더),
  `--silence-level 0~100`(말 없는 구간 세기, 주면 `--silence` 대신: 0 끔 · 1~50 최소 길이 1.0초→fillers.json 값 · 50 = `--silence on` ·
  51~100 자동 무음 기준을 말소리 쪽으로 올림(여유 +10dB·상한 +24dB·말소리 여유 −4dB, fillers.json 값에 더함) ·
  100 은 단어 없는 틈도 `gap` 으로 컷(단어가 하나도 없으면 안 자름). 무음 판정에만 쓰고 정렬 키·필러 판정은 그대로),
  `--denoise-level 0~100`(결과·미리보기 오디오만, 주면 `--render-denoise` 대신: 0 끔 · 1~50 RNNoise 섞는 비율 0→0.85 (50 = `--denoise rnnoise`) ·
  51~100 비율 1.0 까지 + afftdn 추가. 분석 오디오·전사는 그대로. `--preview` 와 쓰면 미리보기 소리만 다시 만들고 preview_video.mp4 는 캐시 재사용),
  `--subs-edits <json>`(앱의 자막 수정 `{transcript_sha1, words, lines, styles}` 을 SRT 에 적용. `transcript_sha1` 이 지금 전사와 다르면 로그만 남기고 무시),
  `--burn-subs on`·`--burn-fonts <폴더>`(유튜브용 자막 새기기: `styles`(어절마다 글자별 `{font,size,color,bold}`)를 반영한 ASS 를
  `work/<이름>/burn.ass` 로 만들어 컷 뒤 영상에 `ass` 필터로 새김. 켜면 자막 트랙은 넣지 않음. ffmpeg 에 libass 필요 —
  홈브루 ffmpeg 9 엔 없고 `packaging/bin/ffmpeg`(배포용)엔 있다. 크기·여백 비율은 `BURN_*` 와 `app/ui/capstyle.js` 의 `BASE` 가 같아야 함).
- 앱 화면: 자막 탭 줄·(멈췄을 때) 영상 위 자막을 누르면 그 줄 전체를 고치는 칸(contenteditable)이 열리고 꾸밈 막대도 켜짐
  (Api.set_line → `subtitles.plan_line` 이 띄어쓰기 낱말을 어절에 다시 나눠 담고 꾸밈을 글자 따라 옮김, `원문 복원`), 찾아 바꾸기, 줄 시간 ±0.1초·`지금 위치로`.
  타임라인을 끌거나 Shift+클릭 두 번으로 구간을 골라 직접 자르거나 되살림
  (잘린 곳 목록에 `직접 자름`/`직접 되살림`). 구간 끝은 원본 프레임 격자(결과의 `fps`), ←/→ 로 1프레임(Shift 10프레임).
  막대 아래 확대 타임라인: 썸네일 줄(`app/thumbs.py` 가 미리보기 영상에서 1초 간격 시트를 열어 본 영상만 뒤에서 만듦, `get_thumbs`)·
  소리 줄(`get_waveform`, asr.wav 에너지). 보기 계산은 `app/ui/timeline.js`. 편집은 `실행 취소`/`실행 재개`
  (Api.undo/redo, 메모리 스냅숏 — 옵션 변경은 제외). `말 없는 구간`·`배경 잡음` 은 0~100 슬라이더(50 = 예전 기본).
  자막 수정은 `out/<이름>.subs_edits.json` 에 저장되고 전사가 바뀌면 지워진다.
  자막은 영상 화면 아래쪽에 겹쳐 그린다(새기는 자리·크기 그대로). 글자 꾸미기는 줄 고치는 칸 안 선택(없으면 줄 전체) 또는 자막 탭 목록에서
  음절(span.c)을 끌어 선택하면(브라우저 선택이 있는 동안만 켜짐, 꾸민 뒤엔 같은 글자를 다시 선택) 꾸밈 막대(목록 위 — 꺼지면 흐리게·inert, 영상 위에서 고칠 땐 자막 위에 뜨는 한 벌 더)로 글자마다 글꼴·크기·색·굵기를 바꾼다 (Api.set_style, Job.word_styles, 실행 취소 대상). 글꼴은 `app/ui/fonts`(OFL) — 화면 @font-face 와 새기기가 같은 파일을 쓴다.
- 사용자 데이터: 맥 `~/Library/Application Support/youtube-edit`, 윈도우 `%APPDATA%\youtube-edit` (`YTEDIT_DATA_DIR` 로 덮어씀).
  앱 설치 폴더에는 쓰지 않는다. 화면 문구는 스펙(`docs/superpowers/specs/2026-10-22-desktop-app-design.md` 3장) 그대로 쓴다.
- 릴리스: `app/__init__.py` 의 `__version__` 과 같은 `v<버전>` 태그(접미사 `-rc1` 은 prerelease)를 push 하면 CI 가 draft 릴리스를 만든다.
  같은 태그로 다시 돌리기 전엔 기존 draft 를 지운다. **태그 push·릴리스 공개는 사용자 승인 없이 하지 않는다.**
