# 영상 끼워 넣기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 본 영상 A 의 원하는 자리에 다른 영상 B 를 끼워 넣어, 이어진 한 영상으로 필러·무음 컷·자막·편집·렌더를 하고, 자주 쓰는 짧은 영상은 편집째 보관함에 저장해 다시 넣는다.

**Architecture:** 파이프라인에 숨은 옵션 `--sequence <json>`(조각 목록)을 더한다. 파일마다 오디오 정리·전사·필러 시간 보정을 따로 캐시하고(`<work>/<이름>/files/<fkey>/`), 조각 in/out 으로 잘라 이어 붙인 오디오·전사를 지금의 자리(`<work>/<이름>/clean.wav`·`asr.wav`, `out/<이름>.transcript.json`)에 써서 나머지 흐름(미리보기·파형·자막·렌더)은 그대로 쓴다. 컷 판정은 조각마다 따로 한 뒤 시간을 밀어 합치고, 렌더·미리보기 영상은 조각별 trim→A 에 맞추기→concat 을 필터 앞에 붙여 한 번만 인코딩한다. 앱은 `Job.pieces` 를 들고, 넣기·빼기 때 편집 시간을 순수 함수로 밀고 자막 수정 키는 "파일 기준 키"로 바꿔 두었다가 다시 판정 뒤 새 전사 키로 되돌린다.

**Tech Stack:** Python 3 (numpy, wave), ffmpeg 9 / ffprobe, faster-whisper(기존), pywebview + 바닐라 JS(app/ui), pytest, node(timeline.js 테스트).

**Spec:** `docs/superpowers/specs/2026-10-30-video-insert-design.md`

## Global Constraints

- `pipeline.py` CLI 기본 동작은 그대로. `--sequence` 가 없으면 지금과 바이트 단위로 같은 결과·캐시 경로.
- 앱은 `Job.pieces` 가 비어 있지 않으면 분석·저장·미리보기 소리 명령 모두에 `--sequence` 를 넘긴다 (끼운 영상을 다 빼서 A 하나만 남아도 계속 넘김 — 캐시 경로가 오락가락하지 않게).
- 결과 해상도·fps·회전·색(8비트 H.264 / 10비트 HEVC)·오디오 형식은 A(맨 처음 조각의 파일)를 따른다. 비율이 다르면 비율 지켜 줄이고 검은 여백. 소리 없는 B 는 무음.
- 조각 경계는 A 의 프레임 격자에 맞춘다. 조각 길이 = 정수 프레임.
- 노이즈 제거·음량 정규화는 파일마다 같은 설정으로 따로 (prepare_audio 를 파일마다). 자동 무음 기준(`auto_silence_stats`)도 파일마다.
- 조각 경계는 영상 맨 앞/뒤처럼: 조각마다 따로 판정하므로 무음 잇기·자막 줄·필러 경계 넓히기·CTC 가 경계를 넘지 않는다.
- ffmpeg 9: `-/filter_complex <파일>`, 긴 식은 `sum_tree`.
- 끼운 영상은 작업 폴더 `clips/<sha16><확장자>` 로 복사. 보관함은 `<데이터>/clip_presets/` (presets.json + 영상 + 분석 캐시 + 편집).
- 화면 문구(스펙 3.1): 탭 `영상 N`, `사용 중 N`, `보관함 N`, `불러오기`, `이름 변경`, `보관`, `빼기`, `삽입`. 소리만 있는 A 면 영상 탭 숨김.
- 넣기·빼기는 `실행 취소`/`실행 재개` 대상. 넣은 영상 전사·분석 중엔 편집을 막는다 (지금의 다시 판정처럼 상태 PROCESSING).
- 테스트: `.venv/bin/python -m pytest` 전체가 통과해야 각 작업이 끝난다. 합성 신호는 `--denoise none`.

## Review Focus

