# 결과 화면 손보기 (자막 고치기 · 구간 직접 자르기 · 잡음/무음 슬라이더) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 데스크톱 앱 결과 화면에서 자막 어절·줄 시간을 고치고, 구간을 직접 자르거나 되살리고, 말 없는 구간·배경 잡음 세기를 슬라이더로 조절해 저장 결과(SRT·mp4 자막·영상)에 반영한다.

**Architecture:** 자막 줄 나누기를 `pipeline.py` 의 순수 함수(`build_captions`)로 떼어 앱 프로세스와 렌더가 같은 함수를 쓴다. 사용자 수정(어절 글자·줄 시간·구간 작업)은 `Job` 에 저장되고, 저장 때 `--subs-edits` 파일과 `save.cuts.json` 으로 파이프라인에 넘어간다. 두 슬라이더는 숫자 옵션(0~100)으로 바뀌어 앱 전용 숨은 옵션 `--silence-level`·`--denoise-level` 로 전달되며, 50 이 지금의 "제거" 와 같은 결과다.

**Tech Stack:** Python 3 (pipeline.py, numpy, ffmpeg), pywebview 앱(app/), 바닐라 JS(app/ui), pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-edit-subs-and-tuning-design.md` (목업: `.superpowers/brainstorm/88578-1792653401/content/` 의 `subs-layout-v2.html`·`range-edit.html`·`sliders-v2.html`)

## Global Constraints

- CLI 기본 동작 불변: 숨은 옵션 없이 `pipeline.py` 를 돌린 결과(cuts.json, SRT)는 바이트 단위로 지금과 같아야 한다.
- 받아쓰기(전사)를 다시 하지 않는다: 슬라이더·자막 수정·구간 작업 어느 것도 `transcript.json` 캐시 키를 바꾸지 않는다.
- 두 슬라이더 모두 정수 0~100, 기본 50. **50 = 지금의 `제거` 결과와 같음**, 0 = 끔. 기존 작업 옵션 `keep`→0, `remove`→50.
- 화면 문구(사용자가 고른 것, 그대로):
  - 말 없는 구간 양 끝 `안 자름` · `최대한 자름`. 설명: 0 `말 없는 구간을 자르지 않습니다` / 1~50 `조용히 말 없는 구간 중 {N}초 이상만 자름 · {곳}곳 · −{초}초` / 51~99 `조금 시끄러운 말 없는 구간도 자름 (0.3초 이상) · {곳}곳 · −{초}초` / 100 `말 없는 구간은 소리가 있어도 전부 자름 · {곳}곳 · −{초}초`. "지금" 을 붙이지 않는다.
  - 배경 잡음 양 끝 `끔` · `최대 (목소리 먹먹해질 수 있음)`. 설명: 0 `원래 소리 그대로` / 1~50 `목소리를 살리며 가볍게 지움` / 51~100 `계속 깔리는 소리·튀는 소리까지 세게 지움`.
  - 화면에 "전사" 대신 "받아쓰기" 를 쓴다 (새로 만드는 문구에 한함).
- 색: 고친 어절 초록 `#2a9d5c`(`--restored`), 잘린 곳 `#d93036`, 선택 구간 `--accent` `#4a7dff`.
- 시간은 모두 **원본 영상 기준 초**로 저장한다. 결과 영상 시간으로 바꾸는 건 SRT 만들 때뿐.
- 어절 키는 문자열 `"seg:i"` (전사 `segments[].id` 와 단어 순번). cuts.json 의 `words_idx` `[seg, i]` 와 같은 단어.
- 파이프라인·앱을 고친 뒤 `.venv/bin/python -m pytest` 전체 통과.

## Review Focus

- 자막 수정 후 옵션(슬라이더 등)을 바꿔 다시 판정 → 고친 어절·줄 시간·직접 자른 곳이 그대로 남아야 한다 (Task 5 `test_edits_survive_rejudge`).
- 받아쓰기가 바뀐 작업(지문 불일치)에 남은 수정 → 렌더가 멈추지 말고 수정을 버리고 화면에 알려야 한다 (Task 5 `test_stale_edits_dropped`).
- 직접 자른 구간이 영상 전체를 덮음 → "남길 구간이 없습니다" 로 저장이 실패하고 앱이 오류를 보여 줘야 한다; 구간 작업 API 는 `end <= start`·영상 밖 값을 거절 (Task 4 `test_range_op_rejects_bad_range`).
- 슬라이더를 빠르게 여러 번 움직임 → 마지막 값으로만 한 번 더 판정 (기존 `rejudge` 후속 한 번 규칙, Task 7 `test_slider_burst_rejudges_once_more`).
- 찾아 바꾸기에서 찾는 말이 빈칸이거나 공백뿐 → 0곳, 아무것도 안 바뀜 (Task 3 `test_replace_blank_noop`).

