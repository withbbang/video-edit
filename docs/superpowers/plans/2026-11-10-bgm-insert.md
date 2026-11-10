# 음원(BGM) 넣기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 데스크톱 앱에서 결과 영상의 원하는 시점에 BGM 을 여러 개 넣고, 말소리 구간을 낮춘 음량 곡선(점과 직선)을 자동으로 받아 다듬은 뒤, 미리보기로 바로 듣고 결과 mp4/m4a 에 섞는다.

**Architecture:** 앱은 영상별 BGM 목록(`Job.bgms`, **결과 초** 기준)을 들고 편집·되돌리기·저장한다. 손대지 않은 BGM 은 `points=None` 으로 두고,
곡선은 그때그때 지금 컷으로 계산한다(`app/bgm.py`) — 그래서 컷이 바뀌면 저절로 다시 깔린다. 저장 때 곡선을 풀어 `out/<이름>.bgm.json` 을 만들고
파이프라인 숨은 옵션 `--bgm` 이 컷 적용한 말소리(`cut.wav`) 위에 numpy 로 섞는다. 화면은 원본 초 축의 확대 타임라인에 결과 시간을 펼쳐(timeMap) 음원 줄을 그리고,
미리보기는 Web Audio 로 재생기(결과 시간)에 맞춰 섞는다.

**Tech Stack:** Python 3.11 + numpy, ffmpeg(디코드·`ebur128`), pywebview, 바닐라 JS (`app/ui`, Web Audio), pytest + node.

**Spec:** `docs/superpowers/specs/2026-11-10-bgm-insert-design.md`

## Global Constraints

- `pipeline.py` CLI 기본 동작은 그대로. `--bgm` 은 앱 전용 옵션, 없으면 지금과 똑같은 명령·결과.
- 시간 기준: BGM 의 `at` 은 **결과 영상 초**. 앞쪽 편집이 바뀌어도 `at` 은 바뀌지 않는다. 점은 **곡 초**.
- 곡선: 점 `[[곡 초, dB], …]` 사이 **dB 직선 보간**, 첫 점 앞·끝 점 뒤는 그 값. `dB <= -40` 은 무음(배율 0). 세로 축 +6dB ~ -40dB(무음).
  배율 = `10**((dB + gain)/20)` (`gain` = 곡 크기 맞춤 보정). 미리보기·렌더 둘 다 이 규칙.
- 곡 크기 맞춤: 불러올 때 통합 라우드니스를 재서 `gain = clamp(-24 - I, -30, +12)` dB (I 를 못 재면 0).
- 자동 곡선 상수(`app/bgm.py` 한 곳): `AUTO_BASE_DB = 0`, `AUTO_DUCK_DB = -15`, `AUTO_RAMP = 0.3`, `AUTO_FADE = 0.5`, `SPEECH_JOIN = 0.5`.
- 곡보다 길게 늘리기·반복 없음: `0 <= in < out <= dur`. 최소 길이 0.1초.
- 겹치는 BGM 은 둘 다 섞임(밀거나 막지 않음). 결과 길이 밖은 버림 — 소리가 영상보다 길어지면 안 됨.
- BGM 은 말소리 잡음 제거·loudnorm **뒤**에 더한다. 전사·컷 판정·자막에 영향 없음. `--preview-cut-audio` 소리에는 섞지 않는다(화면이 섞음).
- 편집은 확인 대기·저장됨에서만, `실행 취소` 대상, 저장됨이면 확인 대기로 (`_change_overlays` 와 같은 모양). 다시 판정은 하지 않는다.
- "손댐" = 점을 추가·끌기·지우기 (`points` 를 patch). 옮기기(`at`)·양끝 자르기(`in`/`out`)·이름은 손댐 아님.
- 고른 파일은 작업 폴더 `bgm/<sha16>.m4a` 로 **AAC 48kHz 스테레오로 다시 인코딩**해 둔다(웹뷰가 어떤 형식이든 디코드할 수 있게, 렌더도 이 파일). 보관함 `data_dir()/bgm_presets/`(`presets.json` + `<sha16>.m4a`), 파일·이름·길이·gain 만.
- 받는 파일: 확장자 `.mp3 .m4a .aac .wav .flac .ogg .opus`, ffprobe 로 소리 스트림이 있고 영상 스트림(앨범 아트 제외)이 없을 것.
- 새 화면 문구: 탭 `음원`, `사용 중`·`보관함`, `불러오기`, `이름 변경`·`보관`·`제거`, `삽입`, 버튼 `음원 자동 조절`, 그 말풍선
  `말소리가 나오는 구간은 BGM을 낮추고, 말이 없는 구간은 다시 올리는 음량 선을 새로 깔아요. 손으로 고친 점은 지워져요.` — 앱 스펙 3장에 적는다.
