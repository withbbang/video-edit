# youtube-edit 데스크톱 앱 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 파이썬을 모르는 사람이 맥·윈도우에서 설치 파일로 설치해, 영상을 끌어다 놓고 옵션을 고르고, 결과를 미리 들어본 뒤 저장하는 데스크톱 앱.

**Architecture:** `app/` 파이썬 패키지가 pywebview 창(HTML/JS 화면)과 작업 대기열을 맡고, 영상마다 기존 `scripts/pipeline.py` 를 **별도 프로세스**로 실행한다. 파이프라인에는 기본값이 지금과 같은 옵션만 더한다(CLI 동작 불변). PyInstaller 로 묶어 GitHub Actions 가 `v*` 태그에서 .dmg / 설치 .exe 를 릴리스에 올린다.

**Tech Stack:** Python 3.11, pywebview 5.x(맥 WKWebView, 윈도우 WebView2), faster-whisper, torch(CPU), huggingface_hub, pytest, PyInstaller, Inno Setup, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-22-desktop-app-design.md` (화면 시안: `.superpowers/brainstorm/7684-1792625391/content/layout-v5.html`, `queue-v2.html`, `first-run-v3.html`)

## Global Constraints

- 기존 CLI `scripts/pipeline.py` 와 `/youtube-clean` 스킬 동작을 바꾸지 않는다. 새 옵션은 모두 기본값이 현재 동작.
- 사용자 원본 파일은 읽기만 한다. 앱 설치 폴더(맥 .app 내부)에 쓰지 않는다.
- 사용자 데이터 폴더: 맥 `~/Library/Application Support/youtube-edit/`, 윈도우 `%APPDATA%\youtube-edit\`.
- 기본 저장 위치: 맥 `~/Movies/youtube-edit`, 윈도우 `%USERPROFILE%\Videos\youtube-edit`. 저장 폴더엔 `<이름>_clean.mp4`(오디오 원본은 `.m4a`)와 `<이름>_clean.srt` 만. 같은 이름은 덮어씀.
- 화면 문구는 스펙 3장 표·문구를 글자 그대로 쓴다 (예: `망설임 소리`, `아·어·음…`, `유지 / 제거`, `유지 / 일부 / 모두`, `빼기 / 넣기`, `작게 / 자동 / 높게`, `설정`, `시작`, `저장`, `＋ 끌어다 놓거나 파일 선택`, `대기`, `처리 중 N%`, `확인 대기`, `저장 중 N%`, `✓ 저장됨`, `실패`, `원본 없음`, `N곳 잘림`, `■ 잘린 곳`, `■ 달라진 곳`, `추가 잘림`, `다시 살림`, `▶ 원본 · ▶ 수정본`, `+ 더 보기`, `모델 내려받는 중`, `모델 내려받기 실패`, `인터넷 연결을 확인해 주시기 바랍니다`, `다시 시도`).
- 색: 잘린 곳 `#d93036`, 달라진 곳·추가 잘림 `#e07b00`, 다시 살림 `#2a9d5c`. 시작·저장 버튼은 오른쪽 정렬.
- 대상 플랫폼: macOS Apple Silicon(arm64) 전용, Windows x64. 인텔 맥 미지원 ("Apple Silicon(M1 이후) 전용" 표기).
- 라이선스 GPL-3.0. 설치 파일에 ffmpeg 라이선스 문구와 소스 위치 포함.
- 업데이트 확인 대상: GitHub `withbbang/video-edit` 릴리스, 켤 때 + 24시간마다.
- 화질 배율: 작게 0.6배, 자동 1배, 높게 1.5배 (`auto_bitrate` 기준).
- 분석은 항상 배경 잡음 rnnoise + 음량 맞춤으로 정리한 오디오로 한다. 사용자의 배경 잡음·음량 선택은 저장(렌더) 때만 적용.

## Review Focus

1. **한글·공백·이모지가 든 경로** (`~/바탕 화면/강의 03 (최종).mov`, 윈도우 `C:\Users\김영선\...`) — 드롭, 처리, 미리보기 재생, 저장이 모두 된다. → Task 3 `test_job_dir_unicode_path`, Task 6 `test_command_unicode_path`.
2. **같은 파일 이름이 다른 폴더에서 두 개 들어옴** — 캐시가 섞이지 않고, 저장 시 같은 이름이면 나중 것이 덮어쓴다. → Task 3 `test_job_dir_same_name_different_folder`.
3. **처리 중 앱 종료 후 다시 켬 / 원본이 지워짐** — `처리 중`·`저장 중` 은 `대기` 로, 원본 없으면 `원본 없음`. → Task 4 `test_restore_resets_running_and_marks_missing`.
4. **파이프라인 프로세스가 오류·크래시로 끝남** (윈도우 ctranslate2 크래시, ffmpeg 실패, "남길 구간이 없습니다") — 해당 영상만 `실패` + 마지막 오류 줄, 대기열은 다음으로. → Task 6 `test_runner_failure_reports_last_error`, Task 7 `test_queue_continues_after_failure`.
5. **오프라인 상태로 첫 실행 / 업데이트 확인** — 모델 받기는 실패 화면 + 다시 시도, 업데이트 확인은 조용히 넘김. → Task 9 `test_download_failure_raises_retryable`, Task 10 `test_check_offline_returns_none`.

---

## 파일 구조

