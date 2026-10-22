import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app import models
from app.paths import ModelPaths

MAN = {"models": [
    {"key": "whisper", "repo": "org/whisper", "revision": "aaa111", "bytes": 30,
     "files": ["model.bin", "config.json"]},
    {"key": "ctc", "repo": "org/ctc", "revision": "bbb222", "bytes": 12, "files": ["model.safetensors"]}],
    "cuda_win": []}

SIZES = {"model.bin": 20, "config.json": 10, "model.safetensors": 12}


def raise_(exc):
    def f(*a, **k):
        raise exc
    return f


def fake_snapshot(calls):
    def snapshot_download(repo_id, *, revision, local_dir, allow_patterns, tqdm_class, **kw):
        calls.append((repo_id, revision, Path(local_dir), list(allow_patterns)))
        # huggingface_hub 처럼: 파일 수 막대 + 전체 바이트 막대(이름으로 구분)를 만들고, 있는 파일은 건너뜀
        tqdm_class(total=len(allow_patterns), desc="Fetching").update(len(allow_patterns))
        bar = tqdm_class(name="huggingface_hub.snapshot_download", total=0, initial=0, unit="B")
        net = tqdm_class(name="huggingface_hub.snapshot_download.transfer", total=0, initial=0, unit="B")
        for name in allow_patterns:
            p = Path(local_dir) / name
            if p.exists():
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * SIZES[name])
            net.update(SIZES[name] // 2)  # 압축돼 덜 받은 경우: 쓴 바이트와 겹쳐 세지 않아야 함
            bar.update(SIZES[name])
        return str(local_dir)
    return snapshot_download


def test_status_lists_missing(tmp_path):
    assert [m["key"] for m in models.status(MAN, tmp_path)] == ["whisper", "ctc"]
    d = tmp_path / "models" / "ctc" / "bbb222"
    d.mkdir(parents=True)
    (d / ".complete").touch()
    assert [m["key"] for m in models.status(MAN, tmp_path)] == ["whisper"]


def test_download_marks_complete_and_reports_progress(tmp_path, monkeypatch):
    calls, seen = [], []
    monkeypatch.setattr(models, "snapshot_download", fake_snapshot(calls))
    p = models.download(MAN, tmp_path, lambda d, t: seen.append((d, t)))
    assert p == ModelPaths(whisper=str(tmp_path / "models" / "whisper" / "aaa111"),
                           ctc=str(tmp_path / "models" / "ctc" / "bbb222"))
    assert models.status(MAN, tmp_path) == []
    assert calls[0] == ("org/whisper", "aaa111", tmp_path / "models" / "whisper" / "aaa111",
                        ["model.bin", "config.json"])
    assert seen and seen[-1] == (42, 42) and all(t == 42 for _, t in seen)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
    assert models.paths(MAN, tmp_path) == p
    # 다 받은 뒤엔 다시 받지 않는다
    calls.clear()
    models.download(MAN, tmp_path, lambda d, t: None)
    assert calls == []


def test_download_resumes_by_file_and_counts_once(tmp_path, monkeypatch):
    d = tmp_path / "models" / "whisper" / "aaa111"
    stale = d / ".cache" / "huggingface" / "download" / "model.bin.1234abcd.incomplete"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"y" * 7)
    (d / "model.bin").write_bytes(b"x" * 20)  # 지난번에 다 받은 파일
    seen = []
    monkeypatch.setattr(models, "snapshot_download", fake_snapshot([]))
    models.download(MAN, tmp_path, lambda a, t: seen.append((a, t)), poll=0.01)
    assert seen[0][0] >= 20 and seen[-1] == (42, 42) and all(a <= 42 for a, _ in seen)
    assert not stale.exists()


def test_download_failure_raises_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "snapshot_download", raise_(OSError("network")))
    with pytest.raises(models.DownloadError):
        models.download(MAN, tmp_path, lambda d, t: None)
    assert models.status(MAN, tmp_path)  # 여전히 받아야 함


