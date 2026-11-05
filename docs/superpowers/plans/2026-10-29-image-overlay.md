# 이미지 겹쳐 넣기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 데스크톱 앱에서 PNG(로고·썸네일)를 결과 영상 위 원하는 자리·크기·원본 시간 구간에 겹쳐 새기고, 로고를 저장해 다른 영상에 다시 넣는다.

**Architecture:** 앱은 영상별 이미지 목록(`Job.overlays`, 비율 좌표 + 원본 초)을 들고 편집·되돌리기·저장한다. 저장(렌더) 때
`out/<이름>.overlays.json` 을 만들어 파이프라인 숨은 옵션 `--overlays` 로 넘기고, 파이프라인은 원본 구간을 keeps 로 결과 구간으로 바꿔
`overlay` 필터(enable 식)를 컷 뒤·자막(ass) 앞에 잇는다. 화면은 영상 위 겹침 층(끌기·크기)과 확대 타임라인의 이미지 줄(막대 끌기)을 더한다.

**Tech Stack:** Python 3.11, ffmpeg(`overlay` 필터), pywebview, 바닐라 JS (`app/ui`), pytest + node.

**Spec:** `docs/superpowers/specs/2026-10-29-image-overlay-design.md`

## Global Constraints

- `pipeline.py` CLI 기본 동작은 그대로. `--overlays` 는 앱 전용 숨은 옵션, 없으면 지금과 똑같은 명령·필터.
- 위치·크기는 영상 화면 비율(x, y = 왼쪽 위, w = 영상 너비 대비), 이미지 자체 가로세로 비율은 유지. 구간은 **원본 초**, 원본 프레임 격자(`get_result` 의 `fps`)에 맞춤.
- 이미지는 자막 **아래**, 이미지끼리는 목록 뒤쪽(나중에 넣은 것)이 위.
- 10비트/HDR 원본도 색 변환 없이 그대로 새긴다 (경고·막기 없음).
- 새 이미지 기본: 재생 위치부터 `min(5초, 영상 끝)`, 화면 가운데, 너비 0.25. 저장한 로고 넣기: 저장된 x·y·w, 구간 `[0, 영상 길이]`.
- 편집은 확인 대기·저장됨 상태에서만, `실행 취소` 대상, 저장됨이면 확인 대기로 돌린다 (기존 `_change_ops` 와 같게).
- 고른 PNG 는 `jobs/<id>/overlays/<sha1 앞 16자>.png` 로 복사, 저장한 로고는 `data_dir()/overlay_presets/` (`presets.json` + `<sha16>.png`).
- 오디오만 있는 입력: 화면에서 이미지 기능을 숨기고(미리보기 `videoWidth === 0`), 파이프라인은 `--overlays` 를 무시하고 로그만 남긴다.
- 새 화면 문구는 데스크톱 앱 스펙(`docs/superpowers/specs/2026-10-22-desktop-app-design.md`) 3장에 적는다. 문구: `이미지 넣기`, `로고로 저장`, `저장한 이미지`, `지우기`.
- Pillow 없음 — PNG 크기는 IHDR(바이트 16~24, 빅엔디언 너비·높이)에서 읽고, 시그니처 `\x89PNG\r\n\x1a\n` 가 아니면 거절.
- 파이프라인이나 앱을 고친 뒤엔 `.venv/bin/python -m pytest` 전체를 돌린다.

## Review Focus

1. **구간이 컷에 걸침/통째로 잘림** — 원본 구간 안이 잘리면 결과에선 이어 붙은 한 구간, 통째로 잘리면 필터에 아예 안 들어감 (Task 1 테스트).
2. **세로 영상(rotation 90/270)·홀수 해상도** — 비율 좌표가 돌린·크롭한 뒤 화면 크기로 계산돼야 한다 (Task 1 `overlay_filter` 테스트에 회전 케이스).
3. **이미지가 화면 밖으로 끌려 나감** — 끌기·크기 바꾸기 결과는 영상 안으로 고정(최소 너비 0.02), 막대는 `[0, 영상 길이]` 안, 최소 1프레임 이상 (Task 4 테스트).
4. **고른 파일이 PNG 가 아니거나 원본이 지워짐** — 거절(None), 복사본으로 렌더 (Task 3 테스트).
5. **자막 새기기와 함께 저장** — 이미지가 자막 아래에 깔리고 둘 다 보인다 (Task 1 실제 ffmpeg 테스트에서 필터 순서 확인).