```
app/
  __init__.py        __version__ = "0.1.0"
  __main__.py        진입점: 인자 없으면 창, "--pipeline ..." 이면 pipeline.main(), "--probe-gpu" 면 GPU 시험
  paths.py           데이터 폴더·저장 폴더·번들 폴더·ffmpeg 경로
  options.py         JobOptions(화면 옵션) ↔ 파이프라인 인자 변환
  store.py           Job 상태 모델, state.json 저장/복원
  progress.py        파이프라인 진행 줄(@progress) 파싱, 단계 가중치
  runner.py          파이프라인 하위 프로세스 실행·중단·진행 콜백
  jobqueue.py        한 줄 대기열 (저장 우선), 작업 스레드
  diff.py            두 cuts.json 비교 → 변화 목록
  models.py          모델 manifest, 상태 확인, 내려받기, 정리, 윈도우 CUDA 라이브러리
  updates.py         GitHub 릴리스 확인, 버전 비교, 설치 파일 내려받기
  api.py             pywebview js_api (화면 ↔ 파이썬)
  window.py          창 생성, 드롭 이벤트, 파일 대화상자, 미리보기 파일 제공
  manifest.json      모델 목록 (저장소·리비전·크기), CUDA 휠
  ui/index.html, ui/app.js, ui/style.css
scripts/pipeline.py  (수정) 경로·진행·분석/출력 오디오 분리·미리보기·모델 경로 옵션
tests/               pytest
packaging/
  youtube-edit.spec  PyInstaller
  fetch_ffmpeg.py    CI 에서 플랫폼별 ffmpeg/ffprobe 받기
  macos/make_dmg.sh
  windows/installer.iss
.github/workflows/release.yml
requirements-app.txt  pywebview, pyinstaller (앱 빌드용)
requirements-dev.txt  pytest
LICENSE               GPL-3.0
```

`app/` 모듈은 파이프라인을 import 하지 않는다 (하위 프로세스로만 실행). 예외: `__main__.py` 의 `--pipeline` 모드.

---

### Task 1: 파이프라인 — 경로 옵션과 미디어 출력 위치

**Files:**
- Modify: `scripts/pipeline.py` (`main`, `process`, `probe_whisper_windows`, `CtcAligner.__init__`)
- Create: `tests/conftest.py`, `tests/test_pipeline_args.py`, `requirements-dev.txt` (`pytest>=8`)

**Interfaces:**
- Produces (CLI, 기본값 = 현재 동작):
  - `--work-root PATH` (기본 `ROOT/"work"`): 작업 캐시 위치. `work = work_root / stem`, probe 기록 `work_root/".whisper_probe.json"`.
  - `--media-out DIR` (기본 = `--out`): `_clean.mp4/.m4a` 와 `_clean.srt` 를 둘 폴더. 나머지 산출물은 `--out`.
  - `--ctc-model ID_OR_PATH` (기본 `CTC_MODEL`): 정렬 모델. `--model` 은 이미 로컬 폴더 경로를 받음(faster-whisper).
- Produces (Python): `pipeline.build_parser() -> argparse.ArgumentParser` (`main()` 에서 분리, 테스트용).
- `tests/conftest.py`: `scripts/` 를 `sys.path` 에 넣어 `import pipeline` 가능하게.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_defaults_unchanged():
    a = pipeline.build_parser().parse_args(["x.mp4"])
    assert a.work_root == pipeline.ROOT / "work"
    assert a.media_out is None and a.ctc_model == pipeline.CTC_MODEL

def test_new_paths_parse(tmp_path):
    a = pipeline.build_parser().parse_args(["x.mp4", "--work-root", str(tmp_path), "--media-out", str(tmp_path / "m"), "--ctc-model", "/m/ctc"])
    assert a.work_root == tmp_path and a.media_out == tmp_path / "m" and a.ctc_model == "/m/ctc"
```

- [ ] **Step 2:** `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests/test_pipeline_args.py -v` → FAIL (`build_parser` 없음)
- [ ] **Step 3: 구현.** `CtcAligner(model_id: str)` 로 바꿔 `ctc_fix_fillers` 에 인자로 전달. `probe_whisper_windows` 에 `marker: Path` 인자. `process` 에서 `out_media = (args.media_out or out_dir) / ...`, `media_out` 는 `mkdir(parents=True, exist_ok=True)`.
- [ ] **Step 4:** 테스트 PASS
- [ ] **Step 5: 회귀 확인.** `output/test.transcript.json` 을 `<scratch>/before`, `<scratch>/after` 에 복사. 변경 전 상태(`git stash`)에서 `.venv/bin/python scripts/pipeline.py input/test.mp4 --review --out <scratch>/before`, 변경 후 `--out <scratch>/after` → `diff <(jq .cuts <scratch>/before/test.cuts.json) <(jq .cuts <scratch>/after/test.cuts.json)` 출력 없음.
- [ ] **Step 6: 커밋** `git commit -m "pipeline: --work-root, --media-out, --ctc-model 옵션"`

### Task 2: 파이프라인 — 진행 출력, 분석/출력 오디오 분리, 미리보기 사본, 화질 배율

**Files:**
- Modify: `scripts/pipeline.py`
- Test: `tests/test_pipeline_features.py`

**Interfaces:**
- Produces (CLI, 기본값 = 현재 동작):
  - `--progress-json`: 켜면 stdout 에 `@progress {"stage": S, "frac": F}` 줄을 낸다. `S ∈ {"audio","transcribe","align","judge","preview","render"}`, `F` 0~1. 기존 `log` 줄은 그대로.
  - `--render-denoise {same,rnnoise,afftdn,none}` 기본 `same`, `--render-loudnorm {same,on,off}` 기본 `same`. `same` 이 아니고 분석 설정과 다르면 렌더용 오디오 `work/render.wav` 를 따로 만든다 (키 `work/render.meta.json`, `prepare_audio` 와 같은 캐시 방식). 컷 적용(`cut_audio`)은 이 파일로.
  - `--preview PATH`: 판정 뒤(렌더 전, `--review` 에서도) 원본 타임라인 그대로의 미리보기 사본을 만든다: 긴 변 1280 이하, H.264(가능하면 `h264_videotoolbox`, 실패 시 `libx264 -preset veryfast -crf 28`), 오디오는 `clean.wav` AAC 128k, `-movflags +faststart`. 원본 크기·mtime·`clean.wav` 키가 같으면 다시 만들지 않는다. 오디오만인 원본이면 오디오만 m4a.
  - `--vbitrate-scale FLOAT` 기본 `1.0`: `auto_bitrate` 결과에 곱한다 (`--vbitrate` 가 `auto` 일 때만).
- Produces (Python): `progress(stage: str, frac: float) -> None` (플래그 꺼져 있으면 아무것도 안 함), `ffmpeg_with_progress(cmd: list, total_sec: float, stage: str) -> None` (`-progress pipe:1 -nostats` 붙여 `out_time_us` 로 frac 계산, 실패 시 `CalledProcessError`).
- 진행 지점: `prepare_audio` 끝 audio 1.0, `transcribe` 세그먼트마다 `seg.end / duration`, 캐시 재사용 시 transcribe 1.0 즉시, `ctc_fix_fillers` 끝 align 1.0, 컷 판정 끝 judge 1.0, 미리보기·렌더는 `ffmpeg_with_progress`.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_progress_line(capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "PROGRESS", True)
    pipeline.progress("transcribe", 0.5)
    assert capsys.readouterr().out.strip() == '@progress {"stage": "transcribe", "frac": 0.5}'

def test_progress_silent_by_default(capsys):
    pipeline.progress("audio", 1.0)
    assert capsys.readouterr().out == ""

def test_render_audio_defaults_same():
    a = pipeline.build_parser().parse_args(["x.mp4"])
    assert (a.render_denoise, a.render_loudnorm, a.vbitrate_scale, a.preview) == ("same", "same", 1.0, None)

def test_bitrate_scale():
    info = {"width": 1920, "height": 1080, "fps": Fraction(30), "high_bit": False}
    assert pipeline.auto_bitrate(info) == "8M"
    assert pipeline.auto_bitrate(info, scale=0.6) == "4M"   # 하한 4M
    assert pipeline.auto_bitrate(info, scale=1.5) == "12M"
```

