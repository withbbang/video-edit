# 이어서 재생하는 미리보기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 앱 미리보기를 렌더할 완성 mp4 와 같은 소리·같은 컷으로, 이음새에서 멈춤 없이 이어 재생한다 (맥 WKWebView, 윈도우 WebView2).

**Architecture:** 파이프라인이 미리보기 폴더(`jobs/<id>/preview/`)에 모든 프레임이 기준 프레임인 조각 영상과 전체 소리, 그리고 컷마다
렌더와 같은 방식으로 잘라 만든 소리를 조각 목록(index JSON)과 함께 쓴다. 앱 백엔드는 컷이 바뀔 때마다 잘린 소리를 백그라운드로
만들고, 화면은 MediaSource 에 남길 구간의 영상 조각만 이어 붙이고 잘린 소리를 그대로 넣어 재생한다. 화면의 시간은 원본↔결과 시간 변환을 거친다.

**Tech Stack:** Python 3 (pipeline.py, app/), ffmpeg/libx264/aac, pywebview, 순수 JS (MediaSource Extensions), pytest, node(로컬 JS 단위 시험, 없으면 건너뜀)

**Spec:** `docs/superpowers/specs/2026-10-23-gapless-preview-design.md`

## Global Constraints

- 미리보기 영상: fragmented MP4, `libx264 -preset veryfast -crf 30 -g 1 -bf 0 -pix_fmt yuv420p`, 긴 변 960px 이하(짝수), `-movflags empty_moov+default_base_moof -frag_duration 500000`, 소리 없음.
- 미리보기 소리(전체·잘린 것): AAC `-aac_coder fast -b:a 128k`, 같은 movflags·frag_duration.
- 잘린 소리는 렌더와 같은 소스(`--denoise-level`·`--render-loudnorm` 을 적용한 렌더 오디오)와 같은 함수(`keep_segments` + `cut_audio`, 3ms 페이드)로 만든다.
- 영상 `appendWindowEnd` = 구간 끝 + 미리보기 파일의 1프레임. 소리는 appendWindow 없이 `timestampOffset = 0` 으로 넣는다.
- 버퍼: 재생 위치 뒤 10초 ~ 앞 60초만 유지, `QuotaExceededError` 면 앞 범위를 20초로 줄여 다시 시도.
- 파이프라인 코드는 `scripts/pipeline.py` 한 파일에 둔다 (패키징이 그 파일만 묶는다, `packaging/*.spec`).
- 앱은 사용자 데이터 폴더에만 쓴다. 화면 새 문구는 데스크톱 스펙 3장에도 추가한다.
- CLI 기본 동작·렌더 결과는 바꾸지 않는다. 새 옵션은 앱 전용 숨은 옵션이다.
- ffmpeg 가 필요한 테스트는 `needs_ffmpeg`(`tests/test_denoise_level.py:63` 과 같은 표시)로 CI 에서 건너뛴다. node 가 필요한 테스트는 `shutil.which("node")` 로 건너뛴다.
- 파이프라인·앱을 고친 뒤 전체 테스트: `.venv/bin/python -m pytest -q`.

## Review Focus

- 잘린 구간이 영상 맨 앞·맨 끝에 있거나, 남길 구간이 3프레임처럼 아주 짧을 때 → 구멍 없이 이어지고 끝에서 `ended` 가 난다 (Task 1 의 변환 시험, Task 5 의 node 시험).
- 재생 중에 되살리기·직접 자르기·실행 취소로 컷이 바뀔 때 → 새 소리가 준비될 때까지 지금 미리보기를 계속 틀고, 준비되면 같은 원본 위치에서 이어 재생 (Task 4 의 대기 상태 시험, Task 5 의 수동 확인).
- 같은 영상이 다시 판정·저장 중일 때 잘린 소리를 만들려 함 → 만들지 않고 실패로도 표시하지 않으며, 끝난 뒤 다시 요청하면 만든다 (Task 4 시험).
- 소리만 있는 입력(m4a 등) → 영상 파일 없이 잘린 소리만으로 재생 (Task 2·4 시험).
- 기존 작업(옛 `preview.mp4` 만 있음)을 처음 열 때 → 다시 판정 전까지는 예전 방식으로 재생되고, 새 미리보기가 생기면 옛 파일을 지운다 (Task 4 시험).

