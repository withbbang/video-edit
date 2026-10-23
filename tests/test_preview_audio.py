"""app/preview.py: 컷을 적용한 미리보기 소리를 백그라운드로 만들기 (CutAudio·cut_key)."""
import os
import threading
from pathlib import Path

from app import preview, runner
from app.options import JobOptions
from app.paths import ModelPaths
from app.runner import RunResult
from app.store import Job

WAIT = 5.0


def _make(out: Path, tick=[0]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"a")
    out.with_suffix(".index.json").write_text("{}")
    tick[0] += 1
    ns = tick[0] * 1_000_000_000
    for p in (out, out.with_suffix(".index.json")):
        os.utime(p, ns=(ns, ns))


class FakeRun:
    """cmd[-1] 이 out 경로. ok 면 파일을 만들고, gate 가 있으면 풀릴 때까지 기다린다."""

    def __init__(self, result=None, gate=None):
        self.calls = []
        self.result = result or RunResult(True)
        self.gate = gate
        self.started = threading.Event()

    def __call__(self, cmd):
        self.calls.append(cmd)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(WAIT)
        if self.result.ok:
            _make(Path(cmd[-1]))
        return self.result


class Ready(list):
    def __init__(self):
        super().__init__()
        self.event = threading.Event()

    def __call__(self, job_id):
        self.append(job_id)
        self.event.set()


def _wait_idle(ca):
    assert ca.wait_idle(WAIT)


def test_cut_key_changes_with_cuts_and_denoise():
    cuts = [{"start": 1.0, "end": 1.5, "word": "어"}, {"start": 3.0, "end": 3.25}]
    o = JobOptions()
    k = preview.cut_key(cuts, o)
    assert len(k) == 16 and all(c in "0123456789abcdef" for c in k)
    # 컷 시간만 본다 (단어·사유는 무관), 소수 6자리
    assert preview.cut_key([{"start": 1.0, "end": 1.5}, {"start": 3.0, "end": 3.25, "x": 1}], o) == k
    assert preview.cut_key([{"start": 1.0000001, "end": 1.5}, {"start": 3.0, "end": 3.25}], o) == k
    assert preview.cut_key([{"start": 1.0, "end": 1.6}, {"start": 3.0, "end": 3.25}], o) != k
    assert preview.cut_key(cuts, JobOptions(denoise=60)) != k
    assert preview.cut_key(cuts, JobOptions(loudnorm="keep")) != k
    assert preview.cut_key(cuts, JobOptions(habit="all", quality="high")) == k


def test_request_ready_when_files_exist(tmp_path):
    run, ready = FakeRun(), Ready()
    ca = preview.CutAudio(run, ready)
    out = tmp_path / "audio_cut.0123456789abcdef.m4a"
    _make(out)
    assert ca.request("j1", "0123456789abcdef", ["x", str(out)], out) == "ready"
    assert run.calls == [] and ready == []
    # index 가 없으면 아직 다 안 만들어진 것
    out.with_suffix(".index.json").unlink()
    assert ca.request("j1", "0123456789abcdef", ["x", str(out)], out) == "pending"
    _wait_idle(ca)
    ca.shutdown()


def test_request_runs_once_and_notifies(tmp_path):
    run, ready = FakeRun(), Ready()
    ca = preview.CutAudio(run, ready)
    out = tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    prepared = []
    assert ca.request("j1", "aaaaaaaaaaaaaaaa", ["x", str(out)], out,
                      prepare=lambda: prepared.append(1)) == "pending"
    assert ready.event.wait(WAIT)
    _wait_idle(ca)
    assert ready == ["j1"] and len(run.calls) == 1 and prepared == [1]
    assert ca.request("j1", "aaaaaaaaaaaaaaaa", ["x", str(out)], out) == "ready"
    assert len(run.calls) == 1
    ca.shutdown()