---

### Task 1: 자막 줄 계산을 순수 함수로 (`build_captions`) + 수정 반영

**Files:**
- Modify: `scripts/pipeline.py` (`TimeRemap`, `build_srt` 부근 1038-1110)
- Test: `tests/test_captions.py` (새로)

**Interfaces:**
- Produces:
  - `build_captions(tr: dict, cut_words: set[tuple[int,int]], remap: TimeRemap, always_fillers: set[str] = frozenset(), single_only: set[str] = frozenset(), word_edits: dict[str,str] | None = None, max_chars=28, max_dur=5.0, gap_break=0.7) -> list[dict]`
    각 줄: `{"key": "seg:i"(첫 어절), "start": 원본초, "end": 원본초, "words": [{"key", "text", "orig", "start", "end"}]}`.
    줄 나누기는 **원래 Whisper 글자**로 한다 (고쳐도 줄이 다시 나뉘지 않게). `word_edits` 의 빈 글자 어절은 `words` 에서 빠진다(빈 줄도 빠짐).
  - `captions_to_srt(caps: list[dict], remap: TimeRemap, final_dur: float, line_times: dict[str, list[float]] | None = None) -> str`
    `line_times[key] = [start, end]`(원본초) 가 있는 줄은 그 시간을 쓰고 뒷처리(겹침 0.05초 띄우기, 최소 0.3초)에서 **움직이지 않는다**; 이웃의 고치지 않은 줄만 맞춘다. 원본초→결과초 변환은 `remap`.
  - `build_srt(...)` 는 시그니처 그대로 두고 위 두 함수를 부르는 얇은 래퍼가 된다.

- [ ] **Step 1: 지금 `build_srt` 출력 고정 테스트(특성 테스트) 작성** — `test_build_srt_unchanged`: 필러 컷·무음 컷·문장부호·0.7초 멈춤·28자 초과가 섞인 합성 전사(3 세그먼트, 15어절)로 지금 코드의 SRT 문자열을 **먼저 돌려서 얻은 값**을 테스트에 문자열 상수로 박는다.
- [ ] **Step 2: 실패 테스트 작성**
  - `test_captions_keys_and_original_times`: 줄 `key` 가 첫 어절 `"seg:i"`, `start/end` 가 원본초.
  - `test_word_edit_changes_text_not_grouping`: `word_edits={"0:1": "아주아주아주긴글자로바뀐어절"}` 이어도 줄 경계(각 줄 key 목록)가 수정 전과 같음.
  - `test_empty_edit_drops_word`: `{"0:1": ""}` → 그 어절이 SRT 에 없음.
  - `test_line_time_fixed`: `line_times={"0:3": [2.0, 4.0]}` → 그 줄 SRT 시간이 `remap(2.0)`,`remap(4.0)`; 앞 줄 끝이 `remap(2.0)-0.05` 이하로 줄어듦; 고친 줄은 그대로.
- [ ] **Step 3: `pytest tests/test_captions.py -v` → 새 테스트 FAIL (`build_captions` 없음), 특성 테스트 PASS**
- [ ] **Step 4: 구현** — 기존 `build_srt` 의 줄 나누기 루프를 `build_captions` 로, 겹침·길이 뒷처리와 SRT 글자 만들기를 `captions_to_srt` 로 옮긴다. `TimeRemap.cut_words` 는 그대로.
- [ ] **Step 5: `pytest tests/test_captions.py tests/test_pipeline_features.py -v` → PASS**
- [ ] **Step 6: Commit** `git commit -m "자막 줄 계산을 build_captions/captions_to_srt 로 분리, 어절·줄 시간 수정 반영"`

### Task 2: 렌더가 자막 수정 파일을 받는다 (`--subs-edits`)

**Files:**
- Modify: `scripts/pipeline.py` (`build_parser`, `process` 의 4) SRT 단계)
- Test: `tests/test_captions.py`