- 고친 뒤엔 `.venv/bin/python -m pytest` 전체. 실행 중인 앱은 `app/ui` 를 다시 읽지 않는다.

## Review Focus

1. **BGM 이 결과 끝을 넘거나 앞쪽 컷으로 결과가 짧아져 `at` 이 결과 끝 뒤가 됨** — 렌더는 넘는 부분을 버리고 길이 차 1프레임 이내, 화면은 끝에서 잘라 그리고 흐리게 (Task 1·5 테스트).
2. **BGM 막대가 잘린 구간(빗금)을 가로지름** — 곡선은 빗금 위에서 평평, 빗금 안을 눌러 점을 찍으면 그 구간 끝의 결과 시간으로 (Task 5 테스트).
3. **겹친 두 곡 + 큰 말소리로 합이 넘침** — 클리핑 대신 부드러운 제한(soft limit), 겹치지 않는 곳의 말소리 샘플은 그대로 (Task 1 테스트).
4. **손댄 BGM 은 컷이 바뀌어도 그대로, 손대지 않은 것은 따라 바뀜** — get_result 를 컷 변경 전후로 불러 비교 (Task 3 테스트).
5. **소리 파일이 아님/영상 파일/깨진 파일, 작업 폴더 복사 뒤 원본 삭제** — 거절(None), 렌더는 복사본으로 (Task 4 테스트).

---

### Task 1: 파이프라인 — `--bgm` 으로 섞기

**Files:**
- Modify: `scripts/pipeline.py` (`cut_audio` 아래에 새 함수, 인자 `--overlays` 옆, 렌더 흐름 `cut_audio(render_wav, …)` 바로 뒤, `write_report`)
- Test: `tests/test_bgm_render.py` (새 파일)

**Interfaces:**
- Produces:
  - `BGM_FLOOR_DB = -40.0`
  - `bgm_db_at(points: list[list[float]], t: np.ndarray) -> np.ndarray` — 곡 초 배열 → dB (규칙은 Global Constraints). 점이 없으면 0.
  - `bgm_gain(points, gain_db: float, t: np.ndarray) -> np.ndarray` — 배율. `bgm_db_at <= BGM_FLOOR_DB` 인 곳은 0.
  - `load_bgms(path: Path | None) -> list[dict]` — json `[{path, at, in, out, gain, points}]`. 없음·형식 틀림·파일 없음 항목은 로그 남기고 뺀다.
  - `soft_limit(x: np.ndarray) -> np.ndarray` — int16 범위 float. `|x| <= 0.9*32767` 은 그대로, 넘는 부분은 `0.9*FS + 0.1*FS*tanh((|x|-0.9*FS)/(0.1*FS))` (부호 유지).
  - `mix_bgm(wav: Path, bgms: list[dict]) -> int` — `cut.wav` 를 **제자리에서** 고친다(np.memmap, `wave` 가 쓴 44바이트 머리 뒤 int16). 곡마다 ffmpeg 로
    `-ss in -t (out-in)` 를 wav 와 같은 샘플레이트·채널 s16le 로 받아 `bgm_gain(points, gain, in + i/sr)` 를 곱해 결과 샘플 `round(at*sr)` 부터 더한다.
    wav 길이 밖은 버림. 더한 구간만 `soft_limit`. 섞은 곡 수를 돌려준다.
  - CLI `--bgm <json>` (help 에 앱 전용임을 적음). `write_report(..., bgms: list[dict] | None = None)` → "BGM" 절에 곡마다 `파일 이름 · 결과 시작초 · 길이초` 한 줄.
- 흐름: `n_samples, sr = cut_audio(...)` 뒤 `bgms = load_bgms(args.bgm)`; 있으면 `mix_bgm(cut_wav, bgms)` 하고 `log(f"BGM {n}곡을 섞음")`. 영상·소리만 출력 둘 다 그 cut.wav 를 쓰므로 따로 손볼 곳 없음.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
import numpy as np, pipeline as pl