- [ ] **Step 2:** `pytest tests/test_pipeline_features.py -v` → FAIL
- [ ] **Step 3: 구현.** 전역 `PROGRESS = False` 를 `main` 에서 플래그로 설정. `auto_bitrate(info, scale=1.0)`. `render_video` 와 미리보기 ffmpeg 는 `ffmpeg_with_progress` 로.
- [ ] **Step 4:** 테스트 PASS
- [ ] **Step 5: 통합 확인 (합성 신호 금지 — RNNoise 가 지움. 실제 영상으로).**
  `.venv/bin/python scripts/pipeline.py input/test.mp4 --out <scratch>/t2 --progress-json --preview <scratch>/t2/p.mp4 --render-denoise none --render-loudnorm off --vbitrate-scale 0.6`
  → `@progress` 줄이 단계 순서대로 나오고 마지막 render 1.0; `ffprobe p.mp4` 길이 = 원본 길이(±1프레임); `work/test/render.wav` 존재; CLAUDE.md 의 렌더 검증 명령으로 영상·오디오 길이 차 1프레임 이내.
- [ ] **Step 6:** Task 1 Step 5 회귀 확인 다시 (cuts 동일).
- [ ] **Step 7: 커밋** `git commit -m "pipeline: 진행 출력, 렌더용 오디오 분리, 미리보기 사본, 화질 배율"`

### Task 3: 앱 경로와 옵션 변환

**Files:**
- Create: `app/__init__.py`, `app/paths.py`, `app/options.py`
- Test: `tests/test_paths.py`, `tests/test_options.py`

**Interfaces:**
- `paths.data_dir() -> Path` (플랫폼별, 없으면 생성; 테스트용 환경변수 `YTEDIT_DATA_DIR` 가 있으면 그것)
- `paths.default_save_dir() -> Path`
- `paths.bundle_dir() -> Path` (PyInstaller `sys._MEIPASS` 또는 저장소 루트)
- `paths.job_dir(src: Path) -> Path` = `data_dir()/"jobs"/<sha1(정규화된 절대 경로)[:12]>`
- `paths.ffmpeg_bin_dir() -> Path | None` (번들에 `bin/` 있으면 그 경로, 개발 중엔 None → PATH 사용)
- `options.JobOptions` dataclass: `filler: str="remove"`, `habit: str="some"` (`keep|some|all`), `silence: str="remove"`, `denoise: str="remove"`, `loudnorm: str="match"` (`keep|match`), `subs: str="embed"` (`omit|embed`), `quality: str="auto"` (`low|auto|high`). `to_dict()/from_dict()`.
- `options.analysis_args(o: JobOptions) -> list[str]`: `--filler on|off --habit off|on|aggressive --silence on|off` (분석은 denoise/loudnorm 기본값 그대로).
- `options.render_args(o: JobOptions) -> list[str]`: `--render-denoise rnnoise|none --render-loudnorm on|off --embed-subs on|off --vbitrate-scale 0.6|1.0|1.5`.
- `options.FAST_KEYS = {"filler","habit","silence"}` (바꾸면 다시 판정), 나머지는 저장 때만.
- `paths.ModelPaths(whisper: str, ctc: str)` dataclass (모델 로컬 경로; Task 9 가 채움).

- [ ] **Step 1: 실패하는 테스트**

```python
def test_job_dir_same_name_different_folder(tmp_path):
    assert paths.job_dir(tmp_path/"a"/"강의.mp4") != paths.job_dir(tmp_path/"b"/"강의.mp4")

def test_job_dir_unicode_path(tmp_path):
    p = tmp_path/"바탕 화면"/"강의 03 (최종).mov"
    assert paths.job_dir(p) == paths.job_dir(p)  # 안정적
    assert paths.job_dir(p).name.isascii()

def test_analysis_args_defaults():
    assert options.analysis_args(options.JobOptions()) == ["--filler","on","--habit","on","--silence","on"]

def test_analysis_args_all_habit_keep_filler():
    o = options.JobOptions(filler="keep", habit="all", silence="keep")
    assert options.analysis_args(o) == ["--filler","off","--habit","aggressive","--silence","off"]

def test_render_args():
    o = options.JobOptions(denoise="keep", loudnorm="keep", subs="omit", quality="low")
    assert options.render_args(o) == ["--render-denoise","none","--render-loudnorm","off","--embed-subs","off","--vbitrate-scale","0.6"]
```

- [ ] **Step 2:** FAIL 확인 → **Step 3:** 구현 → **Step 4:** PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 경로와 옵션 변환"`

### Task 4: 작업 상태 저장/복원