**Interfaces:**
- Consumes: Task 1 `build_captions`, `captions_to_srt`.
- Produces: 숨은 옵션 `--subs-edits <json>`. 파일 형식
  `{"transcript_sha1": str, "words": {"seg:i": str}, "lines": {"seg:i": [start, end]}}`.
  `transcript_sha1` 이 지금 전사와 다르면 `log("자막 수정이 지금 받아쓰기와 맞지 않아 무시합니다")` 하고 수정 없이 진행 (멈추지 않음).
  함수 `load_subs_edits(path: Path | None, tr: dict) -> tuple[dict, dict]` → `(words, lines)`.

- [ ] **Step 1: 실패 테스트** — `test_load_subs_edits_matches_sha`(맞으면 그대로), `test_load_subs_edits_stale_ignored`(다르면 `({}, {})`), `test_load_subs_edits_none`(None → `({}, {})`).
- [ ] **Step 2: FAIL 확인** `pytest tests/test_captions.py -k subs_edits -v`
- [ ] **Step 3: 구현** — `process` 에서 `build_srt` 대신 `build_captions(..., word_edits=words)` → `captions_to_srt(..., line_times=lines)`. 옵션 없을 때 결과는 Task 1 특성 테스트와 같음.
- [ ] **Step 4: PASS 확인** + `pytest tests/test_pipeline_args.py -v`
- [ ] **Step 5: Commit** `"렌더: --subs-edits 로 자막 수정 반영 (받아쓰기 달라지면 무시)"`

### Task 3: 앱 쪽 자막 모델 — 어절 고치기·찾아 바꾸기·줄 시간

**Files:**
- Create: `app/subtitles.py`
- Modify: `app/store.py` (`Job` 필드), `app/api.py` (새 메서드)
- Test: `tests/test_subtitles.py` (새로)

**Interfaces:**
- Consumes: Task 1 함수 (`pipeline` 을 `paths.bundle_dir()/"scripts"` 를 `sys.path` 에 넣고 지연 import — `app/__main__.py` 51행과 같은 방식).
- Produces:
  - `Job` 새 필드: `word_edits: dict = {}` (`"seg:i"`→글자), `line_times: dict = {}` (`"seg:i"`→`[s,e]`), `range_ops: list = []` (Task 4), `edits_sha1: str | None = None` (수정할 때의 전사 지문). `from_dict` 는 없는 필드를 기본값으로 (기존 state.json 그대로 열림).
  - `subtitles.captions(cuts_json: dict, transcript: dict, cfg: dict, word_edits: dict, line_times: dict) -> list[dict]` — 넘겨받은 컷(저장 때 쓰는 컷)으로 `build_captions` 를 부르고, 각 어절에 `edited: bool`, 줄에 `timed: bool`, 줄 `start/end` 에 `line_times` 를 반영한다. `cfg` 는 `pipeline.load_fillers(<데이터 폴더 fillers.json 또는 번들 fillers.json>)` (`runner._tail` 과 같은 선택).
  - 이 Task 에서 Api 는 컷을 `diff.without_restored(cuts_json, job.restored)` 로 만든다 (Task 4 가 `effective_cuts` 로 바꿈).
  - `subtitles.replace_plan(caps: list[dict], find: str, repl: str) -> dict[str, str]` — 바뀔 어절 키→새 글자. 규칙(스펙 3.2): 한 줄 안에서 공백 무시하고 찾음, 대소문자 구분, 맞지 않은 앞/뒷부분 유지, 여러 어절 걸침이면 첫 어절=[앞부분+repl], 마지막=[뒷부분], 사이=빈 글자. 이미 고친 글자 기준.
  - `subtitles.line_bounds(caps, key) -> tuple[float, float]` — 앞 줄 끝·다음 줄 시작 (줄 시간 제한용).
  - Api: `get_captions(job_id) -> list | None`, `set_word(job_id, key: str, text: str | None) -> list | None` (None=원래대로), `replace_text(job_id, find: str, repl: str, apply: bool) -> {"count": int, "keys": [..]}`, `set_line_time(job_id, key: str, start: float | None, end: float | None) -> list | None` (둘 다 None=원래대로; 앞 줄 끝~다음 줄 시작 안으로 자르고 길이 최소 0.3초).
    모두 `REVIEW`/`SAVED` 상태에서만, `SAVED` 면 `REVIEW` 로 돌리고 `push("job", ...)` (지금 `set_restored` 와 같은 규칙). 처음 수정할 때 `edits_sha1 = transcript_sha1(tr)`.

