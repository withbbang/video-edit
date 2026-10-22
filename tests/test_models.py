import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

from app import models
from app.paths import ModelPaths

CONTENT = {"model.bin": bytes(range(40)), "config.json": b"{\"a\": 1}", "model.safetensors": b"S" * 12}


def entry(key, repo, rev, names):
    files = [{"name": n, "size": len(CONTENT[n]), "sha256": hashlib.sha256(CONTENT[n]).hexdigest()} for n in names]
    return {"key": key, "repo": repo, "revision": rev, "bytes": sum(f["size"] for f in files), "files": files}


MAN = {"models": [entry("whisper", "org/whisper", "aaa111", ["model.bin", "config.json"]),
                  entry("ctc", "org/ctc", "bbb222", ["model.safetensors"])],
       "cuda_win": []}
TOTAL = 40 + 8 + 12


def raise_(exc):
    def f(*a, **k):
        raise exc
    return f


class FakeResp:
    def __init__(self, body: bytes, status: int, fail_after: int | None):
        self.buf, self.status, self.fail_after, self.sent = io.BytesIO(body), status, fail_after, 0

    def read(self, n):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("끊김")
        if self.fail_after is not None:
            n = min(n, self.fail_after - self.sent)
        data = self.buf.read(n)
        self.sent += len(data)
        return data

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeServer:
    """url → 내용. Range 를 지키거나(206) 무시(200)한다. fail_after 바이트 뒤에 연결이 끊긴다 (한 번)."""

    def __init__(self, files: dict, honor_range=True, fail_after=None):
        self.files, self.honor_range, self.fail_after = files, honor_range, fail_after
        self.requests = []

    def __call__(self, req, timeout=None):
        rng = req.get_header("Range")
        self.requests.append((req.full_url, rng))
        data = self.files[req.full_url]
        if rng and self.honor_range:
            body, status = data[int(rng[len("bytes="):-1]):], 206
        else:
            body, status = data, 200
        fail, self.fail_after = self.fail_after, None
        return FakeResp(body, status, fail)


def model_server(man=MAN, **kw):
    files = {}
    for m in man["models"]:
        for f in m["files"]:
            files[models._file_url(m, f["name"])] = CONTENT[f["name"]]
    return FakeServer(files, **kw)


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(models, "CHUNK", 4)


def wdir(root):
    return root / "models" / "whisper" / "aaa111"


def test_status_lists_missing(tmp_path):
    assert [m["key"] for m in models.status(MAN, tmp_path)] == ["whisper", "ctc"]
    d = tmp_path / "models" / "ctc" / "bbb222"
    d.mkdir(parents=True)
    (d / ".complete").touch()
    assert [m["key"] for m in models.status(MAN, tmp_path)] == ["whisper"]


def test_file_url_is_pinned_revision():
    assert models._file_url(MAN["models"][0], "model.bin") == \
        "https://huggingface.co/org/whisper/resolve/aaa111/model.bin"


def test_download_marks_complete_and_reports_progress(tmp_path, monkeypatch, small_chunks):
    srv = model_server()
    monkeypatch.setattr(models, "_urlopen", srv)
    seen = []
    p = models.download(MAN, tmp_path, lambda d, t: seen.append((d, t)))
    assert p == ModelPaths(whisper=str(wdir(tmp_path)), ctc=str(tmp_path / "models" / "ctc" / "bbb222"))
    assert models.status(MAN, tmp_path) == []
    assert (wdir(tmp_path) / "model.bin").read_bytes() == CONTENT["model.bin"]
    assert not list(tmp_path.rglob("*.part"))
    assert seen[0] == (0, TOTAL) and seen[-1] == (TOTAL, TOTAL) and len(seen) > 3
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
    assert models.paths(MAN, tmp_path) == p
    srv.requests.clear()
    models.download(MAN, tmp_path, lambda d, t: None)
    assert srv.requests == []


def test_download_failure_raises_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_urlopen", raise_(urllib.error.URLError("network")))
    with pytest.raises(models.DownloadError):
        models.download(MAN, tmp_path, lambda d, t: None)
    assert models.status(MAN, tmp_path)  # 여전히 받아야 함