**Files:**
- Create: `app/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- `store.Status` 문자열 상수: `WAITING="waiting"`, `PROCESSING="processing"`, `REVIEW="review"`, `SAVING="saving"`, `SAVED="saved"`, `FAILED="failed"`, `MISSING="missing"`.
- `store.Job` dataclass: `id: str` (= `paths.job_dir(src).name`), `src: str`, `options: JobOptions`, `status: str`, `progress: float = 0.0`, `error: str = ""`, `summary: dict | None` (`{"orig_sec","final_sec","cuts","removed_sec"}`), `prev_cuts: str | None` (직전 판정 cuts.json 경로, diff 기준).
- `store.Store(path: Path)`: `jobs: list[Job]`, `settings: dict` (`save_dir`, `dismissed_version`, `gpu`), `add(src: Path) -> Job | None` (이미 있으면 None), `remove(id)`, `get(id)`, `save()` (임시 파일에 쓰고 교체), `@classmethod load(path) -> Store`.
- `load` 규칙: `processing`/`saving` → `waiting`, progress 0; 원본 없으면 `missing`; 파일 깨졌으면 빈 Store.

- [ ] **Step 1: 실패하는 테스트**

```python
def test_roundtrip(tmp_path): ...  # add 두 개, 옵션 바꿈, save, load → 같은 내용

def test_restore_resets_running_and_marks_missing(tmp_path):
    a, b, c = (tmp_path / n for n in ("a.mp4", "b.mp4", "c.mp4"))
    for p in (a, b, c): p.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    ja, jb, jc = s.add(a), s.add(b), s.add(c)
    ja.status, jb.status, jc.status = "processing", "saving", "review"
    s.save(); c.unlink()
    r = Store.load(tmp_path / "state.json")
    assert [j.status for j in r.jobs] == ["waiting", "waiting", "missing"]

def test_add_duplicate_returns_none(tmp_path): ...

def test_corrupt_state_gives_empty(tmp_path):
    (tmp_path/"state.json").write_text("{")
    assert Store.load(tmp_path/"state.json").jobs == []
```

- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 작업 상태 저장/복원"`

### Task 5: 진행 파싱

**Files:**
- Create: `app/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- `progress.ANALYZE_WEIGHTS = {"audio": .10, "transcribe": .60, "align": .05, "judge": .05, "preview": .20}`, `RENDER_WEIGHTS = {"audio": .10, "render": .90}` (렌더용 오디오가 캐시면 audio 는 즉시 1.0).
- `progress.Tracker(weights: dict)`: `feed(line: str) -> float | None` — `@progress` 줄이면 전체 비율(0~1, 단조 증가) 반환, 아니면 None. 앞 단계는 뒤 단계가 나오면 1.0 으로 간주.

- [ ] **Step 1: 테스트**

```python
def test_weighted():
    t = Tracker(ANALYZE_WEIGHTS)
    assert t.feed('@progress {"stage": "audio", "frac": 1.0}') == pytest.approx(.10)
    assert t.feed('@progress {"stage": "transcribe", "frac": 0.5}') == pytest.approx(.40)
    assert t.feed("[12:00:00] 로그") is None

def test_skipped_stage_counts_done():
    t = Tracker(ANALYZE_WEIGHTS)
    assert t.feed('@progress {"stage": "preview", "frac": 0.0}') == pytest.approx(.80)

def test_never_decreases():
    t = Tracker(ANALYZE_WEIGHTS)
    t.feed('@progress {"stage": "transcribe", "frac": 0.5}')
    assert t.feed('@progress {"stage": "transcribe", "frac": 0.4}') == pytest.approx(.40)
```

- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 진행 파싱"`

### Task 6: 파이프라인 실행기

**Files:**
- Create: `app/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `paths.*`, `options.analysis_args/render_args`, `progress.Tracker`.
- `runner.pipeline_cmd() -> list[str]`: 패키징 상태면 `[sys.executable, "--pipeline"]`, 개발 중이면 `[sys.executable, "<repo>/scripts/pipeline.py"]`.
- `runner.analyze_command(job: Job, model: paths.ModelPaths, device: str) -> list[str]`:
  `pipeline_cmd() + [src, "--review", "--progress-json", "--out", job_dir/"out", "--work-root", job_dir/"work", "--preview", job_dir/"preview.mp4", "--model", model.whisper, "--ctc-model", model.ctc, "--device", device] + analysis_args(opts)` (+ 데이터 폴더에 `fillers.json` 있으면 `--fillers` 그것)
- `runner.render_command(job, model, device, save_dir: Path) -> list[str]`: `... ["--apply-cuts", job_dir/"out"/f"{stem}.cuts.json", "--media-out", save_dir, ...] + render_args(opts)` (`--review`·`--preview` 없음).
- `runner.run(cmd: list[str], weights: dict, on_progress: Callable[[float], None], env: dict | None = None, on_start: Callable[[Running], None] | None = None) -> RunResult`; `RunResult(ok: bool, error: str)`. 실패 시 `error` = stderr/stdout 의 마지막 비어 있지 않은 줄 중 `오류:` 로 시작하는 줄 우선, 없으면 마지막 줄, 크래시(음수/큰 종료 코드)면 `"처리 프로그램이 비정상 종료했습니다 (코드 N)"`.
- `runner.Running.cancel()`: `on_start` 로 받는 핸들, 프로세스 트리 종료 (윈도우 `taskkill /T /F`, 맥 `os.killpg`). 취소되면 `RunResult(False, "cancelled")`.
- `runner.read_summary(job: Job) -> dict`: `out/<stem>.cuts.json` 과 원본 길이로 `{"orig_sec","final_sec","cuts","removed_sec"}`.
- 하위 프로세스 env: `PATH` 앞에 `paths.ffmpeg_bin_dir()`, `HF_HUB_OFFLINE=1` (모델은 앱이 미리 받음), `PYTHONIOENCODING=utf-8`, 윈도우 CUDA 사용 시 `YTEDIT_CUDA_DIR`(Task 9).

- [ ] **Step 1: 테스트** — 가짜 파이프라인(`tests/fake_pipeline.py`: 인자로 받은 줄을 출력하고 지정 코드로 종료)으로:

```python
def test_runner_progress_and_ok(): ...      # @progress 줄 → on_progress 호출값이 증가, ok True
def test_runner_failure_reports_last_error():
    r = runner.run(fake("오류: 남길 구간이 없습니다 (전부 컷 대상).", code=1), ANALYZE_WEIGHTS, lambda f: None)
    assert not r.ok and r.error.startswith("오류: 남길 구간이 없습니다")