- [ ] **Step 1: 실패 테스트** (`tests/test_subtitles.py`, 합성 전사)
  - `test_replace_keeps_particle`: `김영산이` 에서 `김영산`→`김영선` → `{"k": "김영선이"}`.
  - `test_replace_across_words`: 어절 `에이피`,`아이를` 에서 `에이피아이`→`API` → `{"k1": "API", "k2": "를"}`.
  - `test_replace_not_across_lines`: 두 줄 끝/처음에 나뉜 말은 0곳.
  - `test_replace_case_sensitive`: `Ian` 찾기에 `ian` 안 걸림.
  - `test_replace_blank_noop`: `find=""` / `"  "` → `{}`.
  - `test_line_time_clamped` (Api): 다음 줄 시작보다 뒤로 끝을 주면 다음 줄 시작으로 잘림; 길이 0.1초를 주면 0.3초.
  - `test_set_word_marks_review` (Api): `SAVED` 작업에서 `set_word` → 상태 `review`, `get_captions` 에 `edited: True`.
  - `test_job_from_old_dict`: 새 필드 없는 dict 로 `Job.from_dict` 성공.
- [ ] **Step 2: FAIL 확인** `pytest tests/test_subtitles.py -v`
- [ ] **Step 3: 구현** (`app/subtitles.py`, `store.Job` 필드, `api.Api` 메서드)
- [ ] **Step 4: PASS 확인** + `pytest tests/test_store.py tests/test_api.py -v`
- [ ] **Step 5: Commit** `"앱: 자막 어절 고치기·찾아 바꾸기·줄 시간 API"`

### Task 4: 구간 직접 자르기·되살리기 (`range_ops`)

**Files:**
- Modify: `app/diff.py`, `app/api.py` (`get_result`, `save_cuts`, 새 메서드), `app/store.py`
- Test: `tests/test_range_ops.py` (새로)

**Interfaces:**
- Consumes: `Job.range_ops`, 기존 `Job.restored`, `diff.is_restored`.
- Produces:
  - `diff.effective_cuts(cuts_json: dict, restored: list, range_ops: list, transcript: dict | None) -> dict` — 순서: (1) 기존 `without_restored` (컷 단위 되살리기), (2) `range_ops` 를 순서대로 적용. `cut` 은 `{"start","end","word":"(직접 자름)","reason":"manual","words_idx": 가운데 시점이 구간 안인 단어들}` 추가, `restore` 는 모든 컷에서 그 시간만큼 빼서 쪼갬(쪼갠 조각의 `words_idx` 는 가운데 시점이 조각 안인 단어만, 0.05초 미만 조각은 버림). 나중 작업이 이김. 단어 가운데 시점은 transcript 의 `start/end`.
  - `cut_items` 에 직접 자른 곳 항목: `{"kind": "manual", "word": "", "op": 인덱스, "restored": False}`; 목록 이름 `직접 자름`.
  - Api: `add_range_op(job_id, op: "cut"|"restore", start: float, end: float) -> dict | None`, `remove_range_op(job_id, index: int) -> dict | None`. `end - start < 0.05`, `start < 0`, `end > duration` 이면 None. 상태 규칙은 `set_restored` 와 같음.
  - `get_result`·`save_cuts`·`get_captions`(Task 3) 가 `effective_cuts` 를 쓴다 (미리보기 건너뛰기·요약·자막·저장이 같은 컷). `save_cuts` 는 `range_ops` 가 있어도 파일을 만든다.
  - `Job.range_ops` 필드는 Task 3 에서 이미 추가됨.

- [ ] **Step 1: 실패 테스트**
  - `test_manual_cut_adds_cut_and_words`: 구간 안 가운데 시점 단어만 `words_idx` 에.
  - `test_restore_range_splits_auto_cut`: 1.0~3.0 무음 컷에 1.5~2.0 되살리기 → 컷 [1.0,1.5],[2.0,3.0].
  - `test_restore_range_returns_word_to_subs`: 여러 단어 필러 컷 일부 되살림 → 그 단어가 `words_idx` 에서 빠짐.
  - `test_later_op_wins`: cut(2,4) 후 restore(3,4) → [2,3] 만 잘림; 순서 반대면 [2,4].
  - `test_list_restore_removes_manual_op` (Api): `remove_range_op` 후 `get_result` 에서 그 컷 없음.
  - `test_range_op_rejects_bad_range` (Api): end<=start, 음수, duration 초과 → None.
  - `test_save_cuts_includes_manual` : `save_cuts(job)` 파일에 `reason: manual` 컷.