---

### Task 1: 파이프라인 — `--overlays` 로 이미지 새기기

**Files:**
- Modify: `scripts/pipeline.py` (`render_video` 1635~, 인자 2010 근처, 렌더 호출 2198~2236)
- Test: `tests/test_overlay_render.py` (새 파일)

**Interfaces:**
- Produces:
  - `overlay_windows(start: float, end: float, keeps: list[tuple[int, int]], fps: Fraction) -> list[tuple[int, int]]` — 원본 초 구간을 **결과 프레임 번호** 반구간 `[a, b)` 목록으로 (이어지는 것 합침, 빈 목록 = 결과에 없음).
    원본 프레임 `fs = round(start*fps)`, `fe = round(end*fps)`; 각 keep `(sf, ef)` 에 대해 `[max(fs,sf), min(fe,ef))` 가 비어 있지 않으면 결과 오프셋을 더해 넣는다.
  - `load_overlays(path: Path | None) -> list[dict]` — json `[{path, x, y, w, start, end}]` 읽기. 파일 없음·형식 틀림·이미지 파일 없음 항목은 로그 남기고 뺀다.
  - `overlay_filter(ovs: list[dict], keeps, fps, W: int, H: int, first_input: int) -> tuple[str, list[str]]` — `[v]` 를 받아 `[vo]` 를 내는 필터 조각과
    ffmpeg 입력 인자(`["-i", path, ...]`). 이미지 i 는 입력 `first_input+i`, `scale=<짝수 px 너비>:-2`, `overlay=x=<px>:y=<px>:enable='<식>'`.
    식은 결과 프레임 `[a,b)` 마다 `between(t,(a-0.5)/f,(b-0.5)/f)` 를 `sum_tree` 로 묶음 (render_video 의 select 식과 같은 반 프레임 규칙). 구간이 없는 이미지는 건너뛴다. ovs 가 비면 `("", [])`.
  - `render_video(..., burn=None, overlays: list[dict] | None = None)` — 필터 순서: `select…setpts{crop}` → 이미지들 → `ass`.
  - CLI: `--overlays <json 경로>` (help=argparse.SUPPRESS 아님, 앱 전용임을 help 에 적음).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from fractions import Fraction
import pipeline as pl

F = Fraction(30)

def test_windows_follow_cuts():
    keeps = [(0, 90), (120, 300)]                       # 원본 3~4초가 잘림
    assert pl.overlay_windows(2.0, 5.0, keeps, F) == [(60, 120)]   # 2~3초(60~90) + 4~5초(→90~120) 이어 붙음
    assert pl.overlay_windows(3.0, 4.0, keeps, F) == []            # 통째로 잘림
    assert pl.overlay_windows(0.0, 10.0, keeps, F) == [(0, 270)]

def test_filter_order_and_geometry_rotated():
    ov = [{"path": "/x/a.png", "x": 0.5, "y": 0.1, "w": 0.25, "start": 0.0, "end": 1.0}]
    frag, inputs = pl.overlay_filter(ov, [(0, 300)], F, 1080, 1920, 3)   # 세로 화면 W=1080
    assert inputs == ["-i", "/x/a.png"]
    assert "[3:v]scale=270:-2" in frag and "x=540:y=192" in frag and "between(t," in frag
    assert pl.overlay_filter([], [(0, 300)], F, 1920, 1080, 2) == ("", [])