def test_runner_crash_code():  ...         # code=-11 또는 3221225477 → "비정상 종료" 문구
def test_runner_cancel(): ...              # 오래 sleep 하는 가짜 → cancel → error == "cancelled", 2초 안에 반환
def test_command_unicode_path(tmp_path):
    job = Job(... src=str(tmp_path/"바탕 화면"/"강의 03 (최종).mov") ...)
    cmd = runner.analyze_command(job, ModelPaths("/m/w", "/m/c"), "cpu")
    assert str(tmp_path/"바탕 화면"/"강의 03 (최종).mov") in cmd and "--review" in cmd
```

- [ ] **Step 2–4:** FAIL → 구현 (`subprocess.Popen`, `text=True, encoding="utf-8", errors="replace"`, stdout+stderr 합쳐 줄 단위 읽기; 맥은 `start_new_session=True`, 윈도우는 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`) → PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 파이프라인 실행기"`

### Task 7: 대기열

**Files:**
- Create: `app/jobqueue.py`
- Test: `tests/test_jobqueue.py`

**Interfaces:**
- Consumes: `Store`, `runner`.
- `jobqueue.Queue(store: Store, execute: Callable[[Job, str], RunResult], on_change: Callable[[Job], None])` — `execute(job, kind)` 의 `kind ∈ {"analyze","render"}` (실제로는 runner 를 감싼 함수 주입, 테스트에선 가짜).
- `start_all()`: `waiting` 인 영상을 목록 순서로 분석 대기열에 넣는다.
- `request_save(job_id)`: 렌더 작업을 **대기 중인 분석보다 앞**에 넣는다 (실행 중인 것은 끊지 않음). 상태 `saving`.
- `rejudge(job_id)`: `review`/`saved` 인 영상의 빠른 옵션이 바뀌었을 때 — 이전 cuts.json 을 `prev.cuts.json` 으로 복사(`job.prev_cuts`) 후 분석 명령을 **바로**(대기열 맨 앞) 실행. 전사가 캐시돼 있어 몇 초. 끝나면 `review`.
- `cancel(job_id)`: 대기 중이면 빼고, 실행 중이면 runner 취소 후 빼기.
- 작업 스레드 하나. 상태 전이마다 `store.save()` + `on_change(job)`.
- 분석 성공 → `review` + `summary`; 렌더 성공 → `saved`; 실패 → `failed` + `error`; 취소 → 목록에서 제거.

- [ ] **Step 1: 테스트** (가짜 execute 가 호출 순서를 기록)

```python
def test_runs_in_order_one_at_a_time(): ...
def test_save_jumps_ahead_of_waiting_analysis():
    # a 실행 중, b·c 대기 → request_save(x) → 순서 a, x(render), b, c
def test_queue_continues_after_failure(): ...  # a 실패 → a failed, b 는 실행됨
def test_rejudge_keeps_prev_cuts(): ...         # job.prev_cuts 가 이전 파일을 가리키고 내용이 이전 판정
def test_saved_then_option_change_goes_review(): ...
```

- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 작업 대기열 (저장 우선)"`

### Task 8: 컷 비교

**Files:**
- Create: `app/diff.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- `diff.compare(old: dict, new: dict) -> dict` (인자는 cuts.json 내용). 반환:
  `{"rows": [{"word": str, "kind": "added"|"restored", "start": float, "end": float}], "silence": {"added": int, "restored": int}}`, `rows` 는 시간순.
- 규칙 (스펙 3.3): 단어 단위 비교 키는 `words_idx` 의 각 `(seg, wi)`. 새 판정에만 있으면 `added`(추가 잘림), 이전에만 있으면 `restored`(다시 살림). `reason == "silence"` 컷은 rows 에 넣지 않고 개수만: 겹치는 구간이 없는 무음 컷 수. `(전사 누락)` 컷(`words_idx` 빈 비무음)은 구간 겹침으로 비교. 경계만 0.1초 미만 움직인 것은 변화 아님.
- `diff.cut_ranges(cuts_json: dict) -> list[[float, float]]` (미리보기용 잘린 구간).

- [ ] **Step 1: 테스트**

```python
def cut(s, e, word, reason="habit", idx=None): return {"start": s, "end": e, "word": word, "reason": reason, "words_idx": idx or []}

def test_added_word():
    old = {"cuts": [cut(1, 1.2, "어", "always", [[0, 1]])]}
    new = {"cuts": [cut(1, 1.2, "어", "always", [[0, 1]]), cut(3.1, 3.3, "그", "habit", [[0, 5]])]}
    assert diff.compare(old, new)["rows"] == [{"word": "그", "kind": "added", "start": 3.1, "end": 3.3}]

def test_restored_word(): ...           # 반대 방향 → kind "restored"
def test_merged_cut_counts_each_word(): # 한 컷에 words_idx 두 개 → 새로 생기면 rows 두 줄
def test_silence_summary_only():
    old = {"cuts": [cut(5, 6, "(무음)", "silence")]}
    new = {"cuts": []}
    r = diff.compare(old, new); assert r["rows"] == [] and r["silence"] == {"added": 0, "restored": 1}