def test_db_interp_and_floor():
    pts = [[1.0, 0.0], [2.0, -20.0]]
    db = pl.bgm_db_at(pts, np.array([0.0, 1.0, 1.5, 2.0, 9.0]))
    assert db.tolist() == [0.0, 0.0, -10.0, -20.0, -20.0]
    g = pl.bgm_gain([[0.0, -40.0]], 0.0, np.array([0.5]))
    assert g.tolist() == [0.0]
    assert pl.bgm_gain([], 6.0, np.array([0.0]))[0] == pytest.approx(10 ** (6 / 20))

def test_soft_limit():
    x = np.array([1000.0, -29000.0, 40000.0, -90000.0])
    y = pl.soft_limit(x)
    assert y[0] == 1000.0 and y[1] == -29000.0
    assert 29490 < y[2] < 32767 and -32767 < y[3] < -29490
```

  실제 ffmpeg 테스트 `test_mix_bgm_places_and_trims(tmp_path)`: `wave` 로 48k 모노 무음 2초 `cut.wav`, ffmpeg `sine=f=440:d=3` 로 `song.m4a`.
  `mix_bgm(cut, [{path: song, at: 1.5, in: 0.5, out: 3.0, gain: 0.0, points: [[0, 0]]}])` → 1.5초 전 샘플은 전부 0, 1.5초 뒤 RMS > 0, 파일 길이(프레임 수) 그대로(넘는 1.0초는 버림).
  `points: [[0, -40]]` 이면 전부 0. 그리고 `test_load_bgms_skips_missing` — 없는 path 항목은 빠지고 나머지는 남음.
  `test_render_with_bgm_audio_only(tmp_path)` — `--denoise none` 으로 `lavfi sine` 4초 m4a 입력 + `--bgm` 1곡을 `pipeline.main` 서브프로세스로 렌더해(기존 렌더 테스트의 실행 도우미를 따름), 결과 길이가 BGM 없을 때와 1프레임 이내로 같고 report.md 에 `BGM` 절이 있음.

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_bgm_render.py -v` → `AttributeError: bgm_db_at`
- [ ] **Step 3: 구현** — 위 Interfaces 대로. 곡 디코드는 `run([...], capture)` 의 stdout 바이트를 `np.frombuffer(..., int16)`.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 전체 pytest PASS
- [ ] **Step 5: 커밋** — `git commit -m "파이프라인: --bgm 으로 컷 뒤 소리에 BGM 섞기"`

---

### Task 2: 곡선·시간 계산 — `app/bgm.py`

**Files:**
- Create: `app/bgm.py` (순수 함수 + 불러오기 도우미는 Task 4)
- Test: `tests/test_bgm.py` (새 파일)

**Interfaces:**
- Produces:
  - 상수 `AUTO_BASE_DB, AUTO_DUCK_DB, AUTO_RAMP, AUTO_FADE, SPEECH_JOIN` (Global Constraints 값), `DB_MIN = -40.0`, `DB_MAX = 6.0`, `MIN_LEN = 0.1`.
  - `keeps_from_cuts(cuts: list[list[float]], duration: float, fps: float) -> list[list[float]]` — 겹친 컷을 합치고 `[0, duration]` 의 나머지를 프레임 격자(`round(t*fps)/fps`)에 맞춘 남길 구간(원본 초). 길이 0 구간은 뺌.
  - `to_out(t: float, keeps) -> float` — `player.js` `timeMap.toOut` 과 같은 규칙. `out_total(keeps) -> float`.
  - `speech_spans(words: list[dict], keeps) -> list[list[float]]` — 단어 `{start, end}`(원본 초)마다 keeps 와 겹친 부분을 결과 초로 옮기고 정렬·합침, 틈 `< SPEECH_JOIN` 도 합침.
  - `auto_points(at: float, inn: float, out: float, speech: list[list[float]]) -> list[list[float]]` — 곡 초 점 목록:
    BGM 결과 구간 `[at, at+out-inn]` 안에서, 말 구간 `[s, e]` 마다 `(s-AUTO_RAMP, BASE) → (s, DUCK) → (e, DUCK) → (e+AUTO_RAMP, BASE)`(BGM 구간으로 자르고 겹치는 램프는 합침),
    양끝 `(at, DB_MIN) → (at+AUTO_FADE, BASE)`, `(end-AUTO_FADE, BASE) → (end, DB_MIN)` (BGM 이 `2*AUTO_FADE` 보다 짧으면 가운데 한 점). 결과 초 `x` → 곡 초 `inn + (x - at)`. 시간 순, 같은 시간은 하나.
    페이드와 말 구간이 겹치면 각 시점의 dB 는 두 곡선 중 **작은 값**.
  - `resolve_points(b: dict, speech) -> list[list[float]]` — `b["points"]` 가 있으면 그것, None 이면 `auto_points(b["at"], b["in"], b["out"], speech)`.
  - `db_at(points, t: float) -> float` — Task 1 `bgm_db_at` 과 같은 규칙(스칼라).

