# 프레임 타임라인 (필름스트립 + 소리 파형) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 결과 화면 타임라인 아래에 확대 가능한 프레임 썸네일 줄과 소리 파형 줄을 붙여, 1시간 넘는 영상에서도 프레임 단위로 장면을 보며 구간을 고르게 한다.

**Architecture:** 서버는 미리보기 영상(`jobs/<id>/preview/video.mp4`, 모든 프레임 기준 프레임)에서 ffmpeg 로 1초 간격 썸네일 타일 시트를
백그라운드 일꾼(`app/thumbs.py` `Thumbs`)이 한 장씩 만들고, 분석용 오디오 에너지로 파형 바이트열을 만들어 캐시한다.
화면은 보기 범위 계산을 순수 함수 파일(`app/ui/timeline.js`)에 두고, `app.js` 가 canvas 두 줄(화면·소리)을 그리며 기존 구간 조작을
그대로 붙인다. 시트 간격보다 확대하면 숨은 `<video>` 로 그 프레임을 직접 그린다.

**Tech Stack:** Python 3 (pytest, numpy), ffmpeg 9.x (`fps`·`scale`·`tile` 필터), pywebview HTML/JS (canvas), node (JS 순수 함수 테스트).

**Spec:** `docs/superpowers/specs/2026-10-28-filmstrip-timeline-design.md` (+ 화면 문구는 `docs/superpowers/specs/2026-10-22-desktop-app-design.md` 3.3)

## Global Constraints

- 썸네일은 **지금 열어 본 영상만** 만든다 (`get_result` 가 요청). 다른 영상을 열면 만들던 것은 멈추고 만든 시트는 캐시에 남긴다.
- 분석·저장과 동시에 돌리되 ffmpeg `-threads 2` + 낮은 우선순위. 같은 영상의 분석 전에는 멈추고 끝나면 다시 받는다 (`CutAudio` 와 같은 cancel/release).
- `CutAudio` 와 일꾼을 따로 둔다.
- 영상을 목록에서 빼면 멈춘다 (캐시는 `jobqueue` 가 작업 폴더째 지운다).
- 전체 막대의 조작(클릭 이동·끌기·손잡이·Shift+클릭 두 번)은 그대로. 노란 틀(보기 범위)은 표시만.
- 화면 줄·소리 줄에서도 같은 조작 + ←/→ 1프레임(Shift 10)·I/O. 구간 끝은 원본 `fps` 격자 (`snapT`·`makeRange` 재사용).
- 가장 확대 = 한 칸 1프레임(원본 `fps`), 가장 축소 = 영상 전체.
- 1시간 영상 썸네일 캐시는 수십 MB 안쪽.
- 옛 작업(`preview_url` 만)·영상 없는 입력: 화면 줄 숨김. `asr.wav` 없으면 소리 줄 숨김.
- 결과 렌더·SRT 는 바꾸지 않는다. 윈도우(WebView2)에서도 동작.
- 화면 문구는 스펙 3.3 에 적고 그대로 쓴다. 파이프라인·앱을 고친 뒤 `.venv/bin/python -m pytest` 전체.

## Review Focus

1. 썸네일을 만드는 중에 재생·스크롤·확대·끌기가 버벅이지 않아야 한다 → Task 6 수동 점검 + 시트 도착 시 다시 그리기를 rAF 한 번으로 묶는 것을 확인.
2. 같은 영상을 다시 분석(옵션 바꿔 재판정)할 때 썸네일 일꾼이 미리보기 영상을 붙잡고 있으면 안 된다 → Task 3 `test_make_execute_holds_thumbs`.
3. 미리보기 영상이 바뀌면(다시 분석) 옛 시트를 쓰지 않는다 → Task 1 `test_meta_mismatch_resets_sheets`.
4. 아주 짧은 영상(1초 미만)·마지막 시트가 덜 찬 경우 → Task 1 `test_plan_short_and_partial`.
5. 확대 상태에서 ←/→ 로 구간 끝이 보기 밖으로 나가도 따라간다 → Task 5 `followT` 테스트 + Task 6 연결.

---