```

  그리고 실제 ffmpeg 테스트 `test_render_overlay_pixels(tmp_path)`: `lavfi color=c=black:s=320x240:d=2` 를 원본으로, 빨간 불투명 8x8 PNG(numpy 로 만든
  raw 를 `ffmpeg -f rawvideo -pix_fmt rgba` 로 png 저장)를 `x=0.5,y=0.5,w=0.1`, 구간 0~1초로 `render_video` 에 넣고(enc_args `["-c:v","libx264"]`, keeps `[(0,60)]`),
  결과 0.5초 프레임의 (176,128) 근처가 빨강(R>200), 1.5초 프레임은 검정인지 확인. 가운데 4x4 만 불투명한 PNG 로 투명 부분이 검정으로 남는지도 확인.
  `--burn-subs` 와 함께인 경우는 `_ffmpeg_with_ass()`(tests/test_caption_style.py) 가 있을 때만: 필터 스크립트 `work/video_filter.txt` 에서 `overlay` 가 `ass=` 보다 앞인지 확인.

- [ ] **Step 2: 실행해 실패 확인** — `.venv/bin/python -m pytest tests/test_overlay_render.py -v` → `AttributeError: overlay_windows`
- [ ] **Step 3: 구현** — 위 Interfaces 대로. 메인 흐름: `ovs = load_overlays(args.overlays)`; `info["has_video"]` 가 아니면 `ovs` 가 있을 때 "오디오만 있는 파일이라 이미지는 넣지 않습니다" 로그 후 무시.
  W·H 는 burn 과 같은 계산(짝수 내림, rotation 90/270 이면 맞바꿈)을 함수로 빼서 둘이 같이 쓴다. 두 `render_video` 호출(하드웨어 실패 재시도 포함) 모두 `overlays=ovs`.
  이미지 입력은 SRT 입력 뒤에 붙이므로 `first_input = 3 if srt else 2`.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 이어서 `.venv/bin/python -m pytest` 전체 PASS
- [ ] **Step 5: 커밋** — `git commit -m "파이프라인: --overlays 로 이미지 겹쳐 새기기"`

---

### Task 2: 앱 모델 — `Job.overlays` 저장·렌더로 넘기기

**Files:**
- Modify: `app/store.py` (Job), `app/api.py` (`save_overlays`, `pipeline_command`, `_snap`/`_put`), `app/runner.py` (`render_command`)
- Test: `tests/test_overlays.py` (새 파일 — Task 3·4 테스트도 여기)

**Interfaces:**
- Consumes: Task 1 의 json 형식 `[{path, x, y, w, start, end}]`
- Produces:
  - `Job.overlays: list` — 항목 `{"id": str(8 hex), "img": "<sha16>.png", "name": str, "aspect": float(너비/높이), "x", "y", "w": float, "start", "end": float}`
  - `save_overlays(job, lock=None) -> Path | None` — `out/<stem>.overlays.json` 에 `path = job_dir/overlays/<img>` 절대 경로로 씀. 비었으면 낡은 파일 지우고 None.
  - `runner.render_command(job, model, device, media_dir, cuts_path=None, edits_path=None, overlays_path=None)` → 있으면 `["--overlays", str(overlays_path)]`
  - `Api._snap`/`_put` 튜플 끝에 `job.overlays` 추가.

- [ ] **Step 1: 실패하는 테스트** — `test_job_overlays_roundtrip` (Store 저장·불러오기 후 같음, 옛 jobs.json 에 키 없으면 `[]`),
  `test_render_command_passes_overlays` (`pipeline_command(job, RENDER, ...)` 명령에 `--overlays` 와 json 경로, json 의 path 가 `job_dir/overlays/<img>`),
  `test_render_command_without_overlays` (`--overlays` 없음, 낡은 json 지워짐).
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_overlays.py -v`
- [ ] **Step 3: 구현** — `save_subs_edits` 와 같은 모양(락 안에서 deepcopy, 락 밖에서 씀).
- [ ] **Step 4: 통과 확인** — 같은 명령 + `tests/test_history.py tests/test_runner.py` PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱: 영상별 이미지 목록을 저장 때 파이프라인으로 넘김"`

---

### Task 3: Api — 이미지 넣기·고치기·지우기, 저장한 로고

**Files:**
- Modify: `app/api.py` (Dialogs 프로토콜, 새 메서드, `get_result`), `app/window.py` (`open_image` 대화상자, 미리보기 서버 경로), `app/paths.py` (`presets_dir()`)
- Test: `tests/test_overlays.py`, `tests/test_window.py`