---

### Task 1: fMP4 조각 목록 읽기

**Files:**
- Modify: `scripts/pipeline.py` (5) 컷 적용 절 근처, `make_preview` 위)
- Test: `tests/test_preview_gapless.py` (새 파일)

**Interfaces:**
- Produces: `fmp4_index(path: Path) -> dict` → `{"timescale": int, "codec": str, "init": [0, init_end], "frags": [[t_sec: float, start: int, end: int], ...]}`.
  `init_end` = `moov` 끝 바이트(제외), 조각 = `moof` 시작 ~ 뒤따르는 `mdat` 끝. `t_sec` = `traf/tfdt` baseMediaDecodeTime(v0 32비트 / v1 64비트) ÷ `mdhd` timescale(v0 오프셋 12 / v1 오프셋 20).
  `codec`: 영상이면 `avc1.` + avcC 의 profile·compat·level 바이트 16진 6자리(예 `avc1.640020`), 소리(`mp4a`)면 `mp4a.40.2`.
- Produces: `write_index(media: Path, extra: dict | None = None) -> Path` → `media.with_suffix(".index.json")` 에 `fmp4_index(media)` 와 `extra` 를 합쳐 쓰고 그 경로를 돌려준다.

- [ ] **Step 1: 실패하는 테스트 작성** — 테스트 안에 상자 만드는 작은 도우미(`box(type, payload)`)로 `ftyp`+`moov`(`trak/mdia/mdhd` v0 timescale 1000 + `minf/stbl/stsd/avc1/avcC` profile 0x64 compat 0x00 level 0x20)+`moof`(`traf/tfdt` v1 값 0)+`mdat`+`moof`(`tfdt` v0 값 500)+`mdat` 를 바이트로 만든다.

```python
def test_fmp4_index_reads_fragments(tmp_path):
    p = tmp_path / "v.mp4"; p.write_bytes(sample_fmp4())
    idx = pipeline.fmp4_index(p)
    assert idx["timescale"] == 1000 and idx["codec"] == "avc1.640020"
    assert idx["init"] == [0, MOOV_END]
    assert [f[0] for f in idx["frags"]] == [0.0, 0.5]
    assert idx["frags"][0][1] == MOOV_END and idx["frags"][-1][2] == p.stat().st_size

def test_write_index_merges_extra(tmp_path): ...  # extra={"keeps": [[0, 1]]} 이 JSON 에 함께 들어간다
```

- [ ] **Step 2: 실행해서 실패 확인** — `.venv/bin/python -m pytest tests/test_preview_gapless.py -q` → `AttributeError: ... fmp4_index`
- [ ] **Step 3: `fmp4_index`·`write_index` 구현** — `struct` 로 상자 크기(32비트, 1이면 64비트, 0이면 끝까지)·종류를 읽는 반복. 컨테이너(`moov trak mdia minf stbl moof traf`)는 머리 바로 뒤가 자식, `stsd` 는 8바이트, `avc1` 은 78바이트, `mp4a` 는 28바이트 건너뛴 뒤 자식.
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS
- [ ] **Step 5: 커밋** — `git commit -m "파이프라인: fMP4 조각 목록(index) 읽기"`

### Task 2: 미리보기를 폴더로 — 기준 프레임 조각 영상 + 전체 소리

**Files:**
- Modify: `scripts/pipeline.py` — `make_preview`, `make_preview_video`, `try_preview`, `--preview` 도움말, `process()` 의 `if args.preview:` 블록
- Modify: `app/runner.py:44-46` (`--preview` 에 `jd / "preview"`)
- Test: `tests/test_preview_gapless.py`, `tests/test_runner.py:69` 근처