- [ ] **Step 2: FAIL 확인** `pytest tests/test_range_ops.py -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: PASS 확인** + `pytest tests/test_restore_cuts.py tests/test_diff.py -v`
- [ ] **Step 5: Commit** `"앱: 구간 직접 자르기·시간 그대로 되살리기 (나중 작업이 이김)"`

### Task 5: 저장 때 수정 넘기기 + 다시 판정·지문 변화 처리

**Files:**
- Modify: `app/api.py` (`pipeline_command`/`save_cuts` 주변), `app/runner.py` (`render_command`)
- Test: `tests/test_subtitles.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: Task 2 `--subs-edits` 형식, Task 3 `Job` 필드.
- Produces: `api.save_subs_edits(job: Job) -> Path | None` — 수정이 있으면 `out/<stem>.subs_edits.json` 을 쓰고 경로 반환. `runner.render_command(job, model, device, media_dir, cuts_path=None, edits_path=None)` 가 `--subs-edits` 를 붙임.
  `get_result`/`get_captions` 에서 `job.edits_sha1` 이 지금 전사 지문과 다르면 `word_edits`·`line_times` 를 비우고 결과에 `"edits_dropped": True` 를 한 번 실어 보낸다 (화면 안내: `받아쓰기가 바뀌어 고친 자막을 지웠습니다`).

- [ ] **Step 1: 실패 테스트** — `test_render_command_passes_edits`, `test_edits_survive_rejudge` (옵션 바꿔 `cuts.json` 을 새로 써도 `word_edits`·`range_ops` 그대로, `get_captions` 에 반영), `test_stale_edits_dropped`.
- [ ] **Step 2: FAIL 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: PASS 확인** `pytest tests/test_subtitles.py tests/test_runner.py tests/test_api.py -v`
- [ ] **Step 5: Commit** `"저장: 자막 수정 파일 넘기기, 받아쓰기가 바뀌면 수정 버림"`

### Task 6: 파이프라인 — 말 없는 구간 세기 `--silence-level`

**Files:**
- Modify: `scripts/pipeline.py` (`auto_silence`, `detect_silences` 근처, `build_parser`, `process`)
- Test: `tests/test_silence_level.py` (새로)

**Interfaces:**
- Produces:
  - 숨은 옵션 `--silence-level N` (0~100, 기본 None = `--silence` 그대로). 0 → `--silence off` 와 같음.
  - `silence_level_cfg(cfg: dict, level: int) -> dict` — 덮어쓸 값. 1~50: `silence_min_sec = 1.0 - 0.7*level/50`. 50~100: `silence_min_sec=0.3`, `silence_auto_max_db = -30 + 24*(level-50)/50`, `silence_auto_speech_margin_db = 6 - 4*(level-50)/50`. 50 에서 기존 값(0.3, -30, 6)과 정확히 같다.
  - `auto_silence` 의 "말소리 중간값 − 6" 의 6 을 `cfg.get("silence_auto_speech_margin_db", 6)` 로.
  - `detect_word_gaps(tr: dict, duration: float, cfg: dict, suspects: dict) -> list[Cut]` — level 100 에서만. 의심 단어를 뺀 단어들 사이(및 처음·끝) 틈이 `silence_min_sec` 이상이면 앞뒤 `silence_pad_sec` 남기고 `Cut(..., "(무음)", "gap", context, -1, words_idx=[])`. 영상 맨 앞/뒤는 여유 없이.
  - cuts.json `silence` 모드 값: level 이 있으면 `"level:N"`.
  - `diff._kind` 에서 `gap` 은 `silence` 로 보이게 (목록 `말 없는 구간`), `_is_silence` 도 `gap` 포함.