def test_latest_request_wins(tmp_path):
    gate = threading.Event()
    run, ready = FakeRun(gate=gate), Ready()
    ca = preview.CutAudio(run, ready)
    outs = [tmp_path / f"audio_cut.{c * 16}.m4a" for c in "abc"]
    assert ca.request("j1", "a" * 16, ["x", str(outs[0])], outs[0]) == "pending"
    assert run.started.wait(WAIT)
    # 돌고 있는 같은 key 는 다시 넣지 않는다
    assert ca.request("j1", "a" * 16, ["x", str(outs[0])], outs[0]) == "pending"
    # 하나가 도는 동안 두 요청 → 마지막 것만 실행
    assert ca.request("j1", "b" * 16, ["x", str(outs[1])], outs[1]) == "pending"
    assert ca.request("j1", "c" * 16, ["x", str(outs[2])], outs[2]) == "pending"
    gate.set()
    _wait_idle(ca)
    assert [c[-1] for c in run.calls] == [str(outs[0]), str(outs[2])]
    assert ready == ["j1", "j1"]
    ca.shutdown()


def test_failed_key_is_remembered(tmp_path):
    run, ready = FakeRun(RunResult(False, "오류: 망가짐")), Ready()
    ca = preview.CutAudio(run, ready)
    out = tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    assert ca.request("j1", "a" * 16, ["x", str(out)], out) == "pending"
    _wait_idle(ca)
    assert len(run.calls) == 1
    assert ca.request("j1", "a" * 16, ["x", str(out)], out) == "failed"
    assert len(run.calls) == 1
    out2 = tmp_path / "audio_cut.bbbbbbbbbbbbbbbb.m4a"
    assert ca.request("j1", "b" * 16, ["x", str(out2)], out2) == "pending"
    _wait_idle(ca)
    assert len(run.calls) == 2
    ca.shutdown()


def test_busy_lock_is_not_remembered_as_failed(tmp_path):
    run = FakeRun(RunResult(False, "오류: 같은 영상을 이미 처리 중입니다 (pid 1). 끝날 때까지 기다리세요."))
    ca = preview.CutAudio(run, Ready())
    out = tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    assert ca.request("j1", "a" * 16, ["x", str(out)], out) == "pending"
    _wait_idle(ca)
    assert ca.request("j1", "a" * 16, ["x", str(out)], out) == "pending"
    _wait_idle(ca)
    assert len(run.calls) == 2
    ca.shutdown()


def test_prunes_old_cut_audio(tmp_path):
    run, ready = FakeRun(), Ready()
    ca = preview.CutAudio(run, ready, keep=3)
    keys = [c * 16 for c in "abcde"]
    for k in keys:
        out = tmp_path / f"audio_cut.{k}.m4a"
        assert ca.request("j1", k, ["x", str(out)], out) == "pending"
        _wait_idle(ca)
    (tmp_path / "audio_full.m4a").write_bytes(b"f")
    names = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("audio_cut."))
    assert names == sorted(f"audio_cut.{k}.{e}" for k in keys[2:] for e in ("m4a", "index.json"))
    assert (tmp_path / "audio_full.m4a").is_file()
    ca.shutdown()


def test_shutdown_cancels_running(tmp_path):
    gate = threading.Event()
    cancelled = []

    class Handle:
        def cancel(self):
            cancelled.append(1)
            gate.set()

    ca = None

    def run(cmd):
        ca.started(Handle())
        gate.wait(WAIT)
        return RunResult(False, "cancelled")

    ready = Ready()
    ca = preview.CutAudio(run, ready)
    out = tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    ca.request("j1", "a" * 16, ["x", str(out)], out)
    for _ in range(100):
        if ca._running is not None:
            break
        threading.Event().wait(0.02)
    ca.shutdown()
    assert cancelled == [1]
    assert ca.request("j1", "a" * 16, ["x", str(out)], out) == "failed"


def test_preview_audio_command(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "pipeline_cmd", lambda: ["PY", "pipeline.py"])
    src = tmp_path / "a.mp4"
    job = Job(id="0123456789ab", src=str(src), options=JobOptions(denoise=70, loudnorm="keep"))
    jd = runner.paths.job_dir(src)
    cmd = runner.preview_audio_command(job, ModelPaths("W", "C"), "auto", tmp_path / "c.json", tmp_path / "o.m4a")
    assert cmd[:2] == ["PY", "pipeline.py"]
    assert cmd[2:13] == [str(src), "--apply-cuts", str(tmp_path / "c.json"),
                         "--preview-cut-audio", str(tmp_path / "o.m4a"), "--progress-json",
                         "--out", str(jd / "out"), "--work-root", str(jd / "work"), "--model"]
    assert cmd[-4:] == ["--denoise-level", "70", "--render-loudnorm", "off"]
    assert "--ctc-model" in cmd and "--device" in cmd