**Interfaces:**
- Consumes: `write_index` (Task 1)
- Produces: `--preview DIR` 는 폴더다. 영상이 있으면 `DIR/video.mp4`, `DIR/video.index.json`(`write_index` extra 로 `"frame": 1/미리보기 fps` — 미리보기 파일의 실제 프레임 간격, ffprobe `r_frame_rate`), 항상 `DIR/audio_full.m4a`, `DIR/audio_full.index.json`. 캐시: `DIR/video.meta.json`(`{"v": 3, "src": source_sig}`), `DIR/audio_full.meta.json`(`{"v": 3, "src", "audio": audio_key}`).
- Produces: `make_preview(src, clean_wav, audio_key, info, out_dir: Path, work: Path, hw: bool, force: bool) -> None`, `make_preview_video(src, info, out_dir: Path, force: bool) -> Path` (VideoToolbox 선택지는 없앤다 — `-g 1` 에선 더 느림), `try_preview(...)` 는 실패 시 `out_dir` 의 미완성 `*.part.*` 와 해당 결과 파일을 지운다.
- Produces: `preview_audio_source(args, src, work, info, clean_wav, audio_key) -> tuple[Path, dict]` — 지금 `if args.preview:` 안의 소리 고르기를 함수로 뺀 것에 `--render-loudnorm` 도 반영(`render_audio_args(args)` 그대로). Task 3 이 같은 함수를 쓴다.

- [ ] **Step 1: 실패하는 테스트 작성** (`@needs_ffmpeg`): ffmpeg `testsrc=size=1920x1080:rate=30` 2초 + `sine` 를 tmp 에 만들고 `info = pipeline.ffprobe(src)`, `pipeline.make_preview(src, wav, {"k": 1}, info, tmp/"preview", tmp/"work", False, False)`.

```python
@needs_ffmpeg
def test_make_preview_writes_intra_fragments(tmp_path):
    ...
    v = tmp_path / "preview" / "video.mp4"
    idx = json.loads((tmp_path / "preview" / "video.index.json").read_text())
    assert len(idx["frags"]) >= 3 and idx["codec"].startswith("avc1.")
    w, h, nkey, nall = probe_frames(v)          # ffprobe -skip_frame nokey 로 기준 프레임 수
    assert max(w, h) == 960 and nkey == nall
    assert idx["frame"] == pytest.approx(1 / 30)
    assert (tmp_path / "preview" / "audio_full.index.json").is_file()

@needs_ffmpeg
def test_make_preview_audio_only(tmp_path): ...   # 영상 없는 입력: video.* 없음, audio_full.* 있음

@needs_ffmpeg
def test_make_preview_reuses_video_when_audio_changes(tmp_path): ...  # audio_key 만 바꾸면 video.mp4 mtime 그대로
```

  `tests/test_runner.py`: `analyze_command` 의 `--preview` 다음 값이 `jd / "preview"` 인지.
- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_preview_gapless.py tests/test_runner.py -q`
- [ ] **Step 3: 구현** — 스케일 식은 지금 식에서 1280 → 960. 영상·소리 모두 `*.part.mp4`/`*.part.m4a` 로 쓴 뒤 `os.replace`, 그다음 `write_index`, 마지막에 meta. 진행률 구간은 지금처럼 영상 0~0.9, 소리 0.9~1.0.
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS, 그리고 전체 `.venv/bin/python -m pytest -q`
- [ ] **Step 5: 커밋** — `git commit -m "파이프라인: 미리보기를 기준 프레임 조각 영상·전체 소리 폴더로"`

### Task 3: 잘린 미리보기 소리 (`--preview-cut-audio`)

**Files:**
- Modify: `scripts/pipeline.py` — `build_parser`, `main`(검사), `process()`(컷 리스트 다음, `--review` 반환 앞)
- Modify: `CLAUDE.md` 데스크톱 앱 절의 숨은 옵션 목록 (`--preview` 는 폴더, `--preview-cut-audio`)
- Test: `tests/test_preview_gapless.py`, `tests/test_pipeline_args.py`

**Interfaces:**
- Consumes: `preview_audio_source` (Task 2), `write_index` (Task 1), 기존 `keep_segments`, `cut_audio`
- Produces: `make_cut_preview_audio(p_wav: Path, keeps: list[tuple[int, int]], fps: Fraction, out: Path, work: Path) -> dict` — `work/preview_cut.wav` 로 자르고 AAC fMP4 로 `out` 에 쓴 뒤 `write_index(out, {"keeps": [[sf/fps, ef/fps] (소수 6자리)], "duration": n_samples/sr})`. 반환은 쓴 index 내용.
- Produces: CLI `--preview-cut-audio OUT` — `--apply-cuts` 와 함께만 (아니면 `sys.exit("오류: --preview-cut-audio 는 --apply-cuts 와 함께 씁니다")`), `--review` 와 함께 못 씀. 컷을 불러오고 잘린 소리만 만든 뒤 `log("미리보기 소리: …")` 하고 반환 (SRT·렌더·리포트 없음).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_preview_cut_audio_requires_apply_cuts(): ...      # main() 이 SystemExit, 메시지에 "--apply-cuts"

@needs_ffmpeg
def test_make_cut_preview_audio_matches_keeps(tmp_path):
    wav = sine_wav(tmp_path / "a.wav", sec=4.0, sr=48000)
    fps = Fraction(30)
    keeps = [(0, 30), (45, 48), (90, 120)]                 # 1초, 3프레임, 1초
    idx = pipeline.make_cut_preview_audio(wav, keeps, fps, tmp_path / "audio_cut.x.m4a", tmp_path)
    assert idx["keeps"] == [[0.0, 1.0], [1.5, 1.6], [3.0, 4.0]]
    assert abs(idx["duration"] - 2.1) < 1e-3                # cut_audio 샘플 수 기준
    assert probe_duration(tmp_path / "audio_cut.x.m4a") == pytest.approx(2.1, abs=0.05)
    assert idx["frags"] and idx["codec"] == "mp4a.40.2"
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_preview_gapless.py tests/test_pipeline_args.py -q`
- [ ] **Step 3: 구현** — `process()` 에서 `if args.preview_cut_audio:` 가 `keeps = keep_segments(cuts, total_frames)`, `p_wav, _ = preview_audio_source(...)`, `make_cut_preview_audio(...)` 후 return. 끝나면 `progress("preview", 1.0)`.
- [ ] **Step 4: 통과 확인** — 같은 명령, 그리고 전체 pytest
- [ ] **Step 5: 커밋** — `git commit -m "파이프라인: 컷을 적용한 미리보기 소리(--preview-cut-audio)"`

### Task 4: 앱 백엔드 — 잘린 소리 만들기·내주기