### Task 0: 먼저 만든 구간 다듬기와 설계 문서 커밋

**Files:** 이미 고친 `app/api.py`, `app/ui/app.js`, `app/ui/style.css`, `tests/test_range_ops.py`, `CLAUDE.md`,
`docs/superpowers/specs/2026-10-22-desktop-app-design.md`, `docs/superpowers/specs/2026-10-28-filmstrip-timeline-design.md`, 이 계획.

- [ ] **Step 1:** `.venv/bin/python -m pytest -q` → 모두 통과.
- [ ] **Step 2: Commit**

```bash
git add app/api.py app/ui/app.js app/ui/style.css tests/test_range_ops.py CLAUDE.md docs/superpowers/specs docs/superpowers/plans/2026-10-28-filmstrip-timeline.md
git commit -m "앱: 구간을 프레임 단위로 — ←/→·여기서 시작/끝(I/O)·Shift+클릭 두 번, 프레임 타임라인 설계"
```

---

### Task 1: 썸네일 시트 계획·명령·캐시 메타 (`app/thumbs.py` 순수 부분)

**Files:**
- Create: `app/thumbs.py`
- Modify: `app/runner.py` (`run(..., low_priority: bool = False)`)
- Test: `tests/test_thumbs.py`

**Interfaces:**
- Produces (`app/thumbs.py`):
  - 상수 `THUMB_VERSION = 1`, `STEP = 1.0`(초), `TILE_H = 90`, `COLS = 10`, `ROWS = 10`, `PER_SHEET = 100`.
  - `video_sig(video: Path) -> dict` — `{"size", "mtime_ns"}`.
  - `plan(duration: float, width: int, height: int) -> dict` — `{"v", "step", "tile_w", "tile_h", "cols", "rows", "per_sheet", "count", "sheets"}`.
    `tile_w = max(2, round(TILE_H * width / height / 2) * 2)`, `count = max(1, ceil(duration / STEP))`, `sheets = ceil(count / PER_SHEET)`.
  - `sheet_name(i: int) -> str` — `f"sheet_{i:04d}.jpg"`.
  - `sheet_cmd(video: Path, i: int, p: dict, out: Path) -> list[str]` — `ffmpeg -hide_banner -loglevel error -y -threads 2 -ss <i*per_sheet*step> -t <per_sheet*step> -i video -vf fps=1/step,scale=tile_w:tile_h,tile=COLSxROWS -frames:v 1 -q:v 6 out`.
  - `load_meta(d: Path, video: Path, p: dict) -> dict` — `d/"thumbs.json"` 을 읽어 `{"v", "video": video_sig, **p, "done": [int]}` 를 돌려준다. 버전·`video_sig`·`plan` 이 다르면 `d` 의 `sheet_*.jpg` 를 지우고 `done=[]` 로 새로 쓴다.
  - `mark_done(d: Path, meta: dict, i: int) -> dict` — `done` 에 `i` 추가(정렬, 중복 없음) 후 원자적 쓰기(`.part` → `os.replace`).
- Produces (`app/runner.py`): `run(cmd, weights, on_progress, env=None, on_start=None, low_priority=False)`. True 면 맥·리눅스는 `preexec_fn=lambda: os.nice(10)`, 윈도우는 `BELOW_NORMAL_PRIORITY_CLASS` 를 creationflags 에 더한다.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_plan_one_hour_1080p():
    p = thumbs.plan(3600.0, 960, 540)
    assert (p["tile_w"], p["tile_h"], p["count"], p["sheets"]) == (160, 90, 3600, 36)

def test_plan_short_and_partial():
    assert thumbs.plan(0.4, 960, 540)["count"] == 1 and thumbs.plan(0.4, 960, 540)["sheets"] == 1
    assert thumbs.plan(150.5, 540, 960)["sheets"] == 2 and thumbs.plan(150.5, 540, 960)["tile_w"] == 50

def test_sheet_cmd_window_and_threads(tmp_path):
    cmd = thumbs.sheet_cmd(tmp_path / "v.mp4", 3, thumbs.plan(3600, 960, 540), tmp_path / "s.jpg")
    assert cmd[cmd.index("-ss") + 1] == "300" and cmd[cmd.index("-t") + 1] == "100"
    assert cmd[cmd.index("-threads") + 1] == "2" and "tile=10x10" in cmd[cmd.index("-vf") + 1]