def test_resume_continues_from_part(tmp_path, monkeypatch, small_chunks):
    # 같은 프로세스에서 다시 시도: 끊긴 곳(.part 크기)부터 Range 로
    srv = model_server(fail_after=12)
    monkeypatch.setattr(models, "_urlopen", srv)
    with pytest.raises(models.DownloadError):
        models.download(MAN, tmp_path, lambda d, t: None)
    part = wdir(tmp_path) / "model.bin.part"
    assert part.stat().st_size == 12
    seen = []
    models.download(MAN, tmp_path, lambda d, t: seen.append(d))
    url = models._file_url(MAN["models"][0], "model.bin")
    assert srv.requests[1] == (url, "bytes=12-")
    assert seen[0] == 12  # 받아 둔 만큼에서 시작
    assert (wdir(tmp_path) / "model.bin").read_bytes() == CONTENT["model.bin"] and not part.exists()
    # 프로세스가 죽은 뒤: 남은 .part 만 보고 이어 받는다
    root2 = tmp_path / "r2"
    (wdir(root2)).mkdir(parents=True)
    (wdir(root2) / "model.bin.part").write_bytes(CONTENT["model.bin"][:30])
    srv.requests.clear()
    models.download(MAN, root2, lambda d, t: None)
    assert srv.requests[0] == (url, "bytes=30-")
    assert (wdir(root2) / "model.bin").read_bytes() == CONTENT["model.bin"]


def test_sha_mismatch_deletes_part_and_raises(tmp_path, monkeypatch):
    srv = model_server()
    url = models._file_url(MAN["models"][0], "model.bin")
    srv.files[url] = bytes(reversed(CONTENT["model.bin"]))  # 크기는 같고 내용이 다름
    monkeypatch.setattr(models, "_urlopen", srv)
    with pytest.raises(models.DownloadError):
        models.download(MAN, tmp_path, lambda d, t: None)
    assert not (wdir(tmp_path) / "model.bin.part").exists()
    assert not (wdir(tmp_path) / "model.bin").exists()
    assert models.status(MAN, tmp_path)


def test_200_on_range_restarts_without_progress_regression(tmp_path, monkeypatch, small_chunks):
    wdir(tmp_path).mkdir(parents=True)
    (wdir(tmp_path) / "model.bin.part").write_bytes(b"\xff" * 20)  # 서버가 Range 를 무시하면 버려야 할 조각
    srv = model_server(honor_range=False)
    monkeypatch.setattr(models, "_urlopen", srv)
    seen = []
    models.download(MAN, tmp_path, lambda d, t: seen.append(d))
    assert srv.requests[0][1] == "bytes=20-"
    assert (wdir(tmp_path) / "model.bin").read_bytes() == CONTENT["model.bin"]
    assert seen[0] == 20 and seen == sorted(seen) and seen[-1] == TOTAL


def test_part_larger_than_file_is_discarded(tmp_path, monkeypatch):
    wdir(tmp_path).mkdir(parents=True)
    (wdir(tmp_path) / "model.bin.part").write_bytes(b"x" * 99)
    srv = model_server()
    monkeypatch.setattr(models, "_urlopen", srv)
    models.download(MAN, tmp_path, lambda d, t: None)
    assert srv.requests[0][1] is None
    assert (wdir(tmp_path) / "model.bin").read_bytes() == CONTENT["model.bin"]


def test_cleanup_removes_old_revision(tmp_path):
    keep_cuda = f"cuda/{models.cuda_hash(MAN)}"
    for rel in ["models/whisper/old000", "models/whisper/aaa111", "models/ctc/bbb222", "models/gone/x",
                "cuda/oldhash", keep_cuda]:
        (tmp_path / rel).mkdir(parents=True)
    models.cleanup(MAN, tmp_path)
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.glob("models/*/*")) + \
        sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "cuda").iterdir())
    assert left == ["models/ctc/bbb222", "models/whisper/aaa111", keep_cuda]


def test_cuda_dir_keyed_by_cuda_list_only(tmp_path):
    man2 = json.loads(json.dumps(MAN))
    man2["models"][0]["revision"] = "ccc333"
    assert models.cuda_dir(man2, tmp_path) == models.cuda_dir(MAN, tmp_path)
    assert models.manifest_hash(man2) != models.manifest_hash(MAN)
    man2["cuda_win"] = [{"name": "x", "url": "u", "sha256": "0" * 64, "size": 1}]
    assert models.cuda_dir(man2, tmp_path) != models.cuda_dir(MAN, tmp_path)


def test_manifest_file_is_pinned():
    man = models.load_manifest()
    assert [m["key"] for m in man["models"]] == ["whisper", "ctc"]
    for m in man["models"]:
        assert len(m["revision"]) == 40 and m["files"]
        assert m["bytes"] == sum(f["size"] for f in m["files"])
        assert all(len(f["sha256"]) == 64 and f["size"] > 0 for f in m["files"])
    assert [c["name"] for c in man["cuda_win"]] == ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
    for c in man["cuda_win"]:
        assert c["url"].startswith("https://files.pythonhosted.org/") and c["url"].endswith("win_amd64.whl")
        assert len(c["sha256"]) == 64 and c["size"] > 0