class CancellableRun:
    """job 마다 gate 를 기다린다. started 로 handle 을 알리고, cancel 되면 "cancelled"."""

    def __init__(self):
        self.ca = None
        self.calls = []
        self.gates: dict[str, threading.Event] = {}
        self.running = threading.Event()
        self.stopped_at = []

    def __call__(self, cmd):
        job = cmd[0]
        self.calls.append(job)
        gate = self.gates.setdefault(job, threading.Event())
        state = {"cancelled": False}

        class Handle:
            def cancel(h):
                state["cancelled"] = True
                gate.set()

        self.ca.started(Handle())
        self.running.set()
        gate.wait(WAIT)
        self.stopped_at.append(job)
        if state["cancelled"]:
            return RunResult(False, "cancelled")
        _make(Path(cmd[-1]))
        return RunResult(True)


def _cancellable():
    run, ready = CancellableRun(), Ready()
    ca = preview.CutAudio(run, ready)
    run.ca = ca
    return ca, run, ready


def test_cancel_drops_pending_build_for_job(tmp_path):
    ca, run, ready = _cancellable()
    o2, o1 = tmp_path / "audio_cut.bbbbbbbbbbbbbbbb.m4a", tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    ca.request("j2", "b" * 16, ["j2", str(o2)], o2)
    assert run.running.wait(WAIT)
    ca.request("j1", "a" * 16, ["j1", str(o1)], o1)     # j2 뒤에서 대기
    assert ca.cancel("j1", 1.0) is True
    run.gates["j2"].set()
    _wait_idle(ca)
    assert run.calls == ["j2"] and ready == ["j2"]
    # 막힌 동안 새 요청은 받지 않고, 실패로 기억하지도 않는다
    assert ca.request("j1", "a" * 16, ["j1", str(o1)], o1) == "failed"
    ca.release("j1")
    run.gates["j1"] = threading.Event()
    run.gates["j1"].set()
    assert ca.request("j1", "a" * 16, ["j1", str(o1)], o1) == "pending"
    _wait_idle(ca)
    assert run.calls == ["j2", "j1"]
    ca.shutdown()


def test_cancel_stops_running_build_and_waits(tmp_path):
    ca, run, ready = _cancellable()
    o1 = tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    ca.request("j1", "a" * 16, ["j1", str(o1)], o1)
    assert run.running.wait(WAIT)
    assert ca.cancel("j1", WAIT) is True
    assert run.stopped_at == ["j1"]          # 돌아올 때는 이미 멈춰 있다
    _wait_idle(ca)
    assert ready == []                       # 멈춘 것은 알리지 않는다
    ca.release("j1")
    run.gates["j1"] = threading.Event()
    run.gates["j1"].set()
    assert ca.request("j1", "a" * 16, ["j1", str(o1)], o1) == "pending"   # 실패로 기억 안 함
    _wait_idle(ca)
    assert ready == ["j1"]
    ca.shutdown()


def test_cancel_leaves_other_jobs_alone(tmp_path):
    ca, run, ready = _cancellable()
    o2 = tmp_path / "audio_cut.bbbbbbbbbbbbbbbb.m4a"
    ca.request("j2", "b" * 16, ["j2", str(o2)], o2)
    assert run.running.wait(WAIT)
    assert ca.cancel("j1", 1.0) is True
    assert run.stopped_at == []              # j2 는 계속 돈다
    run.gates["j2"].set()
    _wait_idle(ca)
    assert ready == ["j2"] and preview.is_ready(o2)
    ca.release("j1")
    ca.shutdown()


def test_cancel_times_out(tmp_path):
    gate = threading.Event()
    run, ready = FakeRun(gate=gate), Ready()     # handle 을 알리지 않는 run — 멈출 수 없다
    ca = preview.CutAudio(run, ready)
    o1 = tmp_path / "audio_cut.aaaaaaaaaaaaaaaa.m4a"
    ca.request("j1", "a" * 16, ["j1", str(o1)], o1)
    assert run.started.wait(WAIT)
    assert ca.cancel("j1", 0.1) is False
    gate.set()
    _wait_idle(ca)
    assert ready == []
    ca.release("j1")
    ca.shutdown()