- [ ] **Step 1: 실패 테스트** (`test_silence_auto.py` 의 `make` 재사용)
  - `test_level_50_equals_default`: 합성 영상에서 `--silence on` 판정 컷과 level 50 판정 컷이 같음 (`silence_level_cfg(cfg, 50)` 적용 후 `auto_silence`+`detect_silences`).
  - `test_level_monotonic`: level 0,10,…,100 의 잘린 총 초가 줄지 않음 (배경 -28dB, 말 -24dB 합성).
  - `test_level_100_cuts_noisy_gap`: 배경 -26dB 틈 0.7초가 level 100 에서 `gap` 컷.
  - `test_cli_without_level_unchanged`: `build_parser().parse_args([x])` 의 `silence_level is None`.
- [ ] **Step 2: FAIL 확인** `pytest tests/test_silence_level.py -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: PASS 확인** + `pytest tests/test_silence_auto.py tests/test_diff.py -v`
- [ ] **Step 5: 실제 영상 확인** — 앱 작업 `~/Library/Application Support/youtube-edit/jobs/a6007c0b3e70` 을 scratch 폴더로 복사해 level 50/75/100 으로 `--review` (CLAUDE.md 의 실험 규칙: `--out <scratch> --review`, transcript 먼저 복사). level 50 컷이 기존 `1.cuts.json` 과 같고 100 에서 35.8~40.5초가 잘리는지 본다.
- [ ] **Step 6: Commit** `"파이프라인: 말 없는 구간 세기 --silence-level (50=기존, 100=단어 없는 틈 전부)"`

### Task 7: 파이프라인 — 배경 잡음 세기 `--denoise-level` + 미리보기 소리 바꿔 끼우기

**Files:**
- Modify: `scripts/pipeline.py` (`audio_filter_chain`, `audio_settings`, `prepare_render_audio`, `make_preview`, `process`, `build_parser`, `render_audio_note`)
- Test: `tests/test_denoise_level.py` (새로)

**Interfaces:**
- Produces:
  - `denoise_filters(level: int, model_rel: str) -> list[str]` — 0: `[]`; 1~50: `[f"arnndn=m={model_rel}:mix={0.85*level/50:.3g}"]` (50 → `mix=0.85` 로 지금 문자열과 정확히 같게); 51~100: `arnndn mix = 0.85+0.15*(level-50)/50` 와 `f"afftdn=nf=-25:nr={6+14*(level-50)/50:.3g}:tn=1"`. 값은 Step 5 에서 귀로 듣고 조정 가능(0·50 은 고정).
  - `audio_filter_chain(denoise, loudnorm, model_rel, offset, denoise_level: int | None = None)` — level 이 있으면 denoise 문자열 대신 `denoise_filters`. `audio_settings`/`prepare_render_audio` 도 같은 키워드 인자를 받는다 (캐시 키는 필터 문자열이라 자동으로 갈림).
  - 숨은 옵션 `--denoise-level N` (기본 None). 있으면 렌더 오디오와 **미리보기 오디오** 둘 다 이 세기로 (`prepare_render_audio(..., denoise_level=N)`). 분석용 `clean.wav`/`asr.wav` 는 그대로.
  - `make_preview` 를 두 단계로: 소리 없는 영상 사본 `work/preview_video.mp4`(원본 서명으로 캐시) + 소리 합치기(`-c:v copy`, AAC). 소리만 바뀌면 영상 인코딩을 다시 하지 않는다. 오디오 전용 원본은 지금처럼 바로 m4a/mp4.
- [ ] **Step 1: 실패 테스트**
  - `test_level_50_matches_rnnoise`: `audio_filter_chain("rnnoise", True, m, 0, denoise_level=50) == audio_filter_chain("rnnoise", True, m, 0)`.
  - `test_level_0_no_denoise`: `arnndn`·`afftdn` 없음.
  - `test_level_increases_strength`: 30 의 mix < 50 의 mix; 80 에 `afftdn` 있음.
  - `test_preview_audio_swap_reuses_video` (ffmpeg 필요, `--denoise none` 합성 사인파 영상 2초): 레벨만 바꿔 다시 `make_preview` → `preview_video.mp4` 수정 시각 그대로, `preview.mp4` 는 새로 씀.
- [ ] **Step 2: FAIL 확인** `pytest tests/test_denoise_level.py -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: PASS 확인** + `pytest tests/test_pipeline_features.py -v`
- [ ] **Step 5: 귀로 확인** — 1.1 영상 원본(`/Users/gim-yeongseon/Downloads/1.mp4`)으로 level 0/30/50/80/100 렌더 오디오를 scratch 에 만들어 들어 보고 10 단위 차이가 들리게 값 조정. 0·50 은 바꾸지 않는다.
- [ ] **Step 6: Commit** `"파이프라인: 배경 잡음 세기 --denoise-level, 미리보기는 소리만 바꿔 끼움"`