def test_device_choice(monkeypatch):
    key = models.manifest_hash(MAN)
    monkeypatch.setattr(models.sys, "platform", "win32")
    assert models.device({"gpu": {"key": key, "ok": True}}, MAN) == "cuda"
    assert models.device({"gpu": {"key": key, "ok": False}}, MAN) == "cpu"
    assert models.device({"gpu": {"key": "옛것", "ok": True}}, MAN) == "cpu"  # manifest 가 바뀌었는데 아직 시험 전
    assert models.device({"gpu": None}, MAN) == "cpu"
    monkeypatch.setattr(models.sys, "platform", "darwin")
    assert models.device({"gpu": {"key": key, "ok": True}}, MAN) == "auto"
    assert models.CUDA_COMPUTE_TYPE == "int8_float16"


def test_pipeline_env(tmp_path, monkeypatch):
    key = models.manifest_hash(MAN)
    monkeypatch.setattr(models.sys, "platform", "win32")
    assert models.pipeline_env(MAN, tmp_path, {"gpu": {"key": key, "ok": True}}) == \
        {"YTEDIT_CUDA_DIR": str(models.cuda_dir(MAN, tmp_path))}
    assert models.pipeline_env(MAN, tmp_path, {"gpu": {"key": "옛것", "ok": True}}) == {}
    monkeypatch.setattr(models.sys, "platform", "darwin")
    assert models.pipeline_env(MAN, tmp_path, {"gpu": {"key": key, "ok": True}}) == {}


# ---- 윈도우 GPU (여기선 monkeypatch 로만 시험) ----