def test_meta_mismatch_resets_sheets(tmp_path):
    v = tmp_path / "v.mp4"; v.write_bytes(b"a"); d = tmp_path / "thumbs"; d.mkdir()
    p = thumbs.plan(300, 960, 540)
    m = thumbs.mark_done(d, thumbs.load_meta(d, v, p), 1)
    (d / thumbs.sheet_name(1)).write_bytes(b"x")
    assert thumbs.load_meta(d, v, p)["done"] == [1]
    v.write_bytes(b"bb")                                   # 미리보기 영상이 바뀜
    assert thumbs.load_meta(d, v, p)["done"] == [] and not (d / thumbs.sheet_name(1)).exists()

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg 필요")
def test_sheet_cmd_real_ffmpeg(tmp_path):
    # testsrc 12초 320x180 → 시트 1장, 크기 = (tile_w*10) x (90*10)
    ...ffmpeg -f lavfi testsrc 로 v.mp4 를 만들고 sheet_cmd 를 돌려 ffprobe 로 1600x900 확인
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_thumbs.py -v` → FAIL (모듈 없음).
- [ ] **Step 3:** `app/thumbs.py` 순수 부분과 `runner.run(low_priority=...)` 구현. runner 변경은 `tests/test_runner.py` 에 `low_priority=True` 로 짧은 명령(`[sys.executable, "-c", "print(1)"]`)이 성공하는 테스트 하나 추가.
- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_thumbs.py tests/test_runner.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "앱: 썸네일 시트 계획·ffmpeg 명령·캐시 메타"`

---

### Task 2: 썸네일 일꾼 `Thumbs`

**Files:**
- Modify: `app/thumbs.py`
- Test: `tests/test_thumbs.py`

**Interfaces:**
- Consumes: Task 1 의 `plan`·`load_meta`·`mark_done`·`sheet_cmd`·`sheet_name`.
- Produces: `class Thumbs(run: Callable[[list[str]], RunResult], on_sheet: Callable[[str], None])`
  - `request(job_id: str, video: Path, out_dir: Path, duration: float, size: tuple[int, int]) -> dict | None` — 메타(`load_meta`)를 돌려주고,
    `done` 이 모자라면 그 job 을 **유일한 대상**으로 정한다(다른 job 을 만들던 중이면 그 프로세스를 멈춤). 이미 그 job 이면 그대로 둔다.
    `held` 이거나 멈췄으면 메타만 돌려주고 만들지 않는다. 비디오가 없으면 None.
  - 작업 스레드: 대상 job 의 `done` 에 없는 시트를 번호 순서대로 하나씩 `run(sheet_cmd(...))` → 성공하면 `mark_done` 후 `on_sheet(job_id)`.
    실패한 시트는 이번 실행에서 다시 시도하지 않고 건너뛴다 (로그만). 다 만들면 대상을 비운다.
  - `started(handle)`, `cancel(job_id, timeout) -> bool`, `release(job_id)`, `drop(job_id)`, `shutdown()`, `wait_idle(timeout) -> bool` — `preview.CutAudio` 와 같은 뜻.

- [ ] **Step 1: 실패하는 테스트** (가짜 `run` 이 `out` 경로에 바이트를 쓰고 `RunResult(True, "")` 를 돌려줌)