- [ ] **Step 1: 실패하는 테스트**

```python
from app import bgm

def test_keeps_and_to_out():
    k = bgm.keeps_from_cuts([[1.0, 2.0], [1.5, 3.0]], 10.0, 30)
    assert k == [[0.0, 1.0], [3.0, 10.0]]
    assert bgm.to_out(2.0, k) == 1.0 and bgm.to_out(4.0, k) == 2.0 and bgm.out_total(k) == 8.0

def test_speech_spans_follow_cuts_and_join():
    k = [[0.0, 1.0], [3.0, 10.0]]
    words = [{"start": 0.5, "end": 0.9}, {"start": 3.2, "end": 3.5}, {"start": 5.0, "end": 5.5}]
    assert bgm.speech_spans(words, k) == [[0.5, 1.5], [3.0, 3.5]]   # 0.9→… 틈 0.3 < 0.5 로 합침

def test_auto_points_duck_and_fade():
    pts = bgm.auto_points(10.0, 2.0, 22.0, [[14.0, 16.0]])   # 결과 10~30초, 곡 2~22초
    d = dict((round(t, 3), db) for t, db in pts)
    assert d[2.0] == -40.0 and d[2.5] == 0.0               # 페이드 인
    assert d[5.7] == 0.0 and d[6.0] == -15.0 and d[8.0] == -15.0 and d[8.3] == 0.0
    assert d[22.0] == -40.0
    assert bgm.auto_points(0.0, 0.0, 0.6, []) == [[0.0, -40.0], [0.3, 0.0], [0.6, -40.0]]

def test_resolve_keeps_manual():
    b = {"at": 0, "in": 0, "out": 5, "points": [[1, -3]]}
    assert bgm.resolve_points(b, [[0, 5]]) == [[1, -3]]
```

  그리고 `test_db_at_matches_pipeline` — 임의 점 3개·시각 20개에서 `bgm.db_at` == `pipeline.bgm_db_at` (scripts 를 sys.path 에 넣는 기존 테스트 방식대로).
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_bgm.py -v`
- [ ] **Step 3: 구현** — numpy 없이 순수 파이썬(앱 쪽).
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱: BGM 결과 시간·자동 음량 곡선 계산"`

---

### Task 3: 앱 모델 — `Job.bgms`, 저장·렌더로 넘기기, `get_result`

**Files:**
- Modify: `app/store.py` (Job), `app/api.py` (`save_bgms`, `pipeline_command`, `_snap`/`_put`, `get_result`), `app/runner.py` (`render_command`)
- Test: `tests/test_bgms.py` (새 파일 — Task 4 테스트도 여기)

**Interfaces:**
- Consumes: Task 1 json 형식, Task 2 `keeps_from_cuts`/`speech_spans`/`resolve_points`
- Produces:
  - `Job.bgms: list` — 항목 `{"id": 8 hex, "file": "<sha16>.m4a", "name": str, "dur": float, "gain": float, "at": float, "in": float, "out": float, "points": list | None}` (`points is None` = 손대지 않음 = 스펙의 `touched=false`)
  - `Api._bgm_context(job) -> tuple[keeps, speech, fps]` — 지금 effective 컷·전사로 계산 (get_result 와 같은 effective 컷).
  - `save_bgms(job, lock=None, context=None) -> Path | None` — `out/<stem>.bgm.json` 에 `{path: job_dir/bgm/<file> 절대 경로, at, in, out, gain, points: resolve_points(...)}`. 비면 낡은 파일 지우고 None.
  - `runner.render_command(..., bgm_path=None)` → 있으면 `["--bgm", str(bgm_path)]`
  - `_snap`/`_put` 튜플 끝에 `job.bgms`
  - `get_result` 에 `"bgms": [화면용 항목]`, `"keeps": keeps`, `"out_total": out_total(keeps)`. 화면용 항목 = 저장 항목 + `"url"` + `"points"`(풀린 값) + `"touched": bool`.

