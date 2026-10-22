import json
import threading
from pathlib import Path

from app import paths
from app.jobqueue import Queue
from app.runner import RunResult
from app.store import Status, Store

WAIT = 5


def cuts_path(job) -> Path:
    src = Path(job.src)
    return paths.job_dir(src) / "out" / f"{src.stem}.cuts.json"


def write_cuts(job, cuts):
    p = cuts_path(job)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cuts": cuts}), encoding="utf-8")


class FakeExec:
    """호출 순서를 기록. gate 에 등록된 (id, kind) 는 release 될 때까지 멈춘다."""

    def __init__(self):
        self.calls = []
        self.gates = {}      # (id, kind) -> Event
        self.started = {}    # (id, kind) -> Event
        self.results = {}    # (id, kind) -> RunResult
        self.side = {}       # (id, kind) -> callable(job)
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def gate(self, job_id, kind):
        self.gates[(job_id, kind)] = threading.Event()
        self.started[(job_id, kind)] = threading.Event()

    def __call__(self, job, kind, on_start):
        key = (job.id, kind)
        with self.lock:
            self.calls.append(key)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        cancelled = threading.Event()

        class H:
            def cancel(self_inner):
                cancelled.set()
                if key in self.gates:
                    self.gates[key].set()

        on_start(H())
        if key in self.started:
            self.started[key].set()
        if key in self.gates:
            assert self.gates[key].wait(WAIT)
        try:
            if cancelled.is_set():
                return RunResult(False, "cancelled")
            if key in self.side:
                self.side[key](job)
            elif kind == "analyze":
                write_cuts(job, [{"start": 0.0, "end": 0.5}])
            return self.results.get(key, RunResult(True))
        finally:
            with self.lock:
                self.active -= 1


def make(tmp_path, names=("a", "b", "c")):
    store = Store(tmp_path / "state.json")
    jobs = []
    for n in names:
        p = tmp_path / f"{n}.mp4"
        p.write_bytes(b"x")
        jobs.append(store.add(p))
    fx = FakeExec()
    changes = []
    q = Queue(store, fx, lambda j: changes.append((j.id, j.status)))
    return store, jobs, fx, q, changes


def test_runs_in_order_one_at_a_time(tmp_path):
    store, (a, b, c), fx, q, changes = make(tmp_path)
    q.start_all()
    assert q.join(WAIT)
    assert fx.calls == [(a.id, "analyze"), (b.id, "analyze"), (c.id, "analyze")]
    assert fx.max_active == 1
    assert all(j.status == Status.REVIEW for j in (a, b, c))
    assert a.summary["cuts"] == 1 and abs(a.summary["removed_sec"] - 0.5) < 1e-9
    assert (a.id, Status.PROCESSING) in changes and (a.id, Status.REVIEW) in changes
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert [j["status"] for j in saved["jobs"]] == ["review"] * 3


def test_save_jumps_ahead_of_waiting_analysis(tmp_path):
    store, (a, b, c, x), fx, q, _ = make(tmp_path, ("a", "b", "c", "x"))
    x.status = Status.REVIEW
    fx.gate(a.id, "analyze")
    q.start_all()
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    q.request_save(x.id)
    assert x.status == Status.SAVING
    fx.gates[(a.id, "analyze")].set()
    assert q.join(WAIT)
    assert fx.calls == [(a.id, "analyze"), (x.id, "render"), (b.id, "analyze"), (c.id, "analyze")]
    assert x.status == Status.SAVED


def test_saves_keep_their_own_order(tmp_path):
    store, (a, b, x, y), fx, q, _ = make(tmp_path, ("a", "b", "x", "y"))
    x.status = y.status = Status.REVIEW
    fx.gate(a.id, "analyze")
    q.start_all()
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    q.request_save(x.id)
    q.request_save(y.id)
    fx.gates[(a.id, "analyze")].set()
    assert q.join(WAIT)
    assert fx.calls == [(a.id, "analyze"), (x.id, "render"), (y.id, "render"), (b.id, "analyze")]


def test_queue_continues_after_failure(tmp_path):
    store, (a, b), fx, q, _ = make(tmp_path, ("a", "b"))
    fx.results[(a.id, "analyze")] = RunResult(False, "오류: 망가짐")
    fx.side[(a.id, "analyze")] = lambda job: None
    q.start_all()
    assert q.join(WAIT)
    assert a.status == Status.FAILED and a.error == "오류: 망가짐"
    assert b.status == Status.REVIEW


def test_missing_cuts_after_success_fails_readably(tmp_path):
    store, (a, b), fx, q, _ = make(tmp_path, ("a", "b"))
    fx.side[(a.id, "analyze")] = lambda job: None   # 성공인데 cuts.json 없음
    q.start_all()
    assert q.join(WAIT)
    assert a.status == Status.FAILED and a.error
    assert b.status == Status.REVIEW