```python
def test_builds_all_sheets_in_order_and_notifies(tmp_path): ...
    # duration 250 → 시트 3장, on_sheet 3번, meta done == [0, 1, 2], 다시 request 하면 run 이 불리지 않음
def test_new_job_replaces_current(tmp_path): ...
    # A 를 만드는 중(run 이 gate 에서 대기) B 를 request → A 의 handle.cancel 이 불리고 이후 run 은 B 의 경로만
def test_cancel_holds_until_release(tmp_path): ...
    # cancel(A) 뒤 request(A) 는 만들지 않음, release(A) 뒤 request(A) 는 만듦
def test_failed_sheet_skipped_not_retried(tmp_path): ...
    # 시트 1 의 run 이 실패 → done == [0, 2], 같은 실행 안에서 시트 1 run 은 한 번
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_thumbs.py -v` → 새 테스트 FAIL.
- [ ] **Step 3:** `Thumbs` 구현. 상태는 `CutAudio` 처럼 `threading.Condition` 하나와 `_target: tuple[job_id, video, out_dir, plan] | None`.
- [ ] **Step 4:** 같은 명령 → PASS.
- [ ] **Step 5: Commit** — `git commit -m "앱: 썸네일 일꾼 — 열어 본 영상만, 시트 하나씩, 멈추고 다시 받기"`

---

### Task 3: API·파일 서버 연결

**Files:**
- Modify: `app/api.py` (`Api.__init__`, `_shutdown`, `remove_job`, `get_result`/`_preview`, 새 `get_thumbs`, `make_execute`)
- Modify: `app/window.py` (`_PREVIEW_PATH`, `_CONTENT_TYPES`, `make_execute(...)` 호출부 279행)
- Test: `tests/test_api.py`, `tests/test_window.py`

**Interfaces:**
- Consumes: Task 2 `Thumbs`.
- Produces:
  - `Api.__init__(..., thumbs_run: Callable[[list[str]], RunResult] | None = None)` → `self._thumbs = thumbs.Thumbs(thumbs_run or self._run_thumbs, lambda job_id: self._push("thumbs", {"id": job_id}))`.
    `_run_thumbs` 는 `runner.run(cmd, {"thumbs": 1.0}, lambda p: None, env=env, on_start=self._thumbs.started, low_priority=True)`.
  - `_preview` 안에서 미리보기 폴더가 있고 상태가 REVIEW/SAVED 면 `self._thumbs.request(job.id, pdir/"video.mp4", pdir/"thumbs", duration, size)`.
    `size` 는 `ffprobe` 대신 `video.index.json` 에 없으므로 `runner` 로 `ffprobe -show_entries stream=width,height` 를 한 번 부르고 결과를 `pdir/"thumbs"/"size.json"` 에 캐시하는 `_preview_size(video) -> tuple[int, int] | None`.
  - `Api.get_thumbs(job_id: str) -> dict | None` — `{"step", "tile_w", "tile_h", "cols", "rows", "per_sheet", "count", "sheets": [url 또는 None]}` (만든 시트만 url). 미리보기 폴더 없으면 None.
  - `_shutdown` 은 `self._thumbs.shutdown()` 도, `remove_job` 은 `self._thumbs.drop(job_id)` 도.
  - `make_execute(..., thumbs_getter: Callable[[], "thumbs.Thumbs | None"] | None = None)` — **분석(`ANALYZE`)일 때만** 그 job 을 `cancel`/`release` (`CUT_AUDIO_WAIT` 재사용). 저장(RENDER)은 막지 않는다.
  - `window.py`: `_PREVIEW_PATH` 에 `preview/thumbs/sheet_[0-9]{4}\.jpg` 추가, `_CONTENT_TYPES[".jpg"] = "image/jpeg"`, `make_execute` 호출에 `lambda: api._thumbs if api is not None else None`.
  - 화면 알림: `push("thumbs", {"id"})`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_get_result_requests_thumbs_for_review_job(tmp_path): ...
    # 미리보기 폴더(video.mp4, audio_full.index.json)·size.json 을 만들어 두고 get_result → 가짜 thumbs_run 이 불림,
    # wait_idle 뒤 get_thumbs(job.id)["sheets"] 가 모두 url, push 에 ("thumbs", {"id": job.id})
def test_get_thumbs_none_without_preview(tmp_path): ...
def test_remove_job_drops_thumbs(tmp_path): ...
def test_make_execute_holds_thumbs(tmp_path): ...
    # 분석 실행 중엔 thumbs.request 가 만들지 않음, 끝난 뒤엔 만듦. RENDER 는 cancel 을 부르지 않음