- [ ] **Step 1: 실패하는 테스트** — `test_job_bgms_roundtrip`(옛 jobs.json 에 키 없으면 `[]`), `test_render_command_passes_bgm`(json 의 path 가 `job_dir/bgm/<file>`, 손대지 않은 BGM 은 자동 점이 풀려 들어감),
  `test_render_command_without_bgm`(옵션 없음, 낡은 json 지워짐),
  `test_untouched_follows_cuts_touched_stays` — 전사·cuts.json 을 갖춘 기존 픽스처로 BGM 둘(하나 `points=None`, 하나 손댐)을 넣고 `get_result` → `add_range_op(cut)` 로 말 구간 앞을 잘라 다시 `get_result`:
  손대지 않은 것의 점은 바뀌고, 손댄 것은 같고, 둘 다 `at` 은 그대로.
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_bgms.py -v`
- [ ] **Step 3: 구현** — `save_overlays` 와 같은 모양. `pipeline_command` RENDER 에서 `save_bgms(job, lock)` 를 넘김.
- [ ] **Step 4: 통과 확인** — 같은 명령 + `tests/test_history.py tests/test_runner.py` PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱: 영상별 BGM 목록을 저장 때 파이프라인으로 넘김"`

---

### Task 4: Api — 불러오기·고치기·지우기·자동 조절, 보관함, 미리보기 서버

**Files:**
- Modify: `app/bgm.py` (`import_audio`), `app/api.py` (Dialogs 프로토콜, 새 메서드), `app/window.py` (`open_audio` 대화상자, 서버 경로)
- Test: `tests/test_bgms.py`, `tests/test_window.py`

**Interfaces:**
- Consumes: Task 3 `Job.bgms`, `_snap`, `_bgm_context`
- Produces (편집은 확인 대기·저장됨에서만, 안 되면 None, 성공 → `{"bgms": [화면용 항목], "history": {...}}`):
  - `bgm.AUDIO_EXTS`, `bgm.import_audio(src: Path, folder: Path) -> dict | None` — ffprobe: 소리 스트림 있음·영상 스트림 없음(attached_pic 제외)이 아니면 None.
    `sha1(파일)[:16]`.m4a 로 `ffmpeg -i src -vn -ac 2 -ar 48000 -c:a aac -b:a 192k` (이미 있으면 다시 안 함, 임시 이름 → `os.replace`).
    `ffmpeg -i <m4a> -af ebur128 -f null -` 의 마지막 `I:` 값으로 gain. → `{"file", "dur", "gain"}`.
  - `Dialogs.open_audio() -> str | None` (`window.py`: `file_types=("음원 (*.mp3;*.m4a;…)",)`)
  - `Api.add_bgm(job_id, at: float) -> dict | None` — 대화상자 → `import_audio` → `at` 은 `[0, out_total]` 안으로·프레임 격자, `in=0, out=dur, points=None`, 이름 = 파일 이름(확장자 뺌).
  - `Api.update_bgm(job_id, bid, patch: dict)` — 키 `at, in, out, points` 만. 수는 유한, `at >= 0`, `0 <= in < out <= dur`, `out - in >= MIN_LEN`.
    `points` 는 `[[t, db], …]`(t 는 `[0, dur]`, db 는 `[DB_MIN, DB_MAX]` 로 고정해 받음, t 로 정렬해 저장, 최대 500개). `points` 가 들어오면 손댐.
  - `Api.reset_bgm(job_id, bid)` — `points=None` (`음원 자동 조절`). `Api.rename_bgm(job_id, bid, name)`, `Api.delete_bgm(job_id, bid)`.
  - 보관함: `list_bgm_presets() -> list[dict]`, `save_bgm_preset(job_id, bid, name)`, `rename_bgm_preset(pid, name)`, `delete_bgm_preset(pid)`, `insert_bgm_preset(job_id, pid, at)` — 이미지 보관함(`list_presets` 등)과 같은 모양·이름 규칙(`PRESET_NAME_MAX`, 같은 이름이면 거절은 이미지 탭과 똑같이). 항목 `{id, name, file, dur, gain, url}`.
  - 미리보기 서버: `/<id12>/bgm/<16hex>.m4a` (jobs 아래), `/bgm_presets/<16hex>.m4a`. Content-Type `audio/mp4`(이미 있음). Range 지원은 기존 그대로.