**Files:**
- Create: `app/preview.py`
- Modify: `app/runner.py` (`preview_audio_command`), `app/api.py` (`get_result`, 생성자, `Api` 에 매니저 연결), `app/window.py` (`_PREVIEW_PATH`, `_file`, Content-Type)
- Test: `tests/test_preview_audio.py` (새 파일), `tests/test_window.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: CLI `--preview-cut-audio` (Task 3), `diff.effective_cuts`, `save_cuts` 와 같은 컷 계산
- Produces: `runner.preview_audio_command(job: Job, model: ModelPaths, device: str, cuts_path: Path, out: Path) -> list[str]` = `pipeline_cmd() + [src, "--apply-cuts", cuts, "--preview-cut-audio", out, "--progress-json", "--out", jd/"out", "--work-root", jd/"work"] + _tail(...) + ["--denoise-level", str(o.denoise), "--render-loudnorm", "on" if o.loudnorm == "match" else "off"]`.
- Produces: `preview.cut_key(cuts: list[dict], options: JobOptions) -> str` = 컷 `[start, end]`(소수 6자리) 목록·`denoise`·`loudnorm` JSON 의 sha1 앞 16자리.
- Produces: `class preview.CutAudio(run: Callable[[list[str]], RunResult], on_ready: Callable[[str], None], keep: int = 3)` —
  `request(job_id: str, key: str, cmd: list[str], out: Path) -> str` 는 `"ready" | "pending" | "failed"`. 파일(`out` 과 `out.index.json`)이 있으면 ready. 없으면 작업 스레드 하나에 넣고(같은 job 의 이전 대기 요청은 최신으로 바꿈) pending. 끝나면 `on_ready(job_id)`, 실패면 그 key 를 failed 로 기억. 성공하면 같은 폴더의 `audio_cut.*` 를 최신 `keep` 개만 남긴다. `shutdown()` 은 돌고 있는 프로세스를 멈춘다(`Running.cancel`).
- Produces: `get_result()` 의 `"preview"` = `{"video": url|None, "video_index": url|None, "audio_full": url, "audio_full_index": url, "cut_audio": url|None, "cut_index": url|None, "pending": bool}` (URL 은 `/<id>/preview/<이름>?v=<mtime_ns>`), 미리보기 폴더가 없으면 `"preview": None`. 기존 `preview_url` 은 옛 `preview.mp4` 만 있는 작업에만 준다(예전 방식). 새 폴더에 `audio_full.index.json` 이 있으면 옛 `jobs/<id>/preview.mp4` 를 지운다.
  잘린 소리 요청은 job 상태가 `REVIEW` 또는 `SAVED` 일 때만 한다. 컷 목록 파일은 `out/<이름>.preview.cuts.json` (`diff.effective_cuts` 결과, 되살림·구간 작업 없으면 판정 컷 그대로).
- Produces: push 이벤트 `"preview"` `{"id": job_id}`.
- Produces: 미리보기 서버 경로 `/<12hex>/preview/(video\.mp4|video\.index\.json|audio_full\.m4a|audio_full\.index\.json|audio_cut\.[0-9a-f]{16}\.m4a|audio_cut\.[0-9a-f]{16}\.index\.json)`, Content-Type `.mp4` video/mp4, `.m4a` audio/mp4, `.json` application/json. 옛 `/<id>/preview.mp4` 도 계속 내준다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_cut_key_changes_with_cuts_and_denoise(): ...
def test_request_ready_when_files_exist(tmp_path): ...            # run 을 부르지 않음
def test_request_runs_once_and_notifies(tmp_path): ...            # 가짜 run 이 out·index 를 만들고 ok → on_ready("j1"), 두 번째 request 는 ready
def test_latest_request_wins(tmp_path): ...                       # 대기 중 두 요청 → 마지막 cmd 만 실행
def test_failed_key_is_remembered(tmp_path): ...                  # run 실패 → 같은 key 는 "failed", 다른 key 는 다시 pending
def test_prunes_old_cut_audio(tmp_path): ...                      # 5개 만들면 최근 3쌍만 남음
# test_api.py
def test_get_result_preview_fields_and_pending(...): ...          # 폴더 있음 → cut_audio None, pending True, 요청 1번
def test_get_result_skips_cut_audio_while_processing(...): ...    # status PROCESSING → 요청 안 함, pending False
def test_get_result_audio_only(...): ...                          # video None, audio_full 있음
def test_get_result_old_preview_only(...): ...                    # preview None, preview_url 있음
def test_get_result_removes_old_preview_mp4(...): ...
# test_window.py
@pytest.mark.parametrize("name,ctype", [("video.mp4","video/mp4"), ("audio_full.m4a","audio/mp4"),
    ("audio_cut.0123456789abcdef.index.json","application/json")])
def test_preview_dir_files(served, name, ctype): ...
def test_preview_dir_rejects_other_names(served): ...             # "../state.json", "audio_cut.zz.m4a", "x.mp4" → 404
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_preview_audio.py tests/test_window.py tests/test_api.py -q`
- [ ] **Step 3: 구현** — `CutAudio` 는 데몬 스레드 + `threading.Condition`, 실행은 `runner.run(cmd, {"preview": 1.0}, lambda p: None, env=…)` 를 감싼 `run` 콜백(api 가 `models.pipeline_env` 와 함께 만들어 넘김). 파이프라인이 `오류: 같은 영상을 이미 처리 중` 으로 끝나면 failed 로 기억하지 않는다(다음 요청 때 다시). `window.run` 의 `stop()` 에서 `shutdown()`.
- [ ] **Step 4: 통과 확인** — 같은 명령, 그리고 전체 pytest
- [ ] **Step 5: 커밋** — `git commit -m "앱: 컷을 적용한 미리보기 소리를 백그라운드로 만들어 내주기"`

