# youtube-edit

유튜브 올리기 전 영상 정리: **노이즈 제거 → 한국어 필러·습관어·말 사이 무음 자동 컷 → 자막(SRT + 영상 내장)**.
전부 내 PC에서 돌아가고 외부 서비스 계정이 필요 없다.

## 처음 한 번

```bash
cd ~/Desktop/code/youtube-edit
./setup.sh                    # ffmpeg 확인 + .venv + 의존성
./setup.sh --download-model   # (선택) Whisper 모델 1.6GB 미리 받기
```

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
--hw on|off            맥 하드웨어 인코딩 h264_videotoolbox (기본 on. off 면 libx264 — 느리지만 화질 조금 더 좋음)
--embed-subs on|off    mp4 에 자막 트랙 넣기 (기본 on, 번인 아님)
--review               컷 목록만 만들고 렌더링 안 함 → 검토 후 --apply-cuts
--apply-cuts output/영상.cuts.json   수정한 컷 목록으로 렌더 (전사·판정 생략)
--model medium         전사 속도 우선 (기본 large-v3-turbo)
--denoise afftdn|none  노이즈 제거 방식 변경 / 끄기
--no-loudnorm          라우드니스 정규화 끄기
```

## 필러 사전

`fillers.json` 에서 고친다.

- `always` — 무조건 컷 (아, 어, 음 …)
- `habit` — 습관어. 기본(on)에서 전부 컷 (그니까, 약간, 뭔가, 이제 …)
- `habit_pause_after` — 기본(on)에선 뒤에 멈춤이 있을 때만 컷 (그, 저, 뭐, 좀 …). "그 사람"의 "그"는 안 잘림. aggressive 면 전부 컷
- `never_cut` — 절대 안 자를 단어
- `silence_min_sec` / `silence_pad_sec` / `silence_threshold_db` — 무음 컷 기준 (기본 0.3초 이상 무음을, 말 앞뒤로 0.08초씩 남기고 컷. -40dB 미만을 무음으로 봄)
- `prompt` — Whisper 가 필러를 전사에서 빼먹지 않도록 주는 예시 문장. 필러를 추가하면 여기에도 넣어주면 좋다

## 동작 원리

1. ffmpeg `arnndn`(RNNoise) 으로 배경 노이즈 제거 → `loudnorm` 으로 유튜브 권장 라우드니스
2. faster-whisper(한국어, 단어 타임스탬프)로 전사. 필러가 담긴 프롬프트를 줘서 "어/음"이 전사에 남게 함
3. 사전과 대조해 필러 컷 후보 추출 + 오디오 에너지로 말 사이 무음 구간 추출 → 필러 경계를 ±120ms 안의 가장 조용한 지점으로 옮김 → 프레임 격자에 정렬
4. 오디오는 샘플 단위로, 영상은 프레임 단위로 같은 구간을 잘라 A/V 싱크 유지
5. 잘린 시간을 빼서 SRT 타임코드 재계산 → 같은 자막을 mp4 에 mov_text 트랙으로도 넣음 (재인코딩 없음)