- [ ] **Step 1: 실패하는 테스트**
  - `test_add_bgm_imports_and_defaults` — FakeDialogs 에 `audio=` (ffmpeg `sine` 3초 mp3). 항목 `in == 0`, `out ≈ 3`, `points is None`, 화면용 `touched is False`, `bgm/<16hex>.m4a` 존재, 원본 지워도 `save_bgms` 경로 파일 존재, gain 은 [-30, 12].
  - `test_add_bgm_rejects_video_and_garbage` — `lavfi testsrc` mp4, `.mp3` 이름의 텍스트 파일 → None.
  - `test_update_bgm_validates` — `out > dur`, `in >= out`, 길이 0.05, 모르는 키, NaN, `at < 0` → None. `points` 넣으면 `touched`, db 999 는 6 으로.
  - `test_reset_and_undo` — 점 고침 → `reset_bgm` → `touched False` → undo 두 번이면 add 직후 상태, SAVED 에서 고치면 REVIEW.
  - `test_bgm_presets_roundtrip` — 이미지 보관함 테스트와 같은 흐름 (`YTEDIT_DATA_DIR` 은 tmp).
  - `tests/test_window.py` — `bgm/<16hex>.m4a`·`bgm_presets/<16hex>.m4a` 를 내주고 `bgm/../x` 는 404.
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_bgms.py tests/test_window.py -v`
- [ ] **Step 3: 구현** — 편집 공통 `_change_bgms(job_id, change: Callable[[Job, float], bool])` (두 번째 인자 = `out_total`), `_change_overlays` 와 같은 모양.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 전체 pytest PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱: BGM 불러오기·고치기·자동 조절·보관함 Api"`

---

### Task 5: 화면 순수 계산 — 음원 줄 (`timeline.js`)

**Files:**
- Modify: `app/ui/timeline.js` (TL 에 더함)
- Test: `tests/test_timeline_js.py`

**Interfaces:**
- Consumes: `get_result` 의 `keeps`, `out_total`, `bgms`, 조각 띠(`clipBands` 와 같은 조각 목록)
- Produces (map = `player.js` 의 `timeMap(keeps)` 객체를 인자로 받음 — timeline.js 는 player.js 를 부르지 않는다):
  - `dbAt(points, t) -> number` — Task 2 `db_at` 과 같은 규칙.
  - `dbToY(db, h) / yToDb(y, h)` — 줄 높이 h 안에서 위 `DB_MAX=6`, 아래 `DB_MIN=-40`, dB 직선 축. `yToDb` 는 범위로 고정.
  - `bgmLanes(bgms) -> {[id]: lane}` — `at` 순(같으면 id 순)으로 겹치지 않는 가장 위 줄에 넣음. 겹침 판정은 결과 구간 `[at, at+out-in)`.
  - `bgmSpan(b, map) -> {o0, o1, cut}` — 원본 축 구간 `toOrig(at)` ~ `toOrig(min(at+len, total))`, `cut = at+len > total` (넘침 표시용). `at >= total` 이면 null.
  - `bgmPolyline(b, points, map, v, cw, h) -> [[x, y], …]` — 결과 시간을 keeps 조각별로 원본 축에 펼친 꺾은선. 잘린 구간 위는 앞 조각 끝 값으로 **평평한 선**.
  - `bgmSnap(at, len, total, others: number[], pxPerSec, thr = 8) -> number` — `at` 과 `at+len` 중 하나가 대상(0, `total`(끝 맞춤: `total - len`), others 의 경계)과 화면상 `thr` px 안이면 붙임(가장 가까운 것), 아니면 그대로. 결과는 `>= 0`.
  - `bgmDrag(kind, b, dt, total) -> {at, in, out}` — `kind`: `'move'|'start'|'end'`. move = `at+dt`(≥0). start = `in+dt` 를 `[0, out-0.1]` 로, `at` 도 같은 양만큼(곡의 나머지 위치 그대로, `at >= 0` 이 되게 in 도 제한). end = `out+dt` 를 `[in+0.1, dur]` 로. 늘리기는 dur 에서 막힘.
  - `xToOutT(x, map, v, cw) -> number` — 화면 x → 원본 초 → `map.toOut` (빗금 안이면 그 구간 끝의 결과 시간).