def test_download_size_mismatch_is_failure(tmp_path, monkeypatch):
    man = json.loads(json.dumps(MAN))
    man["models"][1]["bytes"] = 99
    monkeypatch.setattr(models, "snapshot_download", fake_snapshot([]))
    with pytest.raises(models.DownloadError):
        models.download(man, tmp_path, lambda d, t: None)
    assert [m["key"] for m in models.status(man, tmp_path)] == ["ctc"]


def test_cleanup_removes_old_revision(tmp_path):
    for rel in ["models/whisper/old000", "models/whisper/aaa111", "models/ctc/bbb222", "models/gone/x",
                "cuda/oldhash", f"cuda/{models.manifest_hash(MAN)}"]:
        (tmp_path / rel).mkdir(parents=True)
    models.cleanup(MAN, tmp_path)
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.glob("*/*/*")) + \
        sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "cuda").iterdir())
    assert left == ["models/ctc/bbb222", "models/whisper/aaa111", f"cuda/{models.manifest_hash(MAN)}"]
    assert not (tmp_path / "models" / "gone").exists()


def test_manifest_file_is_pinned():
    man = models.load_manifest()
    keys = [m["key"] for m in man["models"]]
    assert keys == ["whisper", "ctc"]
    for m in man["models"]:
        assert len(m["revision"]) == 40 and m["bytes"] > 0 and m["files"]
    assert [c["name"] for c in man["cuda_win"]] == ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
    for c in man["cuda_win"]:
        assert c["url"].startswith("https://files.pythonhosted.org/") and c["url"].endswith("win_amd64.whl")
        assert len(c["sha256"]) == 64


def test_device_choice(monkeypatch):
    monkeypatch.setattr(models.sys, "platform", "win32")
    assert models.device({"gpu": {"key": "k", "ok": True}}) == "cuda"
    assert models.device({"gpu": {"key": "k", "ok": False}}) == "cpu"
    assert models.device({"gpu": None}) == "cpu"
    monkeypatch.setattr(models.sys, "platform", "darwin")
    assert models.device({"gpu": {"key": "k", "ok": True}}) == "auto"


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


def cuda_manifest(wheels: dict) -> dict:
    man = json.loads(json.dumps(MAN))
    man["cuda_win"] = [{"name": n, "url": f"https://example.invalid/{n}.whl",
                        "sha256": hashlib.sha256(w).hexdigest(), "bytes": len(w)} for n, w in wheels.items()]
    return man


def fake_fetch(wheels: dict, calls: list):
    def fetch(url, dest: Path, on_bytes):
        calls.append(url)
        data = wheels[url.rsplit("/", 1)[1][:-4]]
        dest.write_bytes(data)
        on_bytes(len(data))
    return fetch


def test_download_cuda_extracts_only_dlls(tmp_path, monkeypatch):
    wheels = {"nvidia-cublas-cu12": make_wheel({"nvidia/cublas/bin/cublas64_12.dll": b"a" * 5,
                                               "nvidia/cublas/bin/cublasLt64_12.dll": b"b",
                                               "nvidia/cublas/include/cublas.h": b"h"}),
              "nvidia-cudnn-cu12": make_wheel({"nvidia/cudnn/bin/cudnn64_9.dll": b"c",
                                              "nvidia_cudnn_cu12-9.dist-info/METADATA": b"m"})}
    man = cuda_manifest(wheels)
    calls, seen = [], []
    monkeypatch.setattr(models, "_fetch", fake_fetch(wheels, calls))
    d = models.download_cuda(man, tmp_path, lambda a, t: seen.append((a, t)))
    assert d == tmp_path / "cuda" / models.manifest_hash(man) == models.cuda_dir(man, tmp_path)
    assert sorted(p.name for p in d.iterdir() if p.name != ".complete") == \
        ["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"]
    assert (d / ".complete").exists() and models.cuda_ready(man, tmp_path)
    total = sum(len(w) for w in wheels.values())
    assert seen[-1] == (total, total)
    assert not list(tmp_path.rglob("*.whl"))
    calls.clear()
    models.download_cuda(man, tmp_path, lambda a, t: None)
    assert calls == []