1. **A 의 자동 컷이 B 를 넣어도 바뀌지 않아야 한다** — 넣기 전 A 단독 컷과, 넣은 뒤 A 조각 컷(시간 되돌린 것)이 같다. → Task 4 `test_inserting_b_keeps_a_cuts`.
2. **B 가 해상도·fps·소리 유무가 다를 때** 결과 영상·오디오 길이 차가 1프레임 이내 → Task 5 `test_render_mixed_sources`.
3. **재생 위치가 잘린 구간 안·영상 맨 끝·맨 앞(0)일 때 넣기** — 각각 거절 없이 들어가고 편집이 맞게 밀린다 → Task 6 `test_insert_at_edges`, Task 8 `test_add_clip_inside_cut_snaps_to_cut_start`.
4. **B 안에 C 를 넣은 뒤 B 빼기** — C 와 C 위 편집은 남고 B1·B2 위 편집만 사라진다 → Task 6 `test_remove_outer_keeps_inner`.
5. **넣은 뒤 다시 판정이 끝나면 A 의 자막 수정·꾸밈이 새 전사 키로 살아 있다** (지금 코드는 지문이 바뀌면 지운다) → Task 8 `test_insert_keeps_word_edits`.

---

### Task 1: 조각 목록과 이어 붙인 전사 (순수 함수)

**Files:**
- Create: `scripts/sequence.py`
- Test: `tests/test_sequence.py`

**Interfaces:**
- Produces:
  - `Piece = dataclass(id: str, clip: str, src: Path, fkey: str, t_in: float, t_out: float)`; `load_sequence(path: Path) -> list[Piece]` (json `[{id, clip, src, in, out}]`, `fkey = sha1(source_sig(src))[:16]` — `source_sig` 는 pipeline 의 것을 인자로 받지 말고 같은 로직(경로·크기·mtime)을 여기서 씀).
  - `Span = dataclass(piece: Piece, f_in: int, f_out: int, start_f: int)` — 파일 안 프레임 [f_in, f_out), 이어 붙인 시간의 시작 프레임. `spans(pieces, fps: Fraction) -> list[Span]` (f_in = round(t_in·fps), f_out = round(t_out·fps), 길이 0 조각은 뺌).
  - `combine_transcripts(spans, file_trs: dict[str, dict], fps) -> dict`: 각 조각에서 단어 중점이 [in,out) 인 단어만, 시간 = t − in + start(초). 세그먼트는 조각 경계에서 갈리고 id 는 0부터 다시. 세그먼트마다 출처 `{"src": {"fkey", "sid": 원래 seg id, "w0": 원래 첫 단어 번호}}`. 단어의 `whisper` 키 시간도 같이 민다. 최상위 `duration`(초), `pieces: [{id, clip, start, end, seg_from, seg_to}]`, `language` 는 첫 조각 것.
  - `slice_piece(tr: dict, p: dict) -> dict`: `tr["pieces"]` 의 한 항목 범위의 세그먼트만 남기고 시간을 조각 기준(0부터)으로 당긴 사본. 세그먼트 id 는 그대로 둔다 (컷의 `words_idx` 가 이어 붙인 전사를 그대로 가리키게).

- [ ] **Step 1: 실패하는 테스트**

```python
def test_spans_frame_grid():
    ps = [P("a", "main", 0.0, 2.0), P("b", "c1", 0.0, 1.01), P("a2", "main", 2.0, 5.0)]
    sp = seq.spans(ps, Fraction(30))
    assert [(s.f_in, s.f_out, s.start_f) for s in sp] == [(0, 60, 0), (0, 30, 60), (60, 150, 90)]

def test_combine_splits_segment_at_boundary_and_keeps_provenance():
    # A 세그먼트 0 이 1.0초 단어와 3.0초 단어를 가짐, 2.0초에 B(1초) 끼움
    tr = seq.combine_transcripts(spans, {"A": trA, "B": trB}, Fraction(30))
    assert [s["id"] for s in tr["segments"]] == [0, 1, 2]
    assert tr["segments"][2]["words"][0]["start"] == pytest.approx(4.0)   # 3.0 + B 1초
    assert tr["segments"][2]["src"] == {"fkey": "A", "sid": 0, "w0": 1}
    assert tr["pieces"][1] == {"id": "b", "clip": "c1", "start": 2.0, "end": 3.0, "seg_from": 1, "seg_to": 2}
    assert tr["duration"] == pytest.approx(6.0)

def test_slice_piece_local_times_keep_ids():
    sub = seq.slice_piece(tr, tr["pieces"][2])
    assert [s["id"] for s in sub["segments"]] == [2]
    assert sub["segments"][0]["words"][0]["start"] == pytest.approx(1.0) and sub["duration"] == pytest.approx(3.0)
```

- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_sequence.py -v` → FAIL (모듈 없음)
- [ ] **Step 3:** `scripts/sequence.py` 구현 (위 시그니처). pipeline 을 import 하지 않는다 (pipeline 이 이걸 import).
- [ ] **Step 4:** 같은 명령 → PASS
- [ ] **Step 5:** `packaging/` 의 PyInstaller spec 이 `scripts/pipeline.py` 를 어떻게 묶는지 보고 `sequence` 가 함께 들어가게 고친다 (`tests/test_packaging.py` 통과 확인). 커밋 `영상 끼워 넣기: 조각 목록과 이어 붙인 전사`

### Task 2: 파일별 분석 묶기 (동작 변화 없음)

**Files:**
- Modify: `scripts/pipeline.py` (`process` 의 "1) 오디오"~"필러 시간 보정" 부분, 2151~2185 줄 근처)
- Test: `tests/test_sequence_pipeline.py`

**Interfaces:**
- Produces: `analyze_file(src: Path, work: Path, tr_path: Path, args, cfg: dict, cut_only: bool) -> FileAnalysis` — `FileAnalysis = dataclass(info, clean_wav, asr_wav, audio_key, tr, rms, hop, cfg, stats)`. `cfg` 는 auto_silence 를 적용한 그 파일의 설정, `stats` 는 `auto_silence_stats` 결과(또는 None). 지금 `process` 가 하던 순서(prepare_audio → transcribe → load_rms → auto_silence → align_key 로 CTC) 그대로, `args.apply_cuts` 일 때 auto_silence·CTC 를 건너뛰는 것도 그대로.
- Produces: `file_work(work: Path, fkey: str) -> Path` = `work / "files" / fkey`. 파일 전사는 `file_work(...)/transcript.json`.
- Produces: `adopt_single_cache(work: Path, out_dir: Path, stem: str, fw: Path) -> None` — `fw` 가 비어 있고 `work/audio.meta.json` 이 단독 모드 캐시(키에 `"sequence"` 없음)면 `clean.wav`·`asr.wav`·`audio.meta.json`·`out/<stem>.transcript.json` 을 `fw` 로 **복사**한다 (처음 끼워 넣을 때 A 를 다시 전사하지 않게).

- [ ] **Step 1:** 테스트: `adopt_single_cache` 가 단독 캐시를 복사하고, 이미 `fw/audio.meta.json` 이 있으면 손대지 않으며, `audio.meta.json` 에 `"sequence"` 키가 있으면 복사하지 않는다.
- [ ] **Step 2:** FAIL 확인
- [ ] **Step 3:** `analyze_file`·`file_work`·`adopt_single_cache` 구현, `process` 단독 경로가 `analyze_file(src, work, out_dir/f"{stem}.transcript.json", ...)` 를 쓰게 바꾼다.
- [ ] **Step 4:** `.venv/bin/python -m pytest` 전체 → PASS (단독 동작이 그대로인지는 기존 테스트가 지킨다)
- [ ] **Step 5:** 커밋 `영상 끼워 넣기: 파일별 분석 묶기`

### Task 3: 이어 붙인 오디오

**Files:**
- Modify: `scripts/sequence.py`, `scripts/pipeline.py`
- Test: `tests/test_sequence.py`

**Interfaces:**
- Produces (sequence.py): `concat_wav(parts: list[tuple[Path | None, int, int]], out: Path, rate: int, channels: int) -> int` — (wav, 시작 샘플, 끝 샘플), wav 가 None 이면 그 길이만큼 0. 채널 수가 다르면 모노→복제 / 스테레오→평균으로 맞춘다. `.part` 에 쓰고 `os.replace`. 반환 = 전체 샘플 수.
- Produces (pipeline.py): `sequence_audio(spans, analyses: dict[str, FileAnalysis], work: Path, fps, which: str) -> tuple[Path, dict]` — which `"clean"`(48k)·`"asr"`(16k). 조각 샘플 = 프레임 경계 × rate / fps (반올림, 앞 조각 끝 = 다음 조각 시작이 되게 누적 프레임으로 계산). `work/clean.wav`·`asr.wav` 에 쓰고 `work/audio.meta.json` = `{"sequence": [(fkey, f_in, f_out)...], "files": {fkey: audio_key}}`. 키가 같으면 다시 만들지 않는다.
- 소리 없는 파일(`info["has_audio"]` 가 거짓이거나 prepare_audio 가 못 만듦)은 `FileAnalysis.clean_wav = None`, 전사는 빈 세그먼트. 빌더는 지금 `ffprobe` 가 소리 없는 영상을 어떻게 내는지 확인하고 그에 맞춘다.

- [ ] **Step 1:** 테스트 `test_concat_wav_lengths_and_silence`: 48k 사인 0.5초 + None 0.25초 + 사인 0.5초 → 샘플 수 = 60000, 가운데 12000 샘플이 0. `test_concat_wav_mono_into_stereo`.
- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5:** 커밋 `영상 끼워 넣기: 이어 붙인 오디오`

### Task 4: 조각별 컷 판정과 `--sequence` 분석

**Files:**
- Modify: `scripts/pipeline.py` (`process` 의 "3) 컷 리스트", `silence_cuts_for_level` 에 `stats` 인자, `build_parser`)
- Test: `tests/test_sequence_pipeline.py`

**Interfaces:**
- Consumes: Task 1 `spans/combine_transcripts/slice_piece`, Task 2 `analyze_file/file_work/adopt_single_cache`, Task 3 `sequence_audio`.
- Produces: `judge_cuts(tr, rms, hop, duration, cfg, sil_base, args, stats) -> tuple[list[Cut], list[dict], int]` — 지금 "3) 컷 리스트" 의 `refine_cuts` 앞까지(필러·의심 단어·전사 누락·무음) 를 그대로 옮긴 것. 반환 (raw_cuts, skipped, n_fillers). `silence_cuts_for_level(..., stats=None)`: 주면 안에서 재지 않는다.
- Produces: 숨은 옵션 `--sequence PATH` (help=argparse.SUPPRESS 또는 기존 숨은 옵션과 같은 방식). 주면 `input` 은 A 원본(이름·결과 자리 기준)이고 첫 조각의 파일이어야 한다 (아니면 오류로 멈춤).
- 흐름: 파일마다 `analyze_file(f, file_work(work,fkey), file_work(...)/"transcript.json", ...)` (A 는 먼저 `adopt_single_cache`) → `combine_transcripts` → `out/<stem>.transcript.json` 에 쓰기 (`cache_key` 대신 `"sequence_key"`) → `sequence_audio` 두 벌 → `info` = A 의 info 에 `duration` = 총 프레임/fps → 조각마다 `slice_piece` + rms 를 hop 단위로 잘라 `judge_cuts(..., cfg=그 파일 cfg, stats=그 파일 stats)` → 컷 시간 += 조각 시작 → 합쳐서 `refine_cuts`. cuts.json 에 `"pieces"` (tr["pieces"]) 추가. `--apply-cuts` 의 지문 검사는 이어 붙인 전사로 그대로.
- 렌더 오디오(`--denoise-level`·`--render-*`)도 파일마다 `render_audio_for` 후 `concat_wav` (Task 3) — `preview_audio_source`·렌더 경로가 sequence 일 때 이 함수를 부르게 한 곳(`sequence_render_audio(...) -> tuple[Path, dict]`)으로 모은다.

- [ ] **Step 1:** 테스트 (합성 A·B wav/mp4 를 ffmpeg 로 만들고 `--denoise none --review` 로 실제 실행, 전사는 `transcribe` 를 monkeypatch 해 미리 정한 단어로):
  - `test_inserting_b_keeps_a_cuts`: A 단독 cuts 와, A[0:2]+B+A[2:] cuts 에서 B 구간 컷을 빼고 2초 이후를 −B 길이 한 것이 같다 (프레임 단위).
  - `test_b_edges_silence_fully_cut`: B 앞뒤 0.5초 무음이 패딩 없이 전부 컷.
  - `test_sequence_does_not_retranscribe_a`: 단독 실행 뒤 `--sequence` 실행에서 transcribe 가 B 에만 불림.
  - `test_sequence_input_must_be_first_piece`: 오류 메시지로 멈춤.
- [ ] **Step 2–4:** FAIL → 구현 → PASS, 전체 테스트 PASS
- [ ] **Step 5:** 커밋 `영상 끼워 넣기: 조각별 컷 판정과 --sequence 분석`

### Task 5: 이어 붙인 렌더·미리보기 영상

**Files:**
- Modify: `scripts/pipeline.py` (`render_video`, `make_preview_video`, `make_preview`, `write_report`, `process` 렌더 부분)
- Test: `tests/test_sequence_render.py`

**Interfaces:**
- Produces: `sequence_video_filter(spans, infos: dict[str, dict], out_info: dict, first_input: int, W: int, H: int) -> tuple[str, list[str], int]` → (필터 조각 `...[seq]`, ffmpeg 입력 인자, 다음 입력 번호). 조각마다 `[i:v]setpts=PTS-STARTPTS,fps=A,trim=start_frame=f_in:end_frame=f_out,setpts=PTS-STARTPTS,scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1,format=<A pix_fmt>[pk]` → `concat=n=N:v=1:a=0[seq]`. 같은 파일은 입력 하나를 `split` 해서 쓴다. 색: A 와 B 의 bt2020/HDR 여부가 다르면 `zscale`(있으면) 또는 `colorspace` 로 A 쪽으로 — 있는 필터를 `ffmpeg_has` 로 보고 고르고, 색이 조금 어긋나는 건 받아들인다 (스펙).
- `render_video(..., seq: tuple[str, list[str]] | None = None)`: 주면 `base` 의 `[0:v]setpts…,fps=…` 대신 `[seq]` 에서 `select` 를 시작하고 입력 번호(소리·자막·이미지)를 그만큼 민다. `make_preview_video(..., seq=None)` 도 같은 조각 + 지금의 960 스케일. 미리보기 캐시 키에 sequence 키를 넣는다.
- `write_report`: sequence 면 "조각" 표 (이름, 원래 길이, 이어 붙인 시작) 한 줄씩.

- [ ] **Step 1:** 테스트 (실제 ffmpeg): `test_render_mixed_sources` — A 320x240 30fps 소리 있음 4초, B 160x160 25fps 소리 없음 1초를 2초에 끼움, 컷 없음(`--silence off --filler off --habit off`), `--hw off` → 결과 길이 5.0초 ±1프레임, 영상·오디오 길이 차 1프레임 이내, 크기 320x240, 2.5초 프레임의 좌우 여백이 검정. `test_preview_video_sequence_length`.
- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5:** 커밋 `영상 끼워 넣기: 이어 붙인 렌더와 미리보기`

### Task 6: 편집 시간 밀기·빼기·자막 키 (앱 순수 함수)

**Files:**
- Create: `app/sequence.py`
- Test: `tests/test_app_sequence.py`

**Interfaces:**
- Produces: `Edits = dict` with keys `restored, range_ops, word_edits, line_times, word_styles, overlays` (Job 필드 그대로의 사본).
  - `split_piece(pieces: list[dict], at: float, new: dict) -> list[dict]` — 이어 붙인 시간 `at` 이 걸친 조각을 둘로 가르고(새 id, 같은 clip) 그 사이에 `new` 를 넣는다. at 이 조각 경계면 가르지 않는다. 조각 `{id, clip, src, name, in, out}`.
  - `shift_for_insert(e: Edits, at: float, d: float) -> Edits` — at 이후 시간 +d. at 을 가로지르는 `range_ops`·`restored` 는 [s,at],[at+d,e+d] 로 나눔, `overlays` 는 end 만 +d, `line_times` 값도 민다.
  - `shift_for_remove(e: Edits, spans: list[tuple[float, float]]) -> Edits` — 빼는 구간들(B1·B2) 안에 완전히 든 것은 지우고, 걸친 것은 잘라내고, 뒤는 당긴다. 이미지는 B 안에만 있으면 지우고 걸치면 줄인다.
  - `to_stable(keys: dict, tr: dict) -> dict` / `from_stable(keys: dict, tr: dict) -> dict` — `"seg:i"` ↔ `"fkey|sid|w0+i"`. 새 전사에 없는 키는 버린다. `word_edits`·`line_times`·`word_styles` 에 쓴다. (`line_times` 키는 줄 첫 어절 키라 같은 변환.)
  - `extract_clip(e: Edits, stable: dict, clip_spans: list[tuple[float, float, float]]) -> dict` — 보관용: (이어 붙인 시작, 끝, 파일 안 in) 마다 그 안의 편집을 **파일 안 초** 로. `place_clip(saved: dict, clip_spans) -> Edits` 는 거꾸로 (Task 10 이 씀).
- `clip_spans(pieces, clip) -> list[tuple[float, float, float]]`: 그 clip 조각들의 이어 붙인 (시작, 끝, 파일 in).

- [ ] **Step 1:** 테스트
  - `test_insert_shifts_and_splits`: range_op [1,5] cut, at=3, d=2 → [[1,3],[5,7]]; overlay [2,4] → [2,6]; line_times "3:0"=[4,5] → [6,7].
  - `test_insert_at_edges`: at=0 → 전부 +d; at=영상 끝 → 그대로.
  - `test_remove_outer_keeps_inner`: A[0:2] B1[2:3] C[3:4] B2[4:5] A[5:] 에서 B 빼기 → C 위 range_op 은 [2,3] 으로, B1 위 것은 사라짐, 이미지 [2.5,4.5] → [2,3].
  - `test_stable_roundtrip_across_split`: B 세그먼트가 C 때문에 둘로 갈린 새 전사에서 from_stable 이 두 번째 조각 어절을 맞는 키로.
  - `test_extract_place_roundtrip`.
- [ ] **Step 2–4:** FAIL → 구현 → PASS
- [ ] **Step 5:** 커밋 `영상 끼워 넣기: 편집 시간 밀기와 자막 키 옮기기`

### Task 7: Job·명령에 조각 목록

**Files:**
- Modify: `app/store.py` (`Job`), `app/runner.py`, `app/api.py` (`pipeline_command`, `save_*` 옆), `app/preview.py`(미리보기 소리 명령에 넘기는 곳)
- Test: `tests/test_runner.py`, `tests/test_store.py`

**Interfaces:**
- `Job.pieces: list = []` (`[{id, clip, src, name, in, out}]`, 비면 A 하나), `Job.stable_edits: dict | None = None` (넣기·빼기 직후 `to_stable` 결과 — 다시 판정이 끝나면 풀어 씀).
- `save_sequence(job, lock=None) -> Path | None` (api.py, `save_overlays` 모양): pieces 가 있으면 `out/<stem>.sequence.json` 에 `[{id, clip, src(절대 경로), in, out}]`, 없으면 지우고 None.
- `runner.analyze_command/render_command/preview_audio_command(..., sequence_path: Path | None = None)` → `["--sequence", str(p)]` 를 덧붙임.
- 테스트: 세 명령 모두 sequence 를 넘기는지, `Job.from_dict` 가 옛 json(필드 없음)을 읽는지.
- [ ] Steps: 테스트 → FAIL → 구현 → PASS → 커밋 `영상 끼워 넣기: 작업에 조각 목록`

### Task 8: 넣기·빼기 API

**Files:**
- Modify: `app/api.py` (이미지 겹쳐 넣기 부분 옆에 "영상 끼워 넣기" 묶음), `app/api.py` `_snap/_put`, `_drop_stale_edits`, `app/window.py` (`Dialogs.open_video`)
- Test: `tests/test_clips.py` (`tests/fake_pipeline.py` 로)

**Interfaces:**
- Consumes: Task 6, Task 7.
- `Dialogs.open_video() -> str | None` (mp4/mov/m4v/mkv/webm).
- `Api.add_clip(job_id, at: float) -> dict | None`, `Api.remove_clip(job_id, clip: str) -> dict | None`, `Api.rename_clip(job_id, clip, name) -> dict | None`, `Api.list_clips(job_id) -> list[dict]` (`[{clip, name, start, end, dur, thumb?}]` — clip 마다 한 줄, start = 첫 조각 시작). 반환은 `{"clips", "history"}`.
- 넣기: 파일을 `clips/<sha16><ext>` 로 복사 (sha1 은 스트리밍), ffprobe 로 길이·영상 있는지 확인(영상 스트림 없으면 None). `at` 을 결과의 `fps` 격자로 맞추고, 잘린 구간 안이면 그 잘린 구간 시작으로 당김. pieces 가 비었으면 먼저 `[{id, clip:"main", src: job.src, in:0, out: 길이}]`. 그다음 lock 안에서: before=_snap → `stable_edits = to_stable(...)` (지금 전사로) → `shift_for_insert` → `split_piece` → _record → `queue.rejudge(job_id)`.
- 빼기: clip "main" 은 거절. `shift_for_remove(clip_spans)` → 조각 제거 → 이웃한 같은 clip·이어진 in/out 조각은 하나로 합침 → rejudge.
- `_snap/_put` 에 `pieces`, `stable_edits` 포함. `_step`(실행 취소) 에서 pieces 가 바뀌었으면 rejudge.
- `_drop_stale_edits`: 지문이 다르고 `job.stable_edits` 가 있으면 버리지 말고 `from_stable` 로 새 키를 만들어 넣고 `edits_sha1` = 새 지문, `stable_edits = None`. 역사(`_history`)는 지우지 않는다.
- [ ] **Step 1:** 테스트: `test_insert_keeps_word_edits` (A 어절 고침 → 넣기 → 가짜 파이프라인이 이어 붙인 전사 → get_captions 에 고친 글자가 새 키로), `test_add_clip_inside_cut_snaps_to_cut_start`, `test_remove_main_refused`, `test_undo_insert_rejudges`, `test_edit_blocked_while_processing`.
- [ ] **Steps 2–4:** FAIL → 구현 → PASS. `tests/fake_pipeline.py` 가 `--sequence` 를 받아 이어 붙인 전사를 쓰도록 필요한 만큼 고친다.
- [ ] **Step 5:** 커밋 `영상 끼워 넣기: 넣기·빼기 API`

### Task 9: 영상 보관함

**Files:**
- Modify: `app/api.py`, `app/window.py` (미리보기 서버가 `clip_presets/` 썸네일을 낼 수 있게 — `overlay_presets` 처리 방식 참고)
- Test: `tests/test_clips.py`

**Interfaces:**
- `<data>/clip_presets/presets.json`: `[{id, name, file, dur, edits}]`, 파일 `<id>/<sha16><ext>`, 분석 캐시 `<id>/files/<fkey>/`(clean.wav·asr.wav·audio.meta.json·transcript.json 복사본), `edits` = Task 6 `extract_clip` 결과.
- `Api.list_clip_presets() -> list[dict]`, `save_clip_preset(job_id, clip, name) -> list | None` (같은 파일이 이미 보관함에 있으면 그 항목의 편집·이름을 바꿈), `rename_clip_preset(pid, name)` (같은 이름 거절, 40자), `delete_clip_preset(pid)`, `insert_clip_preset(job_id, pid, at) -> dict | None` (파일·분석 캐시를 작업 폴더로 복사한 뒤 Task 8 넣기와 같은 흐름 + `place_clip` 으로 편집 합침).
- 분석 캐시 키는 소스 서명(경로·크기·mtime)을 쓰므로, 복사본의 서명이 맞게 캐시를 옮길 때 키의 `src` 를 새 경로 서명으로 다시 쓰거나(빌더 판단) 복사할 때 mtime 을 보존한다. 테스트로 "보관함에서 넣으면 transcribe 가 안 불림" 을 확인.
- [ ] Steps: 테스트(`test_preset_insert_brings_edits`, `test_preset_insert_skips_transcribe`, `test_preset_rename_duplicate`) → FAIL → 구현 → PASS → 커밋 `영상 끼워 넣기: 영상 보관함`

### Task 10: 화면 — 영상 탭·타임라인 띠

**Files:**
- Modify: `app/ui/app.js` (이미지 탭 `renderTools` 근처 1058~1200 줄 모양 그대로 `영상` 탭), `app/ui/timeline.js`, `app/ui/style.css`, `app/ui/index.html` (필요하면)
- Test: `tests/test_timeline_js.py`, `tests/test_e2e_app.py`(있는 흐름에 한 경우)

**Interfaces:**
- `TL.clipBands(pieces: {clip, start, end}[], view) -> {clip, x0, x1}[]` (보기 범위 안 조각의 화면 x) — 순수 계산은 timeline.js 에 두고 node 테스트.
- 영상 탭: 오른쪽 목록 `잘린 곳`·`자막`·`이미지` 옆 `영상 N`. 사용 중/보관함, `불러오기`(Api.add_clip(지금 재생 위치)), 고른 줄 `이름 변경`·`보관`·`빼기`, 보관함 `삽입`·✕, 이름 두 번 누르기 = 이름 바꾸기 (CLAUDE.md 의 `h()`/dblclick 주의 그대로). 소리만 있는 A 면 탭 숨김. clip "main"(A) 은 목록에 안 보임.
- 타임라인: 확대 타임라인 화면 줄·막대에 끼운 구간 색 띠 (clip 마다 같은 색), 누르면 영상 탭에서 그 항목 고르기. 처리 중엔 지금 다시 판정 때와 같은 진행 표시.
- [ ] Steps: `TL.clipBands` 테스트 → FAIL → 구현 → PASS. 화면은 `run` 스킬로 앱을 띄워 짧은 합성 영상 두 개로 넣기·빼기·보관·삽입·실행 취소를 직접 확인하고 스크린숏. 커밋 `영상 끼워 넣기: 영상 탭과 타임라인 띠`

### Task 11: 문서와 전체 확인

**Files:** `CLAUDE.md` (구조 표·숨은 옵션 목록에 `--sequence`, 앱 화면 단락에 영상 탭·`clip_presets/`), 스펙 문서의 바뀐 판단이 있으면 반영.

- [ ] `.venv/bin/python -m pytest` 전체 PASS 출력 확인
- [ ] 실제 영상 두 개로(사용자 `input/` 은 건드리지 않고 복사본으로) 앱에서 넣기 → 저장 → `ffprobe -v error -show_entries stream=codec_type,duration:stream_tags=encoder,language -of csv <결과>` 로 길이 차 1프레임 이내·kor 자막 트랙 확인
- [ ] 커밋 `영상 끼워 넣기: 문서`