- [ ] **Step 1: 실패하는 테스트** — (`timeMap` 은 `require(player.js)` 로 만든다)
  - `bgmLanes`: 0~5, 3~8, 6~9 → lane 0, 1, 0.
  - `bgmSpan`: keeps `[[0,1],[3,10]]`, BGM `at=0.5, len=2` → `o0=0.5, o1=4.5`; `at=7.5, len=3` → `cut true, o1=10`; `at=9` → null.
  - `bgmPolyline`: 위 keeps 에서 점 `[[0,0],[2,-20]]`, `at=0,in=0,out=2` → 원본 1~3초 사이 두 점의 y 가 같음(평평).
  - `bgmSnap`: total 20, len 4, at 15.9, pxPerSec 10 → 16(끝 맞춤); at 0.5 → 0; others [7] 에 at 2.95 → 3(끝이 7 에 붙음); 멀면 그대로.
  - `bgmDrag`: dur 10, `in=2,out=6,at=5`: `end +9` → `out=10`; `start -5` → `in=0, at=3`; `start +9` → `in=5.9`; `move -9` → `at=0`.
  - `dbAt` 이 Task 2 `bgm.db_at` 과 같은 값(파이썬에서 같은 입력으로 비교).
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_timeline_js.py -v`
- [ ] **Step 3: 구현** — 다른 TL 함수처럼 순수 함수.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱 화면: 음원 줄 자리·곡선·끌기 계산"`

---

### Task 6: 미리보기 섞기 — `app/ui/bgmmix.js`

**Files:**
- Create: `app/ui/bgmmix.js` (`index.html` 에서 `timeline.js` 뒤에 불러옴)
- Test: `tests/test_bgmmix_js.py` (새 파일, node — 순수 부분만)

**Interfaces:**
- Consumes: `get_result.bgms`(url, at, in, out, gain, points), 재생기 `<video>`(결과 시간), `TL.dbAt`
- Produces:
  - `bgmPlan(bgms, t, total) -> [{id, wait, offset, dur}]` — 재생 위치 t 에서 아직 끝나지 않은 BGM 마다: `wait = max(0, at - t)`, `offset = in + max(0, t - at)`, `dur` = 결과 끝(`total`)을 넘지 않는 남은 길이. 끝났거나 `dur <= 0` 이면 뺌.
  - `gainCurve(points, gainDb, from, dur, step = 0.05) -> Float32Array` — 곡 초 `from` 부터 step 간격 배율(`DB_MIN` 이하 0). `setValueCurveAtTime` 에 넣는다.
  - `class BgmMixer { constructor(video); set(bgms, total); dispose() }` — AudioContext 하나, url 별 `decodeAudioData` 버퍼 캐시.
    video 의 `play`/`playing`/`pause`/`seeking`/`ratechange`/`volumechange` 와 `set()` 때 모든 소스를 멈추고, 재생 중이면 `bgmPlan` 대로 소스+GainNode 를 다시 예약.
    1초마다 예상 위치와 video.currentTime 차가 0.05초 넘으면 다시 예약. video 의 `muted`/`volume` 을 마스터 GainNode 에 반영. 재생 속도가 1 이 아니면 `playbackRate` 도 맞춘다.
- [ ] **Step 1: 실패하는 테스트** — `bgmPlan([{id:'a',at:2,in:1,out:5}], 0, 100)` → `wait 2, offset 1, dur 4`; t=4 → `wait 0, offset 3, dur 2`; t=7 → `[]`; total 3 → `dur 1`.
  `gainCurve([[0,0],[1,-40]], 0, 0, 1, 0.5)` → 곡 초 0, 0.5, 1 의 dB 0, -20, -40 → `[1, 0.1, 0]` (dB 보간이라 가운데는 0.5 가 아니라 0.1). `gainDb=6` 이면 첫 값 `10**(6/20)`.
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_bgmmix_js.py -v`
- [ ] **Step 3: 구현** — 순수 함수는 `module.exports`, 클래스는 window 에만.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱 화면: BGM 미리보기 섞기 (Web Audio)"`

---

### Task 7: 화면 — `음원` 탭, 음원 줄, 막대 편집