def test_download_cuda_bad_hash_fails(tmp_path, monkeypatch):
    wheels = {"nvidia-cublas-cu12": make_wheel({"x/cublas64_12.dll": b"a"})}
    man = cuda_manifest(wheels)
    man["cuda_win"][0]["sha256"] = "0" * 64
    monkeypatch.setattr(models, "_fetch", fake_fetch(wheels, []))
    with pytest.raises(models.DownloadError):
        models.download_cuda(man, tmp_path, lambda a, t: None)
    assert not models.cuda_ready(man, tmp_path)


def test_download_cuda_network_error(tmp_path, monkeypatch):
    man = cuda_manifest({"nvidia-cublas-cu12": b"zz"})
    monkeypatch.setattr(models, "_fetch", raise_(OSError("network")))
    with pytest.raises(models.DownloadError):
        models.download_cuda(man, tmp_path, lambda a, t: None)


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


def test_prepare_gpu_records_and_skips_same_key(tmp_path, monkeypatch):
    monkeypatch.setattr(models.sys, "platform", "win32")
    counts = {"probe": 0, "cuda": 0}
    monkeypatch.setattr(models, "has_nvidia", lambda: True)

    def dl(man, root, on_progress):
        counts["cuda"] += 1
        return models.cuda_dir(man, root)

    def probe(root, man=None):
        counts["probe"] += 1
        return False
    monkeypatch.setattr(models, "download_cuda", dl)
    monkeypatch.setattr(models, "probe_gpu", probe)
    settings = {"gpu": None}
    assert models.prepare_gpu(MAN, tmp_path, settings, lambda a, t: None) is True
    assert settings["gpu"] == {"key": models.manifest_hash(MAN), "ok": False}
    assert models.prepare_gpu(MAN, tmp_path, settings, lambda a, t: None) is False
    assert counts == {"probe": 1, "cuda": 1}
    assert models.device(settings) == "cpu"
    # manifest 가 바뀌면 다시 시험
    man2 = json.loads(json.dumps(MAN))
    man2["models"][0]["revision"] = "ccc333"
    monkeypatch.setattr(models, "probe_gpu", lambda root, man=None: True)
    models.prepare_gpu(man2, tmp_path, settings, lambda a, t: None)
    assert settings["gpu"] == {"key": models.manifest_hash(man2), "ok": True}
    assert models.pipeline_env(man2, tmp_path, settings) == {"YTEDIT_CUDA_DIR": str(models.cuda_dir(man2, tmp_path))}


def test_prepare_gpu_without_nvidia_or_on_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(models.sys, "platform", "win32")
    monkeypatch.setattr(models, "has_nvidia", lambda: False)
    monkeypatch.setattr(models, "download_cuda", raise_(AssertionError("받으면 안 됨")))
    settings = {"gpu": None}
    models.prepare_gpu(MAN, tmp_path, settings, lambda a, t: None)
    assert settings["gpu"] == {"key": models.manifest_hash(MAN), "ok": False}
    assert models.pipeline_env(MAN, tmp_path, settings) == {}
    monkeypatch.setattr(models.sys, "platform", "darwin")
    settings = {"gpu": None}
    assert models.prepare_gpu(MAN, tmp_path, settings, lambda a, t: None) is False
    assert settings["gpu"] is None


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
                    "compute_type": "int8_float16", "n": 16000}
    monkeypatch.setattr(faster_whisper, "WhisperModel", raise_(RuntimeError("no cudnn")))
    assert models.run_gpu_probe(tmp_path, MAN) == 1
