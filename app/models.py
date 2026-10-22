"""모델(Whisper·정렬 모델)과 윈도우 CUDA 라이브러리 내려받기.

설치 파일엔 모델을 넣지 않는다. 앱 안의 manifest.json 이 저장소·리비전(커밋)을 고정하고,
사용자 데이터 폴더의 models/<key>/<revision>/ 에 받아 둔다. 다 받으면 .complete 를 만든다.
윈도우에서 NVIDIA GPU 가 있으면 ctranslate2 가 쓰는 cuBLAS·cuDNN DLL 을 cuda/<manifest 해시>/ 에 받고
별도 프로세스로 GPU 로드를 시험해 그 결과를 설정에 기록한다 (manifest 가 바뀌기 전엔 다시 시험하지 않음).

파이프라인은 import 하지 않는다.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import http.client
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from huggingface_hub import snapshot_download
from huggingface_hub.utils import tqdm as _hf_tqdm

from .paths import ModelPaths

Progress = Callable[[int, int], None]  # (받은 바이트, 전체 바이트)

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"
COMPLETE = ".complete"
PROBE_TIMEOUT_SEC = 600


_WRITTEN_BAR = "huggingface_hub.snapshot_download"            # 디스크에 쓴 바이트
_TRANSFER_BAR = "huggingface_hub.snapshot_download.transfer"    # 네트워크로 받은 바이트


class _Counter:
    def __init__(self):
        self.n = {_WRITTEN_BAR: 0, _TRANSFER_BAR: 0}
        self._lock = threading.Lock()

    def add(self, name: str, n: int) -> None:
        with self._lock:
            self.n[name] += n

    def done(self) -> int:
        # xet 는 받은 조각을 모았다가 파일에 쓰므로 쓴 바이트가 한참 늦다. 둘 중 큰 쪽을 진행으로 본다.
        return max(self.n.values())


def _progress_tqdm(counter: _Counter) -> type:
    """snapshot_download 의 tqdm_class. 막대는 그리지 않고(창 모드 앱은 stderr 가 None 일 수 있음)
    snapshot_download 가 만드는 전체 바이트 막대 두 개(쓴 바이트·받은 바이트)의 증가분만 센다."""

    class _Bar(_hf_tqdm):
        def __init__(self, *args, **kwargs):
            self._count = kwargs.get("name") if kwargs.get("name") in counter.n else None
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if self._count and n:
                counter.add(self._count, int(n))
            return super().update(n)

    return _Bar


class DownloadError(Exception):
    """내려받기 실패. 받던 것은 남겨 두므로 다시 부르면 이어서 받는다."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def manifest_hash(manifest: dict) -> str:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _model_dir(root: Path, entry: dict) -> Path:
    return Path(root) / "models" / entry["key"] / entry["revision"]


def status(manifest: dict, root: Path) -> list[dict]:
    """아직 받아야 하는 모델 항목."""
    return [m for m in manifest["models"] if not (_model_dir(root, m) / COMPLETE).exists()]


def paths(manifest: dict, root: Path) -> ModelPaths:
    d = {m["key"]: str(_model_dir(root, m)) for m in manifest["models"]}
    return ModelPaths(whisper=d["whisper"], ctc=d["ctc"])


def _present_bytes(d: Path, files: list[str]) -> int:
    return sum((d / f).stat().st_size for f in files if (d / f).is_file())