**Interfaces:**
- Consumes: Task 2 의 `Job.overlays` 항목 형식, `_snap`
- Produces (모두 확인 대기·저장됨에서만, 안 되면 None, 성공 → `{"overlays": [화면용 항목], "history": {...}}`. 화면용 항목 = 저장 항목 + `"url"`):
  - `Dialogs.open_image() -> str | None` (PNG 하나, `window.py` 에서 `file_types=("PNG 이미지 (*.png)",)`)
  - `Api.add_overlay(job_id, at: float, vid_aspect: float) -> dict | None` — 대화상자 → PNG 확인·복사 → `w=0.25`, `x=0.375`,
    `y=max(0, (1 - 0.25*vid_aspect/aspect)/2)` (화면 가운데; `vid_aspect` = 화면이 아는 영상 너비/높이), `[at, min(at+5, 길이)]`, 이름 = 파일 이름(확장자 뺌). 취소하면 None.
  - `Api.update_overlay(job_id, oid: str, patch: dict) -> dict | None` — 키는 `x,y,w,start,end` 만, 유한한 수, `0 <= start < end <= 길이`, `0.02 <= w <= 1`. 아니면 None.
  - `Api.delete_overlay(job_id, oid) -> dict | None`
  - `Api.list_presets() -> list[dict]` — `[{"id", "name", "img", "aspect", "x", "y", "w", "url"}]`
  - `Api.save_preset(job_id, oid, name: str) -> list[dict] | None` — 이름 1~40자(앞뒤 공백 뺌). 이미지를 presets 폴더로 복사.
  - `Api.delete_preset(pid) -> list[dict]`
  - `Api.insert_preset(job_id, pid) -> dict | None` — 새 id, 같은 x·y·w, `[0, 길이]`.
  - `get_result` 결과에 `"overlays"` (화면용 항목).
  - `paths.presets_dir() -> Path` = `data_dir()/"overlay_presets"`
  - 미리보기 서버: `/<id12>/overlays/<sha16>.png` (jobs 아래)와 `/presets/<sha16>.png` (presets_dir), Content-Type `image/png`.
    URL 은 `get_thumbs` 처럼 `f"{self._preview_base}/{job.id}/overlays/{img}"`, 프리셋은 `preview_base` 의 `/jobs` 를 뺀 뿌리 + `/presets/...` — 빌더가 `window.build` 의 preview_base 형태를 보고 맞춘다.
- `presets.json` 쓰기는 임시 파일 + `os.replace`. 같은 이름의 로고가 있으면 덮어쓴다.

- [ ] **Step 1: 실패하는 테스트** — `tests/test_overlays.py`:
  - `test_add_overlay_copies_png_and_sets_defaults` — FakeDialogs 에 `image=` 를 더해 PNG 경로 반환. 결과 항목 `w == 0.25`, `start == at`, `end == min(at+5, 20.0)`, 복사본 존재, 원본 지워도 `save_overlays` 경로 파일 존재.
  - `test_add_overlay_rejects_non_png` — `.png` 이름의 텍스트 파일 → None, 목록 그대로.
  - `test_update_overlay_validates` — `end > 길이`, `w=0`, 모르는 키, NaN → None. 정상 patch 는 반영.
  - `test_overlay_edits_undo_and_saved_to_review` — add → update → undo 두 번이면 `[]`, SAVED 에서 고치면 REVIEW.
  - `test_presets_roundtrip` — save_preset → list_presets 에 1개, 다른 job 에 insert_preset → `[0, 길이]`, 같은 x·y·w. delete_preset → `[]`. `YTEDIT_DATA_DIR` 은 tmp.
  - `tests/test_window.py` 에 서버가 `overlays/<16hex>.png`·`presets/<16hex>.png` 를 내주고 `overlays/../x` 는 404.
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_overlays.py tests/test_window.py -v`
- [ ] **Step 3: 구현** — 편집 공통은 `_change_ops` 를 일반화하거나 같은 모양의 `_change_overlays(job_id, change)` 로. 길이는 `self._duration(job)`.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 전체 pytest PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱: 이미지 넣기·고치기·지우기와 저장한 로고 Api"`

---

### Task 4: 화면 순수 계산 — `timeline.js`

**Files:**
- Modify: `app/ui/timeline.js` (TL 에 더함)
- Test: `tests/test_timeline_js.py`

**Interfaces:**
- Produces:
  - `ovRect(o, box, vidAspect) -> {x, y, w, h}` — 비율 항목 → 화면 px (`box` = `CS.contentBox` 결과). `h = w_px / o.aspect`.
  - `ovMove(o, dx, dy, box, vidAspect) -> {x, y}` — px 이동 → 비율, 이미지가 화면 밖으로 안 나가게 고정.
  - `ovResize(o, dx, box, vidAspect) -> {w}` — 오른쪽 아래 모서리 끌기(가로 기준, 비율 유지). `0.02 <= w`, 오른쪽·아래로 화면을 넘지 않게.
  - `barDrag(kind, s, e, dt, d, fps) -> {start, end}` — `kind` = `'move'|'start'|'end'`. 프레임 격자로 맞추고 `[0, d]` 안, 길이 최소 1프레임. move 는 길이 유지.