**Files:**
- Modify: `app/ui/app.js` (이미지 탭 옆에 음원 탭, `zoomTl` 에 음원 줄, 재생기에 `BgmMixer` 연결, 키보드 Delete), `app/ui/style.css`, `app/ui/index.html`
- Modify: `docs/superpowers/specs/2026-10-22-desktop-app-design.md` 3장(문구), `CLAUDE.md` (데스크톱 앱 절: `--bgm`, 음원 탭·줄, `bgm_presets` 위치, `points=None` 규칙)
- Test: 수동 검증 단계 (가능하면 `tests/test_e2e_app.py` 에 탭 열기·불러오기 한 건)

**Interfaces:**
- Consumes: Task 4 Api, Task 5 TL, Task 6 `BgmMixer`, `S.result.bgms/keeps/out_total`
- Produces: 없음 (최종 화면)

- [ ] **Step 1: 음원 탭** — 이미지 탭(`app.js` 1102~ 근처)과 같은 구성: `사용 중`·`보관함`, `불러오기`(→ `add_bgm(id, 재생 위치(결과 초))`), 고른 음원 `이름 변경`·`보관`·`제거`, 보관함 `삽입`(재생 위치), 이름 두 번 누르기로 이름 바꾸기
  (CLAUDE.md 의 `h()` dblclick 주의 — 이미지 탭 방식 그대로). 탭 이름 `음원 N`. 소리만 있는 입력에서도 보인다.
- [ ] **Step 2: 음원 줄** — 이미지 줄과 같은 패널(높이 손잡이·스크롤 공유)에 `TL.bgmLanes` 줄 수만큼. 막대 = `TL.bgmSpan`, 곡선 = `TL.bgmPolyline`(SVG). 넘침(`cut`)은 끝을 흐리게.
  고른 BGM(`S.bgmSel`)만 active, 나머지 inactive(흐리게) — 영상 띠 고르기와 같은 모양. 고른 막대에만 점·`음원 자동 조절` 버튼(`title` = 말풍선 문구).
- [ ] **Step 3: 끌기·점 편집** — 몸통 4px 넘게 끌기(`TL.isDrag`) = 옮기기, 양끝 6px = 자르기. 끄는 동안 `TL.bgmDrag` → `TL.bgmSnap`(others = 끼워 넣은 영상 띠 경계를 `map.toOut` 한 값 + 다른 BGM 시작·끝, pxPerSec = 지금 보기 배율)
  으로 화면만 움직이고, pointerup 에 `update_bgm` 한 번. 고른 막대의 선 위를 누르면 `TL.xToOutT` → 곡 초 = `in + (t - at)`, dB = `TL.yToDb` 로 점 추가,
  점 끌기는 이웃 점 사이·막대 안으로 고정, 점 두 번 누르기 또는 고른 점 + Delete/Backspace(입력 중이 아닐 때) = 지우기 — 모두 끝날 때 `update_bgm(…, {points})`.
  BGM 이 골라져 있고 점이 안 골라졌을 때 Delete 는 아무것도 안 함(제거는 탭의 `제거` 버튼).
- [ ] **Step 4: 미리보기** — 재생기가 준비되면 `new BgmMixer(video)`, `loadResult`·편집 응답마다 `mixer.set(bgms, out_total)`. 미리보기를 바꾸거나 작업을 닫으면 `dispose()`.
- [ ] **Step 5: 문서** — 스펙 3장에 새 문구·조작, CLAUDE.md 에 한두 줄씩.
- [ ] **Step 6: 확인** — `.venv/bin/python -m pytest` 전체 PASS. 앱을 껐다 띄워(`.venv/bin/python -m app`) 분석된 영상에서:
  불러오기 → 자동 곡선이 말 구간에서 내려감 → 재생해 섞여 들림 → 점 끌기 즉시 반영 → 앞쪽 컷 되살리기 후 손대지 않은 BGM 곡선이 따라 바뀌고 `at` 그대로 →
  손댄 뒤 컷 바꾸면 그대로 → `음원 자동 조절` → 끝으로 끌어 끝 맞춤·영상 띠 경계 붙음 → 두 곡 겹쳐 둘째 줄·섞여 들림 → 오른쪽 끝 늘리기가 곡 길이에서 막힘 →
  실행 취소 → 보관·다른 영상에 삽입 → 저장 후 결과를 CLAUDE.md 의 ffprobe 명령으로 확인(영상·오디오 길이 차 1프레임 이내)하고 들어 봄. 각 단계 결과를 적어 둔다.
- [ ] **Step 7: 커밋** — `git commit -m "앱 화면: 음원 탭·음원 줄·BGM 음량 곡선 편집"`