def download(manifest: dict, root: Path, on_progress: Progress, poll: float = 0.5) -> ModelPaths:
    """빠진 모델을 받는다. on_progress 는 부른 스레드에서 불린다. 실패하면 DownloadError (다시 부르면 이어 받음).

    이어 받기는 파일 단위다: 다 받은 파일은 건너뛰지만, huggingface_hub 2.x 는 받던 파일을 실행마다
    새 임시 파일(<...>.<무작위>.incomplete)에 처음부터 받는다. 남은 임시 파일은 받기 전에 지운다."""
    todo = status(manifest, root)
    total = sum(m["bytes"] for m in todo)
    dirs = [_model_dir(root, m) for m in todo]
    pre = sum(_present_bytes(d, m["files"]) for m, d in zip(todo, dirs))  # 이미 받은 파일 (건너뜀)
    counter = _Counter()
    tqdm_class = _progress_tqdm(counter)
    errors: list[BaseException] = []

    def work():
        try:
            for m, d in zip(todo, dirs):
                d.mkdir(parents=True, exist_ok=True)
                for tmp in (d / ".cache" / "huggingface" / "download").glob("**/*.incomplete"):
                    tmp.unlink(missing_ok=True)
                snapshot_download(m["repo"], revision=m["revision"], local_dir=d, allow_patterns=m["files"],
                                  tqdm_class=tqdm_class)
                got = _present_bytes(d, m["files"])
                if got != m["bytes"]:
                    raise DownloadError(f"{m['repo']}: 받은 크기 {got} != {m['bytes']}")
                (d / COMPLETE).write_text(m["revision"], encoding="utf-8")
        except BaseException as e:  # noqa: BLE001 — 스레드 밖으로 넘겨 DownloadError 로 바꾼다
            errors.append(e)

    if todo:
        t = threading.Thread(target=work, name="model-download", daemon=True)
        t.start()
        last = -1
        while True:
            t.join(poll)
            alive = t.is_alive()
            done = max(last, min(total, pre + counter.done()))
            if not alive and not errors:
                done = total
            if done != last:
                on_progress(done, total)
                last = done
            if not alive:
                break
        if errors:
            e = errors[0]
            raise (e if isinstance(e, DownloadError) else DownloadError(str(e))) from e
    return paths(manifest, root)


def cleanup(manifest: dict, root: Path) -> None:
    """manifest 에 없는 모델 리비전·CUDA 폴더를 지운다."""
    want = {(m["key"], m["revision"]) for m in manifest["models"]}
    keys = {k for k, _ in want}
    mroot = Path(root) / "models"
    if mroot.is_dir():
        for kd in mroot.iterdir():
            if not kd.is_dir():
                continue
            if kd.name not in keys:
                shutil.rmtree(kd, ignore_errors=True)
                continue
            for rd in kd.iterdir():
                if rd.is_dir() and (kd.name, rd.name) not in want:
                    shutil.rmtree(rd, ignore_errors=True)
    croot = Path(root) / "cuda"
    if croot.is_dir():
        keep = manifest_hash(manifest)
        for d in croot.iterdir():
            if d.is_dir() and d.name != keep:
                shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 장치 선택과 윈도우 GPU
# ---------------------------------------------------------------------------
def _is_windows() -> bool:
    return sys.platform == "win32"


def device(settings: dict) -> str:
    """맥은 "auto", 윈도우는 GPU 시험에 성공했으면 "cuda", 아니면 "cpu"."""
    if not _is_windows():
        return "auto"
    gpu = settings.get("gpu") or {}
    return "cuda" if gpu.get("ok") else "cpu"


def cuda_dir(manifest: dict, root: Path) -> Path:
    return Path(root) / "cuda" / manifest_hash(manifest)


def cuda_ready(manifest: dict, root: Path) -> bool:
    return (cuda_dir(manifest, root) / COMPLETE).exists()