- [ ] **Step 1: 실패하는 테스트** — 각 함수 경계: 화면 밖으로 끌면 고정(x+w ≤ 1), w 하한 0.02, 세로 영상(vidAspect 9/16)에서 h 계산, `barDrag('end', 1, 2, -5, 10, 30)` → `end == 1+1/30`, `barDrag('move', 8, 9.5, 3, 10, 30)` → `{start: 8.5, end: 10}`.
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_timeline_js.py -v`
- [ ] **Step 3: 구현** — 다른 TL 함수처럼 순수 함수.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `git commit -m "앱 화면: 이미지 자리·막대 끌기 계산"`

---

### Task 5: 화면 — 영상 위 이미지 층과 타임라인 이미지 줄

**Files:**
- Modify: `app/ui/app.js` (`player.slot`/`layoutSub` 옆에 이미지 층, `zoomTl` 에 이미지 줄, 결과 화면 버튼·저장한 이미지 목록, 키보드 Delete), `app/ui/style.css`
- Modify: `docs/superpowers/specs/2026-10-22-desktop-app-design.md` 3장(문구), `CLAUDE.md` (데스크톱 앱 절: `--overlays`, 이미지 층·줄, presets 위치)
- Test: `tests/test_e2e_app.py` 에 가능한 만큼 (없으면 수동 검증 단계)

**Interfaces:**
- Consumes: Task 3 Api, Task 4 TL 함수, `S.result.overlays`
- Produces: 없음 (최종 화면)

- [ ] **Step 1: 영상 위 이미지 층** — `player.cap` 과 같은 방식으로 `.ovlayer`(capover 아래 z-index)를 `player.box` 에 넣고 `layoutSub` 에서 같은 contentBox 로 배치.
  지금 시각에 구간 안인 이미지를 `<img>` 로 그림(재생 중에도). 멈췄을 때만 눌러 선택·끌기·모서리 손잡이. 끝나면(pointerup) `update_overlay` 한 번만 부름 (끄는 동안은 화면만 움직임).
- [ ] **Step 2: 타임라인 이미지 줄** — `zoomTl` 의 rows 위(화면 줄 위)에 이미지마다 줄(이름 + 막대)을 그림. 막대 몸통 = move, 양 끝 6px = start/end, `TL.barDrag`, pointerup 에 `update_overlay`.
  잘린 곳 빗금은 화면 줄과 같은 그리기 재사용. 고른 이미지는 두 곳에서 함께 강조 (`S.ovSel`). Delete/Backspace(입력 중이 아닐 때 — `typingIn`) = `delete_overlay`.
- [ ] **Step 3: 버튼과 목록** — 타임라인 줄 옆에 `이미지 넣기`(→ `add_overlay(id, 재생 위치, videoWidth/videoHeight)`), 고른 이미지에 `로고로 저장`(이름 입력 → `save_preset`)·`지우기`,
  `저장한 이미지` 목록(작은 그림 + 이름, 누르면 `insert_preset`, ✕ 로 `delete_preset`). 미리보기 영상 `videoWidth === 0` 이면 이 셋을 숨김.
  편집 결과는 기존 `noteHist`·`loadResult` 흐름에 맞춰 반영.
- [ ] **Step 4: 문서** — 스펙 3장에 새 문구·조작, CLAUDE.md 에 한 줄씩.
- [ ] **Step 5: 확인** — `.venv/bin/python -m pytest` 전체 PASS. 앱을 띄워(`.venv/bin/python -m app`) 분석된 영상에서: 이미지 넣기 → 끌어 옮기기·크기 → 막대 끌기 → 컷 되살리기 후 막대가 따라감 →
  실행 취소 → 로고로 저장 → 다른 영상에 넣기 → 저장 후 결과 mp4 를 `ffprobe`·프레임 한 장 뽑아 이미지 위치 확인. 각 단계 결과를 적어 둔다.
- [ ] **Step 6: 커밋** — `git commit -m "앱 화면: 영상 위 이미지 층·타임라인 이미지 줄·저장한 로고"`