### Task 8: 앱 옵션을 숫자 슬라이더로 (말 없는 구간·배경 잡음)

**Files:**
- Modify: `app/options.py`, `app/api.py` (`ALLOWED`, `set_option`, `get_result` 의 `preview_url`), `app/ui/app.js` (`OPTS`, `DEFAULTS`, `FAST`, `renderOptions`, `setOption`), `app/ui/style.css`
- Test: `tests/test_options.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: Task 6 `--silence-level`, Task 7 `--denoise-level`.
- Produces:
  - `JobOptions.silence: int = 50`, `JobOptions.denoise: int = 50`; `from_dict` 가 `"keep"`→0, `"remove"`→50, 숫자 문자열→int, 범위 밖은 0~100 으로 자름.
  - `FAST_KEYS = {"filler", "habit", "silence", "denoise"}` (잡음도 바꾸면 다시 판정 → 미리보기 소리 갱신).
  - `analysis_args`: silence 0 → `--silence off`; 아니면 `--silence on --silence-level N`; 항상 `--denoise-level N`. `render_args`: `--render-denoise` 대신 `--denoise-level N`.
  - `set_option` 은 `silence`/`denoise` 에 int(또는 숫자 문자열) 0~100 을 받는다.
  - `get_result` 의 `preview_url` 끝에 `?v=<preview.mp4 mtime_ns>` (같은 주소면 재생기가 다시 안 불러옴; `window.py` 는 이미 경로만 봄).
  - UI: 두 줄을 슬라이더 행으로 (`input type=range`, 값 숫자, 양 끝 글자, 설명 한 줄 — Global Constraints 문구). `input` 이벤트엔 숫자·설명만, `change`(손 뗌) 때 `setOption`. 말 없는 구간 설명의 `{곳}`·`{초}` 는 지금 결과 요약에서 `silence`/`gap` 컷 수·초를 센 값 (판정이 끝나면 갱신). `{N}초` 는 `1.0 - 0.7*level/50` 소수 1자리.
- [ ] **Step 1: 실패 테스트** — `test_old_options_migrate` (`keep`→0, `remove`→50), `test_analysis_args_levels` (0 → `--silence off`, 70 → `--silence-level 70`, `--denoise-level` 포함), `test_set_option_slider_rejudges` (`set_option(id, "denoise", 80)` → `rejudge` 호출), `test_set_option_rejects_out_of_range` (`"abc"` → ValueError, 150 → 100 으로 자름), `test_preview_url_has_version`, `test_slider_burst_rejudges_once_more` (`test_jobqueue.py` 의 rejudge 후속 테스트 방식으로 연속 3번 → 실행 2번).
- [ ] **Step 2: FAIL 확인** `pytest tests/test_options.py tests/test_api.py tests/test_jobqueue.py -v`
- [ ] **Step 3: 파이썬 구현 → PASS 확인**
- [ ] **Step 4: UI 구현** (목업 `sliders-v2.html` 배치)
- [ ] **Step 5: 앱 실행 확인** — `.venv/bin/python -m app` 로 1.1 영상 작업을 열어: 슬라이더를 끌 때 설명만 바뀌고, 놓으면 몇 초 안에 잘린 곳·요약이 바뀌고, 잡음 슬라이더를 놓으면 재생 위치 그대로 소리가 바뀌는지.
- [ ] **Step 6: Commit** `"앱: 말 없는 구간·배경 잡음을 0~100 슬라이더로"`

### Task 9: 화면 — 자막 탭·영상 위 자막·어절 고치기·찾아 바꾸기·줄 시간

**Files:**
- Modify: `app/ui/app.js` (`resultParts`, `cutRows` 주변, `player`), `app/ui/style.css`

**Interfaces:**
- Consumes: Task 3 Api (`get_captions`, `set_word`, `replace_text`, `set_line_time`), Task 5 `edits_dropped`.
- Produces (화면 동작, 스펙 3.1~3.3):
  - "잘린 곳" 자리에 탭 `잘린 곳 N` · `자막 N줄` (선택 탭은 `S.tab`, 기본 `잘린 곳`). 목록 스크롤 유지 방식은 지금 `.cut-list` 와 같게.
  - 자막 탭: 줄 = `시간` + 어절 버튼들. 시간 누르면 `player.clip(start, end, true)` + 그 줄 선택(아래에 줄 시간 편집기). 어절 누르면 그 자리 `<input>`: Enter/바깥 클릭 확정, Esc 취소, 빈칸 확정=빼기, 고친 어절은 `--restored` 색 + 입력칸 옆 `원래대로`.
  - 줄 시간 편집기: `시작 0:12.4 [◀0.1][0.1▶][지금 위치로]  끝 … [◀0.1][0.1▶][지금 위치로]  원래대로` → `set_line_time`.
  - 찾기 막대: `찾기 [ ] → [ ]  전부 바꾸기 · N곳`. 입력할 때 `replace_text(apply=false)` (150ms 디바운스)로 N·강조, 버튼은 `apply=true`.
  - 영상 위 자막: 재생기 아래에 지금 시간의 줄(`captions` 에서 `start<=t<end`, 고친 줄 시간 반영). 멈춤 상태에서만 어절 클릭 편집 (자막 탭과 같은 함수).
  - `edits_dropped` 면 결과 위에 안내 `받아쓰기가 바뀌어 고친 자막을 지웠습니다`.
  - 결과가 처리 중(`BUSY`)이면 편집 막기 (지금 되살리기 버튼과 같은 조건 `HAS_RESULT`).
- [ ] **Step 1: 구현**
- [ ] **Step 2: 앱 실행 확인** — 1.1 영상: 어절 하나 고치기 → 자막 탭·영상 위 둘 다 초록; `콜럼버스` → `Columbus` 전부 바꾸기 N곳; 줄 시작 "지금 위치로"; 옵션 슬라이더를 바꿔도 고친 것 유지; 저장 후 SRT 를 열어 고친 글자·시간 확인; `ffprobe` 로 mp4 자막 트랙(kor) 확인 (CLAUDE.md 렌더 검증 명령).
- [ ] **Step 3: Commit** `"화면: 자막 탭·영상 위 자막에서 어절 고치기, 찾아 바꾸기, 줄 시간 맞추기"`

### Task 10: 화면 — 타임라인 구간 고르기 (끌기 + 여기서 시작/끝)

**Files:**
- Modify: `app/ui/app.js` (`timeline`, `cutRows`), `app/ui/style.css`

**Interfaces:**
- Consumes: Task 4 Api (`add_range_op`, `remove_range_op`, `cut_items` 의 `manual`).
- Produces (스펙 3.6):
  - 타임라인을 끌면(4px 이상 움직임) 선택 구간, 그냥 누르면 지금처럼 이동. 양 끝 손잡이로 조정.
  - 선택이 있으면 막대 아래: `[여기서 시작] [여기서 끝]  0:15.3 ~ 0:26.5  [◀0.1][0.1▶](양 끝)  [자르기] [되살리기] [취소]`. 선택 없이 "여기서 시작" 만 눌러도 시작점이 잡힘.
  - "잘린 곳" 목록에 `직접 자름` 항목, 그 행의 `되살리기` = `remove_range_op`.
- [ ] **Step 1: 구현**
- [ ] **Step 2: 앱 실행 확인** — 1.1 영상: 22~23초 끌어 자르기 → 미리보기가 건너뛰고 목록에 `직접 자름`; 35.8~40.5 무음 컷 일부만 되살리기 → 그 부분만 재생; 저장 결과 길이가 요약과 1프레임 이내 (`ffprobe` 길이 비교).
- [ ] **Step 3: 전체 테스트** `.venv/bin/python -m pytest` → 전부 PASS
- [ ] **Step 4: Commit** `"화면: 타임라인에서 구간 골라 직접 자르기·되살리기"`

### Task 11: 문서

**Files:**
- Modify: `CLAUDE.md` (데스크톱 앱 숨은 옵션 목록에 `--silence-level`·`--denoise-level`·`--subs-edits`; 컷 판정 요약에 `gap`), `README.md` 의 앱 사용법 해당 부분.

- [ ] **Step 1: 고치기** — 사실만, 각 한두 줄.
- [ ] **Step 2: Commit** `"문서: 자막 고치기·구간 자르기·세기 슬라이더"`