### Task 5: 화면 — MediaSource 로 이어 붙여 재생

**Files:**
- Create: `app/ui/player.js` (MediaSource 재생과 시간 변환. `app.js` 앞에서 불러옴)
- Modify: `app/ui/index.html:53` (`<script src="player.js">` 추가), `app/ui/app.js` (`player` 객체 900~1000줄대, `now()` 821줄, 741줄, `push` 이벤트 처리), `app/ui/style.css`
- Modify: `docs/superpowers/specs/2026-10-22-desktop-app-design.md` 3장 (새 문구 `미리보기 소리를 맞추는 중…`, 미리보기가 결과 시간으로 재생되고 기본 컨트롤은 결과 기준이라는 설명)
- Test: `tests/test_player_js.py` (node 로 `player.js` 의 순수 함수 시험, node 없으면 건너뜀)

**Interfaces:**
- Consumes: `get_result().preview` (Task 4), push `"preview"`, index JSON(`init`, `frags`, `codec`, `keeps`, `duration`)
- Produces (`player.js`, 브라우저 전역 + node 용 `module.exports`):
  - `timeMap(keeps: [number, number][]) -> {total: number, toOut(t: number): number, toOrig(o: number): number, inCut(t: number): boolean}` — 잘린 구간 안의 원본 시간 `toOut` 은 그 구간 끝(다음 남길 구간 시작)의 결과 시간, 마지막 잘린 구간이면 `total`. `toOrig(total)` 은 마지막 남길 구간 끝.
  - `fragsFor(frags, s: number, e: number) -> [start, end][]` — `[s, e)` 와 겹치는 조각의 바이트 범위 (조각 끝 시간 = 다음 조각 시작, 마지막은 무한).
  - `class Gapless { constructor(video: HTMLVideoElement); load({videoUrl, videoIndex, audioUrl, audioIndex}): Promise<void>; destroy(): void }` — 영상 없는 입력이면 `videoUrl` 없이 `video.src = audioUrl` 로 바로 재생. 영상 append: 남길 구간 i 마다 `timestampOffset = o − s`, `appendWindowStart = o`, `appendWindowEnd = o + (e − s) + videoIndex.frame`(미리보기 파일의 1프레임 길이, Task 2), 소리는 `timestampOffset = 0`·appendWindow 없이 순서대로. 버퍼 범위·Quota 처리는 Global Constraints. 바이트는 `fetch(url, {headers: {Range: "bytes=a-b"}})`.
- Produces (`app.js` 의 `player`): `time(): number`(원본 초), `playing(): boolean`, `seek(t)`, `clip(start, end, skip)` (기존 이름 유지). 요소: MediaSource 재생기 `<video>`(기본 컨트롤, 결과 시간), 원본 보기 `<video muted>`(조각 영상 파일 직접, 멈춘 채 잘린 구간 안을 보거나 예전 방식일 때), 예전 방식용 `<audio>`(`audio_full.m4a`).
  - 멈춘 상태에서 `seek(t)` 의 `t` 가 잘린 구간 안이면 원본 보기 요소를 `t` 로 옮겨 보이고, 재생을 누르면 MediaSource 재생기를 `toOut(t)` 로 옮겨 보이고 재생.
  - `clip(start, end)`: `seek(start − 2)` 후 재생, 결과 시간이 `toOut(end + 2)` 에 닿으면 멈춤.
  - `drawHead`·`drawSub`·`지금 위치로`(`now()`)·재생 중 자막 수정 막기는 `player.time()`·`player.playing()` 을 쓴다.
  - 결과가 바뀌어 `cut_audio` URL 이 달라지면 원본 위치·재생 여부를 기억하고 새로 `load` 한 뒤 같은 원본 위치에서 이어 재생. `pending` 이면 지금 미리보기를 그대로 두고 재생기 아래에 `미리보기 소리를 맞추는 중…` 을 보인다. push `"preview"` 가 오면 선택된 영상이면 `loadResult`.
  - 예전 방식(`MediaSource` 없음·`isTypeSupported(codec)` 거짓·append 오류·`preview` 가 None 이고 `preview_url` 만 있음): 지금의 건너뛰기 재생. 새 폴더면 원본 보기 요소 + `<audio>` 를 같이 재생하고 건너뛸 때 둘의 `currentTime` 을 같이 옮긴다.