def test_boundary_shift_ignored(): ...  # 같은 words_idx, start 0.05 이동 → 변화 없음
```

- [ ] **Step 2–4:** FAIL → 구현 → PASS. (실제 cuts.json 의 `reason` 값은 `pipeline.detect_fillers` 를 보고 맞춘다.)
- [ ] **Step 5: 커밋** `git commit -m "app: 컷 비교"`

### Task 9: 모델 관리와 윈도우 GPU

**Files:**
- Create: `app/models.py`, `app/manifest.json`
- Modify: `scripts/pipeline.py` (`load_whisper`: 환경변수 `YTEDIT_CUDA_DIR` 가 있으면 윈도우에서 `os.add_dll_directory`)
- Test: `tests/test_models.py`

**Interfaces:**
- `manifest.json`:
  ```json
  {"models": [
    {"key": "whisper", "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "revision": "<커밋 SHA>", "bytes": 0},
    {"key": "ctc", "repo": "kresnik/wav2vec2-large-xlsr-korean", "revision": "<커밋 SHA>", "bytes": 0}],
   "cuda_win": [{"name": "nvidia-cublas-cu12", "url": "<PyPI 휠 URL>", "sha256": "<...>"},
                {"name": "nvidia-cudnn-cu12", "url": "<PyPI 휠 URL>", "sha256": "<...>"}]}
  ```
  리비전·크기·휠은 구현 시 현재 값으로 채운다 (faster-whisper 가 `large-v3-turbo` 에 쓰는 저장소를 `faster_whisper.utils._MODELS` 에서 확인; cuBLAS/cuDNN 버전은 번들할 ctranslate2 버전 요구에 맞춤).
- `models.status(manifest: dict, root: Path) -> list[dict]`: 받아야 할 항목 (`root/"models"/<key>/<revision>/.complete` 없음).
- `models.download(manifest, root, on_progress: Callable[[int, int], None]) -> ModelPaths`: `huggingface_hub.snapshot_download(repo, revision=, local_dir=root/"models"/key/revision)` 로 받고(이어받기 지원) 바이트 진행을 콜백. 끝나면 `.complete` 생성. 실패 시 `DownloadError` (재시도 가능).
- `models.cleanup(manifest, root)`: manifest 에 없는 `models/<key>/<revision>` 폴더 삭제.
- `models.paths(manifest, root) -> paths.ModelPaths`.
- 윈도우 GPU (스펙 4장): `models.has_nvidia() -> bool` (`nvidia-smi` 실행 성공), `models.download_cuda(manifest, root, on_progress)` → 휠(zip)에서 `*.dll` 만 `root/"cuda"/<manifest 해시>/` 로 풀기, `models.probe_gpu(root) -> bool` → `[sys.executable, "--probe-gpu"]` 하위 프로세스(Task 12 의 `__main__`) 가 `device="cuda"` 로 로드+1초 무음 전사, 종료 코드 0 이면 True. 결과는 `store.settings["gpu"] = {"key": <manifest 해시>, "ok": bool}`; 키가 같으면 다시 시험하지 않음.
- `models.device(settings) -> str`: 맥 `"auto"`, 윈도우 `"cuda"` if gpu ok else `"cpu"`.

- [ ] **Step 1: 테스트** (`snapshot_download` 를 monkeypatch)

```python
def test_status_lists_missing(tmp_path): ...
def test_download_marks_complete_and_reports_progress(tmp_path, monkeypatch): ...
def test_download_failure_raises_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "snapshot_download", raise_(OSError("network")))
    with pytest.raises(models.DownloadError): models.download(MAN, tmp_path, lambda d, t: None)
    assert models.status(MAN, tmp_path)  # 여전히 받아야 함
def test_cleanup_removes_old_revision(tmp_path): ...
def test_device_choice(monkeypatch): ...  # win + ok → cuda, win + fail → cpu, mac → auto
```

- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5: 실제 받기 확인 (맥):** `YTEDIT_DATA_DIR=<scratch>/d .venv/bin/python -c "import json,app.models as m; ..."` 로 두 모델 받기 → `.complete` 두 개, 그 경로로 `pipeline.py input/test.mp4 --review --model <whisper 경로> --ctc-model <ctc 경로> --out <scratch>/m` 성공.
- [ ] **Step 6: 커밋** `git commit -m "app: 모델 manifest, 내려받기, 윈도우 GPU"`

### Task 10: 업데이트 확인

**Files:**
- Create: `app/updates.py`
- Test: `tests/test_updates.py`

**Interfaces:**
- `updates.check(current: str, fetch=_fetch_json) -> dict | None`: `https://api.github.com/repos/withbbang/video-edit/releases/latest` 를 받아 `tag_name`(`v1.2.0`) 이 `current` 보다 크면 `{"version": "1.2.0", "notes": body, "url": asset_url}`; 같거나 작거나 네트워크 오류·형식 오류면 None. 타임아웃 10초.
- 자산 고르기: 맥 `youtube-edit-<ver>-mac-arm64.dmg`, 윈도우 `youtube-edit-<ver>-win-x64-setup.exe` (Task 14 의 이름과 같아야 함).
- `updates.download(url, dest_dir: Path) -> Path` 후 `updates.open_file(path)` (맥 `open`, 윈도우 `os.startfile`).
- 버전 비교는 숫자 튜플 (`packaging` 의존성 없이).

- [ ] **Step 1: 테스트**

```python
def test_newer(): assert updates.check("0.1.0", fetch=lambda u: REL("v0.2.0"))["version"] == "0.2.0"
def test_same_or_older_none(): ...
def test_check_offline_returns_none(): assert updates.check("0.1.0", fetch=raise_(OSError())) is None
def test_picks_platform_asset(monkeypatch): ...  # sys.platform darwin / win32
def test_version_compare(): assert updates.newer("0.10.0", "0.9.9")
```

- [ ] **Step 2–4:** FAIL → 구현 (`urllib.request`) → PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 업데이트 확인"`

### Task 11: 화면 ↔ 파이썬 다리 (js_api)