def pipeline_env(manifest: dict, root: Path, settings: dict) -> dict:
    """파이프라인 하위 프로세스에 더할 환경변수 (runner.build_env(extra=...))."""
    gpu = settings.get("gpu") or {}
    if _is_windows() and gpu.get("ok") and gpu.get("key") == manifest_hash(manifest):
        return {"YTEDIT_CUDA_DIR": str(cuda_dir(manifest, root))}
    return {}


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def has_nvidia() -> bool:
    if not _is_windows():
        return False
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=15, creationflags=_no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _fetch(url: str, dest: Path, on_bytes: Callable[[int], None]) -> None:
    """dest 에 이어 받는다 (이미 받은 만큼은 Range 로 건너뜀)."""
    have = dest.stat().st_size if dest.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # 이미 다 받음
            return
        raise
    with resp:
        mode = "ab" if have and resp.status == 206 else "wb"
        if mode == "wb" and have:
            on_bytes(-have)
        with open(dest, mode) as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
                on_bytes(len(chunk))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download_cuda(manifest: dict, root: Path, on_progress: Progress) -> Path:
    """manifest 의 cuBLAS·cuDNN 휠을 받아 *.dll 만 cuda/<manifest 해시>/ 로 푼다."""
    out = cuda_dir(manifest, root)
    if (out / COMPLETE).exists():
        return out
    wheels = manifest.get("cuda_win") or []
    total = sum(w.get("bytes", 0) for w in wheels)
    tmp = out / "_wheels"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        done = 0
        for w in wheels:
            dest = tmp / w["url"].rsplit("/", 1)[1]
            if dest.exists():
                done += dest.stat().st_size

            def on_bytes(n):
                nonlocal done
                done += n
                on_progress(min(done, total), total)
            if not dest.exists() or dest.stat().st_size != w.get("bytes"):
                _fetch(w["url"], dest, on_bytes)
            if _sha256(dest) != w["sha256"]:
                dest.unlink()
                raise DownloadError(f"{w['name']}: sha256 불일치")
            with zipfile.ZipFile(dest) as z:
                for info in z.infolist():
                    name = info.filename.rsplit("/", 1)[-1]
                    if info.is_dir() or not name.lower().endswith(".dll"):
                        continue
                    with z.open(info) as src, open(out / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        shutil.rmtree(tmp, ignore_errors=True)
        (out / COMPLETE).write_text(manifest_hash(manifest), encoding="utf-8")
    except DownloadError:
        raise
    except (OSError, http.client.HTTPException, zipfile.BadZipFile) as e:
        raise DownloadError(str(e)) from e
    on_progress(total, total)
    return out


def probe_gpu(root: Path, manifest: dict | None = None) -> bool:
    """별도 프로세스(`--probe-gpu`)에서 GPU 로 Whisper 로드 + 1초 무음 전사. 종료 코드 0 이면 True.
    죽어도(cuDNN 로드 실패 등) 이 프로세스는 멀쩡하다."""
    manifest = manifest or load_manifest()
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--probe-gpu"]
    else:
        cmd = [sys.executable, "-m", "app", "--probe-gpu"]
    env = dict(os.environ)
    env["YTEDIT_DATA_DIR"] = str(root)
    env["YTEDIT_CUDA_DIR"] = str(cuda_dir(manifest, root))
    env["HF_HUB_OFFLINE"] = "1"
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, timeout=PROBE_TIMEOUT_SEC,
                           creationflags=_no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _add_dll_dir(d: str) -> None:
    if os.name == "nt" and d and os.path.isdir(d):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def run_gpu_probe(root: Path, manifest: dict | None = None) -> int:
    """`--probe-gpu` 하위 프로세스 본체 (app/__main__.py 가 부른다). 성공하면 0."""
    manifest = manifest or load_manifest()
    _add_dll_dir(os.environ.get("YTEDIT_CUDA_DIR") or str(cuda_dir(manifest, root)))
    try:
        import numpy as np
        from faster_whisper import WhisperModel
        model = WhisperModel(paths(manifest, root).whisper, device="cuda", compute_type="int8_float16")
        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="ko", vad_filter=False)
        list(segments)
    except Exception as e:  # noqa: BLE001
        print(f"GPU 시험 실패: {e}", file=sys.stderr)
        return 1
    return 0


def prepare_gpu(manifest: dict, root: Path, settings: dict, on_progress: Progress) -> bool:
    """윈도우에서 GPU 를 쓸 수 있는지 정해 settings["gpu"] = {"key", "ok"} 에 기록한다.
    모델을 받은 뒤 부른다. 기록을 바꿨으면 True (호출한 쪽이 저장). 같은 manifest 로 이미 정했으면 다시 하지 않는다.
    CUDA 휠 내려받기 실패는 DownloadError 로 올린다 (기록하지 않으므로 다시 시도하면 이어 받음)."""
    if not _is_windows():
        return False
    key = manifest_hash(manifest)
    if (settings.get("gpu") or {}).get("key") == key:
        return False
    ok = False
    if has_nvidia():
        download_cuda(manifest, root, on_progress)
        ok = probe_gpu(root, manifest)
    settings["gpu"] = {"key": key, "ok": ok}
    return True