def test_render_failure_marks_failed(tmp_path):
    store, (a,), fx, q, _ = make(tmp_path, ("a",))
    a.status = Status.REVIEW
    fx.results[(a.id, "render")] = RunResult(False, "디스크 가득")
    q.request_save(a.id)
    assert q.join(WAIT)
    assert a.status == Status.FAILED and a.error == "디스크 가득"


def test_rejudge_keeps_prev_cuts(tmp_path):
    store, (a,), fx, q, _ = make(tmp_path, ("a",))
    a.status = Status.REVIEW
    old = [{"start": 1.0, "end": 2.0}]
    write_cuts(a, old)
    fx.side[(a.id, "analyze")] = lambda job: write_cuts(job, [{"start": 3.0, "end": 3.25}])
    q.rejudge(a.id)
    assert q.join(WAIT)
    assert a.status == Status.REVIEW
    assert a.prev_cuts == str(paths.job_dir(Path(a.src)) / "out" / "prev.cuts.json")
    assert json.loads(Path(a.prev_cuts).read_text(encoding="utf-8"))["cuts"] == old
    assert a.summary["cuts"] == 1 and abs(a.summary["removed_sec"] - 0.25) < 1e-9


def test_saved_then_option_change_goes_review(tmp_path):
    store, (a,), fx, q, changes = make(tmp_path, ("a",))
    q.start_all()
    assert q.join(WAIT)
    q.request_save(a.id)
    assert q.join(WAIT)
    assert a.status == Status.SAVED
    a.options.habit = "all"
    q.rejudge(a.id)
    assert q.join(WAIT)
    assert a.status == Status.REVIEW
    assert fx.calls[-1] == (a.id, "analyze")


def test_rejudge_does_not_wait_behind_running_analysis(tmp_path):
    store, (a, b), fx, q, _ = make(tmp_path, ("a", "b"))
    b.status = Status.REVIEW
    write_cuts(b, [])
    fx.gate(a.id, "analyze")
    q.start_all()
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    done = threading.Event()
    fx.side[(b.id, "analyze")] = lambda job: (write_cuts(job, []), done.set())
    q.rejudge(b.id)
    assert done.wait(WAIT)          # a 가 아직 멈춰 있는데도 b 재판정이 돈다
    assert a.status == Status.PROCESSING
    fx.gates[(a.id, "analyze")].set()
    assert q.join(WAIT)
    assert a.status == b.status == Status.REVIEW


def test_rejudge_ignored_for_other_states(tmp_path):
    store, (a,), fx, q, _ = make(tmp_path, ("a",))
    q.rejudge(a.id)   # waiting
    assert q.join(WAIT)
    assert fx.calls == [] and a.status == Status.WAITING


def test_repeated_rejudge_runs_one_followup(tmp_path):
    store, (a,), fx, q, _ = make(tmp_path, ("a",))
    a.status = Status.REVIEW
    write_cuts(a, [])
    fx.gate(a.id, "analyze")
    seen = []
    first = fx.gates[(a.id, "analyze")]

    def side(job):
        seen.append(job.options.habit)
        write_cuts(job, [])
        fx.gates.pop((a.id, "analyze"), None)   # 후속 실행은 멈추지 않음
    fx.side[(a.id, "analyze")] = side
    q.rejudge(a.id)
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    a.options.habit = "off"
    q.rejudge(a.id)
    a.options.habit = "all"
    q.rejudge(a.id)
    first.set()
    assert q.join(WAIT)
    assert fx.calls == [(a.id, "analyze")] * 2
    assert fx.max_active == 1
    assert seen[-1] == "all"
    assert a.status == Status.REVIEW


def test_cancel_waiting_removes(tmp_path):
    store, (a, b), fx, q, _ = make(tmp_path, ("a", "b"))
    fx.gate(a.id, "analyze")
    q.start_all()
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    q.cancel(b.id)
    assert store.get(b.id) is None
    fx.gates[(a.id, "analyze")].set()
    assert q.join(WAIT)
    assert fx.calls == [(a.id, "analyze")]


def test_cancel_running_removes_after_return(tmp_path):
    store, (a, b), fx, q, changes = make(tmp_path, ("a", "b"))
    fx.gate(a.id, "analyze")
    q.start_all()
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    q.cancel(a.id)
    assert q.join(WAIT)
    assert store.get(a.id) is None
    assert b.status == Status.REVIEW
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert [j["id"] for j in saved["jobs"]] == [b.id]


def test_start_all_does_not_double_queue(tmp_path):
    store, (a,), fx, q, _ = make(tmp_path, ("a",))
    fx.gate(a.id, "analyze")
    q.start_all()
    assert fx.started[(a.id, "analyze")].wait(WAIT)
    q.start_all()
    fx.gates[(a.id, "analyze")].set()
    assert q.join(WAIT)
    assert fx.calls == [(a.id, "analyze")]


def test_set_progress(tmp_path):
    store, (a,), fx, q, changes = make(tmp_path, ("a",))
    q.set_progress(a.id, 0.4)
    assert a.progress == 0.4 and changes[-1] == (a.id, a.status)