**Files:**
- Create: `app/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `Store`, `Queue`, `diff`, `models`, `updates`, `paths`, `runner.read_summary`.
- `api.Api(store, queue, push: Callable[[str, dict], None])` — `push(event, data)` 는 창에 `window.onAppEvent(event, data)` 를 부른다 (Task 12).
- 화면이 부르는 메서드 (모두 JSON 가능한 값 반환):
  - `list_jobs() -> list[dict]` (`id, name, status, progress, error, summary`)
  - `add_files(paths: list[str]) -> list[dict]` — 영상·오디오 확장자만 (`.mp4 .mov .m4v .mkv .webm .avi .mts .m4a .mp3 .wav`), 중복 무시.
  - `pick_files() -> list[dict]` — 파일 대화상자(여러 개) 후 `add_files`.
  - `remove_job(id)`, `start()`, `save(id)`
  - `get_options(id) -> dict`, `set_option(id, key, value) -> dict` — `FAST_KEYS` 이고 상태가 `review`/`saved` 면 `queue.rejudge`; 그 밖의 옵션이고 상태가 `saved` 면 `review` 로만 바꾼다 (스펙 3.3).
  - `get_result(id) -> dict`: `{"summary", "cuts": diff.cut_ranges(...), "changes": diff.compare(prev, cur) 또는 None, "preview_url": str, "duration": float}`.
  - `get_settings() / set_save_dir() -> str` (폴더 대화상자) / `get_version() -> str`
  - `model_status() -> dict`, `download_models()` (스레드에서, 진행 `push("models", {"done","total"})`, 실패 `push("models", {"error": True})`)
  - `check_update() -> dict | None` (닫은 버전이면 None), `dismiss_update(version)`, `install_update()`
- 창으로 보내는 이벤트: `job` (Job dict), `models`, `update`.

- [ ] **Step 1: 테스트** (가짜 queue·push)

```python
def test_add_files_filters_and_dedupes(tmp_path): ...   # .txt 무시, 같은 파일 두 번 → 하나
def test_set_fast_option_triggers_rejudge(): ...
def test_set_render_option_does_not_rejudge(): ...
def test_dismissed_update_hidden(): ...
```

- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5: 커밋** `git commit -m "app: 화면 다리 api"`

### Task 12: 창, 화면, 진입점

**Files:**
- Create: `app/window.py`, `app/__main__.py`, `app/ui/index.html`, `app/ui/app.js`, `app/ui/style.css`, `requirements-app.txt` (`pywebview>=5.3,<6`, `pyinstaller>=6.10`)
- Verify: 실제 앱 실행 (pytest 없음 — 화면은 손으로 확인)

**Interfaces:**
- Consumes: `Api` 전체.
- `__main__.main(argv)`: `--pipeline ...` → `sys.argv` 바꿔 `pipeline.main()`; `--probe-gpu` → Task 9 시험; 그 외 → `window.run()`.
- `window.run()`: `Store.load(data_dir()/"state.json")`, `Queue`, `Api` 생성, `webview.create_window("youtube-edit", url=ui/index.html, js_api=api, width=1100, height=720, min_size=(900, 600))`. 드롭은 `window.dom.document.events.drop` 에서 `e["dataTransfer"]["files"][i]["pywebviewFullPath"]` 로 실제 경로를 받아 `api.add_files` (dragover 는 JS 에서 `preventDefault`). 미리보기 사본은 `window.serve_previews(data_dir) -> str` (기본 URL) 가 띄우는 작은 정적 서버(`127.0.0.1`, 임의 포트, `jobs/<id>/preview.mp4` 만 허용, Range 요청 지원 — `<video>` 탐색에 필요)로 제공하고 `preview_url = f"{base}/{id}/preview.mp4"`.
- 화면 (시안 `layout-v5.html` / `queue-v2.html` / `first-run-v3.html` 그대로, Global Constraints 의 문구·색):
  - 왼쪽: 옵션 4개 세그먼트 버튼 + `▸ 고급`(접힘: 음량 맞추기, 영상 안 자막, 화질, 저장 위치 `설정`) + 오른쪽 정렬 버튼(`시작`/`저장`). 버튼 규칙: 선택한 영상이 `review`/`saved` 면 `저장`, 그 외 `시작`(대기 영상 없으면 비활성).
  - 오른쪽 위: 영상 박스 목록 (이름 · 상태 · 작은 진행 막대 · ✕) + 점선 박스 `＋ 끌어다 놓거나 파일 선택`. 목록이 비면 오른쪽 전체가 큰 드롭 영역 + `파일 선택` 버튼. 처리·저장 중 ✕ 는 `confirm()` 뒤 취소.
  - 오른쪽 아래(`review`/`saved`): 요약 `12:41 → 10:58 · 214곳 잘림 · −103초`, `<video>` 미리보기 — `timeupdate` 마다 현재 시각이 잘린 구간 안이면 그 끝으로 `currentTime` 이동, 타임라인 막대(잘린 곳 빨강, 달라진 곳 주황; 누르면 이동), 변화 목록 (처음 5줄 + `+ 더 보기`, `03:12 “그” [추가 잘림] ▶ 원본 · ▶ 수정본`; 무음은 요약 한 줄 `말 없는 구간 N곳 다시 살림`/`추가 잘림`). `▶ 원본` = 컷 앞 2초~뒤 2초 그대로, `▶ 수정본` = 같은 구간 건너뛰기 재생.
  - 모델 덮개: 실행 시 `model_status()` 가 받을 게 있으면 창 전체 덮개 `모델 내려받는 중` + 막대 + `1.1GB / 2.8GB · 약 4분 남음`, 실패 시 `모델 내려받기 실패` / `인터넷 연결을 확인해 주시기 바랍니다` / `다시 시도`.
  - 업데이트 띠: `새 버전 1.2.0 · 바뀐 점 · 받기` + ✕. 켤 때와 24시간마다 `check_update()`.
- [ ] **Step 1: 구현**
- [ ] **Step 2: 손 확인 (맥, 개발 실행 `.venv/bin/python -m app`)** — 다음이 모두 되면 통과:
  1. `input/test.mp4` 를 창에 드롭 → 목록에 추가, 같은 파일 다시 드롭 → 그대로 하나.
  2. `파일 선택` 으로 두 개 고르기 → 둘 다 추가.
  3. `시작` → `처리 중 N%` 가 오르고 → `확인 대기`, 요약 숫자가 `output` 의 report 와 같은 방식으로 계산됨.
  4. 말버릇 `일부 → 모두` → 몇 초 안에 주황 표시와 `추가 잘림` 줄, `▶ 원본`/`▶ 수정본` 소리가 다름.
  5. `저장` → `저장 중 N%` → `✓ 저장됨`, 저장 폴더에 `test_clean.mp4`, `test_clean.srt` 만 생김; CLAUDE.md 렌더 검증 명령 통과.
  6. 처리 중 앱 종료 → 다시 켬 → 그 영상 `대기`, 끝난 영상은 결과 그대로.
  7. 데이터 폴더의 `models` 를 다른 이름으로 바꾸고 켬 → 모델 덮개; 와이파이 끄고 → 실패 화면, 켜고 `다시 시도` → 이어 받음.
  8. 한글·공백 경로 영상(`~/Desktop/강의 03 (최종).mov` 로 복사)도 1–5 통과.
- [ ] **Step 3: 커밋** `git commit -m "app: 창과 화면"`

### Task 13: 패키징 (로컬 맥 빌드)

**Files:**
- Create: `packaging/youtube-edit.spec`, `packaging/fetch_ffmpeg.py`, `packaging/macos/make_dmg.sh`, `packaging/windows/installer.iss`, `LICENSE` (GPL-3.0 전문), `packaging/THIRD_PARTY.txt` (ffmpeg 라이선스·소스 URL)

**Interfaces:**
- `fetch_ffmpeg.py --platform mac|win --dest packaging/bin`: 맥은 arnndn·VideoToolbox·libx264·libx265 포함 arm64 정적 빌드, 윈도우는 BtbN/gyan GPL 정적 빌드(arnndn 포함). URL 과 sha256 은 파일 안 상수로 고정. 받은 뒤 `ffmpeg -filters` 에 `arnndn` 이 있는지 확인, 없으면 실패.
- PyInstaller spec: 진입 `app/__main__.py`, onedir, `datas`: `app/ui`, `app/manifest.json`, `fillers.json`, `models/sh.rnnn`, `scripts/pipeline.py`, `LICENSE`, `packaging/THIRD_PARTY.txt`; `binaries`: `packaging/bin/*` → `bin/`. 맥 `BUNDLE(name="youtube-edit.app", bundle_identifier="com.withbbang.youtube-edit")`, 윈도우 `console=False`. torch 는 CPU 판(윈도우 CI 에서 `--index-url https://download.pytorch.org/whl/cpu`).
- 파이프라인 `ROOT` 는 번들 안(읽기 전용)이 되므로 앱은 항상 `--work-root`, `--out`, `--media-out` 을 넘긴다 (Task 6). `models/sh.rnnn` 상대 경로용 `cwd=ROOT` 는 번들 폴더라 그대로 동작.
- `make_dmg.sh <app> <out.dmg>`: `hdiutil create` + 응용 프로그램 폴더 바로가기.
- `installer.iss`: `%LOCALAPPDATA%\Programs\youtube-edit` (관리자 권한 불필요), 시작 메뉴·바탕화면 아이콘, 같은 AppId 로 덮어쓰기 설치, WebView2 없으면 Evergreen 부트스트래퍼 실행.

- [ ] **Step 1:** 구현
- [ ] **Step 2: 맥 로컬 빌드 확인:** `python packaging/fetch_ffmpeg.py --platform mac --dest packaging/bin && pyinstaller packaging/youtube-edit.spec && bash packaging/macos/make_dmg.sh dist/youtube-edit.app dist/test.dmg` → dmg 를 열어 앱을 `/Applications` 로 복사, 실행해 Task 12 Step 2 의 1·3·5 통과. `which ffmpeg` 가 없는 셸 환경(`env -i`)에서도 동작 (번들 ffmpeg 사용 확인: 처리 로그에 번들 경로).
- [ ] **Step 3: 커밋** `git commit -m "패키징: PyInstaller, dmg, Inno Setup, ffmpeg, GPL"`

### Task 14: GitHub Actions 릴리스

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- 트리거: `push: tags: ["v*"]`. 잡 `mac` (`macos-14`, arm64), `win` (`windows-latest`).
- 각 잡: Python 3.11 설치 → 의존성(`requirements.txt`, `requirements-app.txt`, 윈도우 torch CPU 인덱스) → `pytest` → `fetch_ffmpeg.py` → 태그에서 버전을 `app/__init__.py` 의 `__version__` 과 비교(다르면 실패) → PyInstaller → 맥 `make_dmg.sh` / 윈도우 `iscc packaging/windows/installer.iss`.
- 자산 이름 (Task 10 과 같아야 함): `youtube-edit-<ver>-mac-arm64.dmg`, `youtube-edit-<ver>-win-x64-setup.exe`.
- 마지막 잡이 `softprops/action-gh-release` 로 두 파일을 릴리스에 올린다. 릴리스 본문에 설치 안내(스펙 6장) 포함.

- [ ] **Step 1:** 구현
- [ ] **Step 2: 확인:** 사용자 승인을 받은 뒤 `v0.1.0-rc1` 같은 시험 태그를 push → 두 잡 성공, 릴리스에 두 파일. 윈도우 PC(또는 VM)에서 설치 → SmartScreen 넘기기 → Task 12 Step 2 의 1·3·5 통과. (태그 push 와 릴리스 생성은 외부에 공개되는 작업이므로 반드시 사용자에게 먼저 묻는다.)
- [ ] **Step 3: 커밋** `git commit -m "CI: 태그로 맥·윈도우 설치 파일 릴리스"`

### Task 15: 문서

**Files:**
- Modify: `README.md` (앱 설치·첫 실행·경고 넘기기 — 스펙 6장: 맥 시스템 설정 › 개인정보 보호 및 보안 › "그래도 열기", 윈도우 "추가 정보" → "실행", 업데이트 때마다 다시 넘김, Apple Silicon 전용), `CLAUDE.md` (구조에 `app/`, `packaging/`, `tests/` 추가; 앱 개발 실행 `.venv/bin/python -m app`; 테스트 `.venv/bin/python -m pytest`; 새 파이프라인 옵션 한 줄씩)
- [ ] **Step 1:** 작성 → **Step 2:** 커밋 `git commit -m "문서: 데스크톱 앱 설치·개발 안내"`