# tests/test_window.py
def test_serves_thumb_sheet_as_jpeg(tmp_path): ...   # /<id>/preview/thumbs/sheet_0000.jpg → 200, image/jpeg
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_api.py tests/test_window.py -v` → 새 테스트 FAIL.
- [ ] **Step 3:** 구현.
- [ ] **Step 4:** 같은 명령 → PASS, 이어서 전체 `-q` 통과.
- [ ] **Step 5: Commit** — `git commit -m "앱: 열어 본 영상의 썸네일을 뒤에서 만들고 get_thumbs·파일 서버로 내주기"`

---

### Task 4: 파형 API

**Files:**
- Modify: `app/api.py` (새 `get_waveform`)
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `Api.get_waveform(job_id: str) -> dict | None` — `{"hop": 0.01, "floor": -60.0, "data": <base64 str>}`.
  값 하나 = 10ms 구간 RMS 를 dB(`20*log10(max(rms, 1e-6))`)로 바꿔 `[floor, 0]` 을 0~255 로 줄인 바이트.
  원천은 `jd/"work"/<원본 stem>/"asr.wav"` (`pipeline.load_rms`). 결과를 `jd/"preview"/"wave.bin"` 에 쓰고 `wave.meta.json` 에
  `asr.wav` 의 크기·mtime 을 남겨 같으면 다시 계산하지 않는다. `asr.wav` 가 없으면 None. 상태가 REVIEW/SAVED 일 때만.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_waveform_bytes_and_cache(tmp_path): ...
    # 16kHz 1초 asr.wav: 앞 0.5초 무음, 뒤 0.5초 진폭 0.5 사인 → len(data) == 100,
    # data[:50] 모두 0, data[60:] 모두 > 200; 두 번째 호출은 load_rms 를 부르지 않음(monkeypatch 로 셈)
def test_waveform_none_without_asr(tmp_path): ...
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_api.py -k waveform -v` → FAIL.
- [ ] **Step 3:** 구현 (`subtitles.py` 처럼 `scripts/` 를 `sys.path` 에 넣고 `pipeline.load_rms`).
- [ ] **Step 4:** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "앱: 소리 파형 get_waveform (분석 오디오 에너지, 캐시)"`

---

### Task 5: 보기 범위 계산 (`app/ui/timeline.js`)

**Files:**
- Create: `app/ui/timeline.js` (`player.js` 처럼 끝에 `window`/`module.exports` 내보내기)
- Modify: `app/ui/index.html` (`<script src="timeline.js">` 를 `app.js` 앞에)
- Test: `tests/test_timeline_js.py` (`tests/test_player_js.py` 의 `run()` 방식)

**Interfaces:**
- Produces (view = `{t0: 초, spc: 칸당 초}`, 칸 너비 `cw` px, 줄 너비 `w` px):
  - `fitView(d, w, cw) -> view` — 영상 전체: `{t0: 0, spc: d / max(1, floor(w / cw))}`.
  - `clampView(v, d, w, cw, fps) -> view` — `spc` 를 `[1/fps, fit.spc]` 로, `t0` 를 `[0, max(0, d - spc*w/cw)]` 로.
  - `zoomAt(v, factor, anchorT, d, w, cw, fps) -> view` — `anchorT` 의 화면 위치를 유지하며 `spc /= factor` 후 `clampView`.
  - `tToX(v, t, cw) -> px`, `xToT(v, x, cw) -> 초`.
  - `followT(v, t, d, w, cw, fps) -> view` — `t` 가 보이면 그대로, 아니면 `t` 가 왼쪽에서 10% 자리에 오게 넘긴 뒤 `clampView`.
  - `cells(v, d, w, cw) -> [{i, t, x}]` — 보이는 칸들 (칸 시작 시간 `t = floor(t0/spc)*spc + k*spc`, `t < d`).
  - `sheetCell(t, meta) -> {sheet, col, row} | null` — `n = floor(t / meta.step)`, `n >= meta.count` 면 null.
  - `waveMax(bytes, hop, a, b) -> 0~255` — `[a, b)` 구간 바이트 최대값 (빈 구간이면 가장 가까운 값).
  - `sliderToSpc(s, minSpc, maxSpc)` / `spcToSlider(spc, minSpc, maxSpc)` — 0~1000 슬라이더, 로그 눈금.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_fit_and_clamp():      # d=3600, w=1000, cw=100 → spc 360; clamp 로 spc 1e-9 → 1/30
def test_zoom_keeps_anchor():  # zoomAt(×2, anchor 1800) 전후 tToX(anchor) 같음
def test_follow_pages():       # 보이는 t 는 그대로, 밖의 t 는 x == 0.1*w
def test_sheet_cell():         # step 1, per 100, cols 10: t=123.4 → {sheet 1, col 3, row 2}; t>=count → null
def test_wave_max_and_slider_roundtrip(): ...
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_timeline_js.py -v` → FAIL.
- [ ] **Step 3:** 구현.
- [ ] **Step 4:** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "앱 화면: 타임라인 보기 범위 계산 (확대·따라가기·썸네일 칸)"`

---

### Task 6: 화면 줄·소리 줄·확대 조절 (시트 썸네일)

**Files:**
- Modify: `app/ui/app.js` (`resultParts`, `timeline()`, `drawRange`, `stepEnd`, `pickEnd`, 키 처리, `player.drawHead`, `onAppEvent`, `select`)
- Modify: `app/ui/style.css`
- Modify: `docs/superpowers/specs/2026-10-22-desktop-app-design.md` 3.3, `CLAUDE.md` 앱 화면 줄

**Interfaces:**
- Consumes: Task 3 `get_thumbs`·`"thumbs"` 알림, Task 4 `get_waveform`, Task 5 함수 전부.
- Produces (app.js 안):
  - 상태: `S.view` (view|null), `S.thumbs` (get_thumbs 결과|null), `S.wave` ({hop, bytes: Uint8Array}|null).
  - `zoomTl` 객체 하나 (rangeBar 처럼 한 번 만들고 다시 붙임): `{el, frames: canvas, wave: canvas, zoom: {out, slider, in, label}, draw()}`.
  - `rangePointer(el, toT)` — 지금 `timeline()` 의 pointerdown/move/up 처리를 꺼낸 함수. `toT(clientX, g)` 만 다르게 해서 전체 막대(전체 비율)와 화면 줄·소리 줄(`xToT(S.view, ...)`)이 함께 쓴다.
  - `ensureVisible(t)` — `S.view = followT(...)` 후 `zoomTl.draw()`. `stepEnd`·`pickEnd`·재생 중 `drawHead` 가 부른다.
  - 전체 막대에 `.vwin` (노란 틀) — `S.view` 범위.
- 그리기: 화면 줄은 `cells()` 마다 `sheetCell` → 시트 `Image` 의 해당 칸을 `drawImage`, 시트가 없으면 회색 칸. 잘린 곳 빗금, 선택 구간 파란 테두리·손잡이(활성 끝 강조, 2장과 같은 뜻), 재생 위치 세로선, 시작점 점선(`S.mark`).
  소리 줄은 픽셀 열마다 `waveMax` 막대. 다시 그리기는 `requestAnimationFrame` 하나로 묶는다 (시트가 연달아 도착해도 한 번).
- 확대: `−`/슬라이더/`+`, ⌘(맥)·Ctrl(윈도우)+휠과 트랙패드 벌리기(`wheel` 의 `ctrlKey`) = `zoomAt` (마우스 위치 기준), 그냥 휠(deltaX 우선, 없으면 deltaY) = 좌우 이동. 라벨: `1칸 = N프레임` 또는 `1칸 = N초`.
- 문구(스펙 3.3 에 추가): 줄 이름 없음, 확대 라벨 `1칸 = {n}프레임` / `1칸 = {n}초`, 버튼 title `축소`/`확대`.
- 화면 줄 숨김: `r.preview` 없음 또는 `get_thumbs` None. 소리 줄 숨김: `get_waveform` None.

- [ ] **Step 1:** 화면 연결 구현. `select()` 에서 `S.view = S.thumbs = S.wave = null`, `loadResult` 뒤 처음 한 번 `get_thumbs`·`get_waveform`, `onAppEvent('thumbs')` 는 그 영상이면 `get_thumbs` 만 다시.
- [ ] **Step 2:** 가짜 API 하네스로 확인 — 스크래치 폴더에 `app/ui` 사본 + `stub.js`(`get_result` 에 `preview: {video: 'v.webm', ...}` 대신 `preview_url`, `get_thumbs` 는 ffmpeg 로 만든 시트 2장, `get_waveform` 은 합성 바이트), VP9 테스트 영상, `python3 -m http.server`. Playwright 로:
  - 휠 확대 → 라벨이 `1칸 = 1프레임` 까지 내려가고 노란 틀이 좁아짐.
  - 화면 줄에서 끌기/Shift+클릭 두 번/손잡이 → `S.range` 가 프레임 격자, 전체 막대 구간과 같음.
  - 확대 상태에서 →(Shift) 를 반복해 구간 끝이 보기 밖으로 → `S.view.t0` 가 따라 바뀜.
  - 전체 막대 클릭 → 재생 위치 이동 + 보기 범위가 따라감. 전체 막대 끌기는 지금처럼 구간 선택.
  - 스크린숏으로 빗금·테두리·재생선·점선이 보이는지.
- [ ] **Step 3:** 스펙 3.3·CLAUDE.md 문구 갱신, `.venv/bin/python -m pytest -q` 전체 통과.
- [ ] **Step 4: Commit** — `git commit -m "앱 화면: 프레임 썸네일 줄·소리 파형 줄·확대 조절"`

---

### Task 7: 프레임 단위 썸네일 (숨은 video)

**Files:**
- Modify: `app/ui/app.js`

**Interfaces:**
- Consumes: Task 6 `zoomTl.draw()`, `cells()`.
- Produces: `frameGrab` 객체 — `{attach(url), detach(), get(t) -> canvas|null}`. 숨은 `<video muted preload="auto">` 하나에 미리보기 영상(`r.preview.video`)을 걸고,
  요청된 시간들을 하나씩 `currentTime = t` → `seeked` 에서 `tile_w×tile_h` canvas 에 `drawImage`. 키는 원본 프레임 번호 `round(t*fps)`.
  메모리 캐시 최대 600장(오래된 것부터 버림). 보이는 칸만 요청하고, 보기 범위가 바뀌면 대기열을 새 칸들로 바꾼다.
  시트 간격보다 확대했을 때(`spc < thumbs.step`)와, 시트가 아직 없는 칸에 쓴다. 다 그리면 `zoomTl.draw()` 를 rAF 로.
  `select()`·결과가 바뀔 때 `detach`.

- [ ] **Step 1:** 구현.
- [ ] **Step 2:** 하네스(미리보기 폴더 방식 결과를 흉내 낼 필요 없이 `frameGrab.attach('v.webm')` 를 직접)에서 1프레임 확대 → 칸마다 다른 프레임(testsrc 숫자)이 보이는 스크린숏, 시트 없는 상태(`get_thumbs` 가 빈 시트)에서도 확대하면 그려짐.
- [ ] **Step 3:** `.venv/bin/python -m pytest -q` 전체 통과.
- [ ] **Step 4: Commit** — `git commit -m "앱 화면: 확대했을 때 프레임 단위 썸네일을 미리보기 영상에서 직접 그리기"`

---

### Task 8: 실제 앱·긴 영상 확인

- [ ] **Step 1:** 합성 1시간 영상 `ffmpeg -f lavfi -i testsrc2=size=1920x1080:rate=30:duration=3600 -f lavfi -i sine=frequency=300:duration=3600 ...` 를 스크래치에 만들고
  `--denoise none` 으로 분석해 미리보기 폴더를 만든 뒤, 썸네일 36장 생성 시간과 `preview/thumbs` 크기(목표 수십 MB 안쪽)를 잰다.
- [ ] **Step 2:** `.venv/bin/python -m app` 으로 짧은 실제 영상(`input/`)을 열어 확대·선택·재생 따라가기·썸네일이 차차 채워짐을 사용자와 함께 확인.
- [ ] **Step 3:** 잰 값을 스펙 5장에 한 줄로 남기고 커밋 — `git commit -m "문서: 프레임 타임라인 썸네일 생성 시간·용량 실측"`