def test_has_nvidia(monkeypatch):
    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setattr(models.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert models.has_nvidia()
    monkeypatch.setattr(models.subprocess, "run", raise_(FileNotFoundError("nvidia-smi")))
    assert not models.has_nvidia()
    monkeypatch.setattr(models.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 9))
    assert not models.has_nvidia()
    monkeypatch.setattr(models.sys, "platform", "darwin")
    monkeypatch.setattr(models.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert not models.has_nvidia()


def make_wheel(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


WHEELS = {"nvidia-cublas-cu12": make_wheel({"nvidia/cublas/bin/cublas64_12.dll": b"a" * 5,
                                           "nvidia/cublas/bin/cublasLt64_12.dll": b"b",
                                           "nvidia/cublas/include/cublas.h": b"h"}),
          "nvidia-cudnn-cu12": make_wheel({"nvidia/cudnn/bin/cudnn64_9.dll": b"c",
                                          "nvidia_cudnn_cu12-9.dist-info/METADATA": b"m"})}
CUDA_TOTAL = sum(len(w) for w in WHEELS.values())


def cuda_manifest(wheels=WHEELS) -> dict:
    man = json.loads(json.dumps(MAN))
    man["cuda_win"] = [{"name": n, "url": f"https://example.invalid/{n}.whl",
                        "sha256": hashlib.sha256(w).hexdigest(), "size": len(w)} for n, w in wheels.items()]
    return man


def full_server(man, **kw):
    srv = model_server(man, **kw)
    for n, w in WHEELS.items():
        srv.files[f"https://example.invalid/{n}.whl"] = w
    return srv


def test_download_cuda_extracts_only_dlls(tmp_path, monkeypatch):
    man = cuda_manifest()
    srv = full_server(man)
    monkeypatch.setattr(models, "_urlopen", srv)
    seen = []
    d = models.download_cuda(man, tmp_path, lambda a, t: seen.append((a, t)))
    assert d == tmp_path / "cuda" / models.cuda_hash(man) == models.cuda_dir(man, tmp_path)
    assert sorted(p.name for p in d.iterdir() if p.name != ".complete") == \
        ["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"]
    assert models.cuda_ready(man, tmp_path) and seen[-1] == (CUDA_TOTAL, CUDA_TOTAL)
    assert not list(tmp_path.rglob("*.whl"))
    srv.requests.clear()
    models.download_cuda(man, tmp_path, lambda a, t: None)
    assert srv.requests == []


def test_download_cuda_bad_hash_fails(tmp_path, monkeypatch):
    man = cuda_manifest()
    man["cuda_win"][0]["sha256"] = "0" * 64
    monkeypatch.setattr(models, "_urlopen", full_server(man))
    with pytest.raises(models.DownloadError):
        models.download_cuda(man, tmp_path, lambda a, t: None)
    assert not models.cuda_ready(man, tmp_path)


def test_probe_gpu_command_and_env(tmp_path, monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(models.subprocess, "run", run)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert models.probe_gpu(tmp_path, MAN)
    assert seen["cmd"] == [sys.executable, "-m", "app", "--probe-gpu"]
    assert seen["env"]["YTEDIT_DATA_DIR"] == str(tmp_path)
    assert seen["env"]["YTEDIT_CUDA_DIR"] == str(models.cuda_dir(MAN, tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(models.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3221226505))
    assert not models.probe_gpu(tmp_path, MAN)
    monkeypatch.setattr(models.subprocess, "run", run)
    models.probe_gpu(tmp_path, MAN)
    assert seen["cmd"] == [sys.executable, "--probe-gpu"]
    monkeypatch.setattr(models.subprocess, "run", raise_(subprocess.TimeoutExpired("x", 1)))
    assert not models.probe_gpu(tmp_path, MAN)


def test_run_gpu_probe(tmp_path, monkeypatch):
    import faster_whisper
    seen = {}

    class FakeModel:
        def __init__(self, path, device, compute_type):
            seen.update(path=path, device=device, compute_type=compute_type)

        def transcribe(self, audio, **kw):
            seen["n"] = len(audio)
            return iter([]), None
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    assert models.run_gpu_probe(tmp_path, MAN) == 0
    assert seen == {"path": models.paths(MAN, tmp_path).whisper, "device": "cuda",
                    "compute_type": models.CUDA_COMPUTE_TYPE, "n": 16000}
    monkeypatch.setattr(faster_whisper, "WhisperModel", raise_(RuntimeError("no cudnn")))
    assert models.run_gpu_probe(tmp_path, MAN) == 1


# ---- prepare: 한 번에 받기 + GPU 결정 ----

def win_gpu(monkeypatch, nvidia=True, probe=True):
    calls = {"nvidia": 0, "probe": 0}
    monkeypatch.setattr(models.sys, "platform", "win32")

    def has():
        calls["nvidia"] += 1
        return nvidia

    def pr(root, man=None):
        calls["probe"] += 1
        return probe
    monkeypatch.setattr(models, "has_nvidia", has)
    monkeypatch.setattr(models, "probe_gpu", pr)
    return calls


def test_prepare_windows_gpu_single_progress(tmp_path, monkeypatch):
    man = cuda_manifest()
    monkeypatch.setattr(models, "_urlopen", full_server(man))
    calls = win_gpu(monkeypatch)
    (tmp_path / "models" / "whisper" / "old000").mkdir(parents=True)
    settings, seen = {"gpu": None}, []
    p = models.prepare(man, tmp_path, settings, lambda d, t: seen.append((d, t)))
    assert p == models.paths(man, tmp_path) and models.status(man, tmp_path) == []
    assert {t for _, t in seen} == {TOTAL + CUDA_TOTAL} and seen[-1] == (TOTAL + CUDA_TOTAL,) * 2
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
    assert settings["gpu"] == {"key": models.manifest_hash(man), "ok": True}
    assert models.cuda_ready(man, tmp_path) and calls == {"nvidia": 1, "probe": 1}
    assert not (tmp_path / "models" / "whisper" / "old000").exists()  # cleanup
    assert models.device(settings, man) == "cuda"
    # 같은 manifest 로 다시: GPU 는 다시 정하지 않는다
    models.prepare(man, tmp_path, settings, lambda d, t: None)
    assert calls == {"nvidia": 1, "probe": 1}


def test_prepare_cuda_failure_falls_back_to_cpu(tmp_path, monkeypatch):
    man = cuda_manifest()
    srv = full_server(man)
    del srv.files["https://example.invalid/nvidia-cudnn-cu12.whl"]  # KeyError 대신 네트워크 오류로
    real = srv.__call__

    def opener(req, timeout=None):
        if "example.invalid" in req.full_url and "cudnn" in req.full_url:
            raise urllib.error.URLError("network")
        return real(req, timeout)
    monkeypatch.setattr(models, "_urlopen", opener)
    calls = win_gpu(monkeypatch)
    settings = {"gpu": None}
    models.prepare(man, tmp_path, settings, lambda d, t: None)  # 올리지 않는다
    assert settings["gpu"] == {"key": models.manifest_hash(man), "ok": False}
    assert calls["probe"] == 0 and models.status(man, tmp_path) == []
    assert models.device(settings, man) == "cpu"


def test_prepare_probe_failure_and_no_nvidia(tmp_path, monkeypatch):
    man = cuda_manifest()
    srv = full_server(man)
    monkeypatch.setattr(models, "_urlopen", srv)
    win_gpu(monkeypatch, probe=False)
    settings = {"gpu": None}
    models.prepare(man, tmp_path, settings, lambda d, t: None)
    assert settings["gpu"] == {"key": models.manifest_hash(man), "ok": False}
    # NVIDIA 없음: CUDA 를 받지 않고 total 에도 넣지 않는다
    root2, seen = tmp_path / "r2", []
    srv.requests.clear()
    calls = win_gpu(monkeypatch, nvidia=False)
    settings = {"gpu": None}
    models.prepare(man, root2, settings, lambda d, t: seen.append(t))
    assert set(seen) == {TOTAL} and calls["probe"] == 0
    assert not any("example.invalid" in u for u, _ in srv.requests)
    assert settings["gpu"] == {"key": models.manifest_hash(man), "ok": False}


def test_prepare_model_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_urlopen", raise_(urllib.error.URLError("network")))
    win_gpu(monkeypatch)
    settings = {"gpu": None}
    with pytest.raises(models.DownloadError):
        models.prepare(cuda_manifest(), tmp_path, settings, lambda d, t: None)
    assert settings["gpu"] is None


def test_prepare_on_mac_leaves_gpu_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_urlopen", model_server())
    monkeypatch.setattr(models.sys, "platform", "darwin")
    monkeypatch.setattr(models, "has_nvidia", raise_(AssertionError("맥에선 안 부름")))
    settings = {"gpu": None}
    models.prepare(MAN, tmp_path, settings, lambda d, t: None)
    assert settings["gpu"] is None and models.status(MAN, tmp_path) == []


def test_pipeline_uses_cuda_dir(tmp_path, monkeypatch):
    import pipeline
    added = []
    monkeypatch.setattr(pipeline.os, "add_dll_directory", added.append, raising=False)
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setenv("YTEDIT_CUDA_DIR", str(tmp_path))
    pipeline.use_cuda_dir(is_windows=True)
    assert added == [str(tmp_path)] and os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    added.clear()
    pipeline.use_cuda_dir(is_windows=False)
    monkeypatch.setenv("YTEDIT_CUDA_DIR", str(tmp_path / "없음"))
    pipeline.use_cuda_dir(is_windows=True)
    monkeypatch.delenv("YTEDIT_CUDA_DIR")
    pipeline.use_cuda_dir(is_windows=True)
    assert added == []


# ---- GPU 결정이 끊겼거나 manifest 가 바뀌면 다시 (final-review I2) ----

def test_gpu_undecided_conditions(tmp_path, monkeypatch):
    man = cuda_manifest()
    key = models.manifest_hash(man)
    monkeypatch.setattr(models.sys, "platform", "darwin")
    assert not models.gpu_undecided({"gpu": None}, man, tmp_path)          # 맥은 상관없음
    monkeypatch.setattr(models.sys, "platform", "win32")
    assert models.gpu_undecided({"gpu": None}, man, tmp_path)              # 모델 다 받은 뒤 CUDA 단계에서 끊김
    assert models.gpu_undecided({"gpu": {"key": "old", "ok": True}}, man, tmp_path)   # 업데이트로 manifest 바뀜
    assert not models.gpu_undecided({"gpu": {"key": key, "ok": False}}, man, tmp_path)  # CPU 로 정함
    assert models.gpu_undecided({"gpu": {"key": key, "ok": True}}, man, tmp_path)   # CUDA 폴더가 덜 갖춰짐
    d = models.cuda_dir(man, tmp_path)
    d.mkdir(parents=True)
    (d / models.COMPLETE).write_text("x")
    assert not models.gpu_undecided({"gpu": {"key": key, "ok": True}}, man, tmp_path)


def test_prepare_redecides_after_manifest_change_and_cleans_old_cuda(tmp_path, monkeypatch):
    man = cuda_manifest()
    monkeypatch.setattr(models, "_urlopen", full_server(man))
    calls = win_gpu(monkeypatch)
    old = tmp_path / "cuda" / "0123456789ab"
    old.mkdir(parents=True)
    settings = {"gpu": {"key": "old-manifest", "ok": True}}
    models.prepare(man, tmp_path, settings, lambda d, t: None)
    assert calls == {"nvidia": 1, "probe": 1}
    assert settings["gpu"] == {"key": models.manifest_hash(man), "ok": True}
    assert not old.exists() and models.cuda_ready(man, tmp_path)
    # GPU 로 정했는데 CUDA 폴더가 사라졌으면 다시 받고 다시 시험한다
    import shutil
    shutil.rmtree(models.cuda_dir(man, tmp_path))
    models.prepare(man, tmp_path, settings, lambda d, t: None)
    assert calls == {"nvidia": 2, "probe": 2} and models.cuda_ready(man, tmp_path)