- [ ] **Step 1: 실패하는 테스트 작성** (`tests/test_player_js.py`, node 로 `require("app/ui/player.js")` 하는 짧은 스크립트를 `subprocess.run(["node", "-e", …])` 으로 돌려 JSON 으로 받음)

```python
@needs_node
def test_time_map_basic():
    m = js("timeMap([[0.5, 2.0], [3.0, 4.0]])", "toOut", [0.0, 0.5, 1.0, 2.5, 3.0, 4.0, 9.0])
    assert m == pytest.approx([0.0, 0.0, 0.5, 1.5, 1.5, 2.5, 2.5])
    assert js_total("[[0.5, 2.0], [3.0, 4.0]]") == pytest.approx(2.5)
    assert js("timeMap([[0.5, 2.0], [3.0, 4.0]])", "toOrig", [0.0, 1.5, 2.5]) == pytest.approx([0.5, 3.0, 4.0])
@needs_node
def test_time_map_cut_at_end_and_short_keep(): ...     # [[0,1],[1.5,1.6]] → toOut(1.55)=1.05, toOut(5)=1.1, inCut(1.2)=True
@needs_node
def test_frags_for(): ...                              # frags [[0,100,200],[0.5,200,300],[1.0,300,400]], (0.4,0.6) → [[100,199],[200,299]]
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_player_js.py -q`
- [ ] **Step 3: `player.js` 의 `timeMap`·`fragsFor`·`Gapless` 구현, `app.js` 연결, 스타일·스펙 문구 추가**
- [ ] **Step 4: 통과 확인** — 같은 명령, 그리고 전체 pytest, `node --check app/ui/player.js app/ui/app.js`
- [ ] **Step 5: 앱에서 확인 (맥)** — `YTEDIT_DEBUG=1 .venv/bin/python -m app` 로 3.mp4 를 다시 판정해 새 미리보기를 만든 뒤:
  - 개발자 도구 콘솔에서 `requestVideoFrameCallback` 간격 측정 스니펫(시험판 `spike/index.html` 의 `watch()` 와 같음)을 MediaSource 재생기 요소에 붙이고 앞 40초를 재생 → 50ms 넘는 간격이 1번 이하(첫 재생의 일시적 멈춤 허용), `video.buffered` 에 구멍 없음.
  - 잘린 곳 하나를 되살림 → `미리보기 소리를 맞추는 중…` 이 잠깐 뜨고, 같은 원본 위치에서 이어 재생되며 그 구간이 들린다.
  - 멈춘 채 타임라인의 잘린 구간 안을 누름 → 그 자리 화면이 보이고, 재생하면 그 구간 끝부터 재생.
  - 영상 아래 기본 컨트롤 길이가 요약의 결과 길이(02:48)와 같다.
- [ ] **Step 6: 커밋** — `git commit -m "앱: 미리보기를 MediaSource 로 이어 붙여 완성본처럼 재생"`

### Task 6: 문서와 윈도우 확인

**Files:**
- Modify: `README.md` (데스크톱 앱 쓰는 법의 미리보기 설명, "파일 위치"의 미리보기 크기 — 5분 약 175MB, 1시간 약 2.1GB)

- [ ] **Step 1: README 갱신** — 미리보기가 완성본과 같은 소리로 이어 재생된다는 것, 디스크 사용량.
- [ ] **Step 2: 전체 테스트** — `.venv/bin/python -m pytest -q` → 모두 통과
- [ ] **Step 3: 사용자에게 윈도우 확인 요청** — 사용자가 윈도우 PC 에서 개발 실행(`.venv\Scripts\python -m app`) 또는 설치 파일로 같은 확인(Task 5 Step 5 의 앞 세 항목)을 하고 결과를 알려 준다. 태그 push·릴리스는 하지 않는다.
- [ ] **Step 4: 커밋** — `git commit -m "문서: 이어서 재생하는 미리보기"`
