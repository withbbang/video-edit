"""모델(Whisper·정렬 모델)과 윈도우 CUDA 라이브러리 내려받기.

설치 파일엔 모델을 넣지 않는다. 앱 안의 manifest.json 이 저장소·리비전(커밋)과 파일별 크기·sha256 을 고정하고,
사용자 데이터 폴더의 models/<key>/<revision>/ 에 받아 둔다. 다 받으면 .complete 를 만든다.
파일은 <이름>.part 에 받고 끊기면 그 크기부터 HTTP Range 로 이어 받는다 (프로세스가 죽어도). 크기·sha256 이 맞아야 제자리로 옮긴다.

윈도우에서 NVIDIA GPU 가 있으면 ctranslate2 가 쓰는 cuBLAS·cuDNN DLL 을 cuda/<cuda_win 해시>/ 에 받고
별도 프로세스로 GPU 로드를 시험해 그 결과를 settings["gpu"] = {"key": <manifest 해시>, "ok": bool} 에 기록한다
(manifest 가 바뀌기 전엔 다시 시험하지 않음).

파이프라인은 import 하지 않는다.
"""
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from huggingface_hub import hf_hub_url

from .paths import ModelPaths

Progress = Callable[[int, int], None]  # (받은 바이트, 전체 바이트)

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"
COMPLETE = ".complete"
PROBE_TIMEOUT_SEC = 600
CUDA_COMPUTE_TYPE = "int8_float16"  # GPU 시험과 실제 실행(--compute-type)이 같은 설정을 써야 한다
CHUNK = 1 << 20

_urlopen = urllib.request.urlopen  # 테스트에서 바꿔 끼운다


class DownloadError(Exception):
    """내려받기 실패. 받던 .part 는 남겨 두므로 다시 부르면 이어서 받는다."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def manifest_hash(manifest: dict) -> str:
    """GPU 시험 기록의 키. manifest 가 조금이라도 바뀌면 다시 시험한다."""
    return _hash(manifest)


def cuda_hash(manifest: dict) -> str:
    """CUDA 폴더 이름. 휠 목록이 같으면 모델만 바뀐 업데이트에서 다시 받지 않는다."""
    return _hash(manifest.get("cuda_win") or [])


def _model_dir(root: Path, entry: dict) -> Path:
    return Path(root) / "models" / entry["key"] / entry["revision"]


def status(manifest: dict, root: Path) -> list[dict]:
    """아직 받아야 하는 모델 항목."""
    return [m for m in manifest["models"] if not (_model_dir(root, m) / COMPLETE).exists()]


def paths(manifest: dict, root: Path) -> ModelPaths:
    d = {m["key"]: str(_model_dir(root, m)) for m in manifest["models"]}
    return ModelPaths(whisper=d["whisper"], ctc=d["ctc"])


# ---------------------------------------------------------------------------
# 이어 받기
# ---------------------------------------------------------------------------
class _Tracker:
    """여러 파일의 진행을 하나로 묶는다. 보고하는 값은 줄어들지 않는다 (처음부터 다시 받는 파일이 있어도)."""

    def __init__(self, total: int, done: int, on_progress: Progress):
        self.total, self.done, self.cb = total, done, on_progress
        self.reported = -1

    def add(self, n: int) -> None:
        self.done += n
        self.emit()

    def emit(self, final: bool = False) -> None:
        v = self.total if final else max(self.reported, min(self.done, self.total))
        if v != self.reported:
            self.reported = v
            self.cb(v, self.total)


def _part(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def _have_bytes(dest: Path, size: int) -> int:
    """이미 받은 바이트 (제자리 파일 또는 .part)."""
    if dest.is_file():
        return dest.stat().st_size
    p = _part(dest)
    return min(p.stat().st_size, size) if p.is_file() else 0


def _fetch(url: str, part: Path, on_bytes: Callable[[int], None]) -> None:
    """part 에 이어 받는다: 이미 있는 만큼은 Range 로 건너뛴다. 서버가 Range 를 무시하고 200 을 주면
    그 파일만 처음부터 다시 쓴다 (on_bytes 로 이미 센 만큼을 한 번 빼 준다). 리다이렉트(CDN)는 따라간다."""
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        resp = _urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # 이미 다 받음 → 크기·sha256 검사로 넘어간다
            return
        raise
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if have and status != 206:
            on_bytes(-have)
            mode = "wb"
        else:
            mode = "ab" if have else "wb"
        with open(part, mode) as f:
            while chunk := resp.read(CHUNK):
                f.write(chunk)
                on_bytes(len(chunk))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _download_file(url: str, dest: Path, size: int, sha256: str, tracker: _Tracker) -> None:
    if dest.is_file() and dest.stat().st_size == size:
        return  # 검사를 통과해 옮겨 둔 파일
    part = _part(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if part.is_file() and part.stat().st_size > size:  # 망가진 조각
        tracker.add(-min(part.stat().st_size, size))
        part.unlink()
    if not part.is_file() or part.stat().st_size < size:
        _fetch(url, part, tracker.add)
    got = part.stat().st_size if part.is_file() else 0
    if got != size or _sha256(part) != sha256:
        part.unlink(missing_ok=True)
        tracker.add(-got)
        raise DownloadError(f"{dest.name}: 받은 파일이 맞지 않습니다 (크기 {got}/{size} 또는 sha256)")
    os.replace(part, dest)


def _guard(fn, *args):
    try:
        return fn(*args)
    except DownloadError:
        raise
    except (OSError, http.client.HTTPException, ValueError, zipfile.BadZipFile) as e:
        # URLError·HTTPError·소켓 타임아웃은 OSError 의 하위 클래스
        raise DownloadError(str(e)) from e


def _file_url(entry: dict, name: str) -> str:
    return hf_hub_url(entry["repo"], name, revision=entry["revision"])


def _model_files(manifest: dict, root: Path, todo: list[dict]) -> list[tuple]:
    out = []
    for m in todo:
        d = _model_dir(root, m)
        out += [(_file_url(m, f["name"]), d / f["name"], f["size"], f["sha256"]) for f in m["files"]]
    return out


def _cuda_files(manifest: dict, root: Path) -> list[tuple]:
    tmp = cuda_dir(manifest, root) / "_wheels"
    return [(w["url"], tmp / w["url"].rsplit("/", 1)[1], w["size"], w["sha256"])
            for w in manifest.get("cuda_win") or []]


def _new_tracker(files: list[tuple], on_progress: Progress) -> _Tracker:
    total = sum(f[2] for f in files)
    done = sum(_have_bytes(f[1], f[2]) for f in files)
    return _Tracker(total, done, on_progress)


def _download_models(manifest: dict, root: Path, todo: list[dict], tracker: _Tracker) -> None:
    for m in todo:
        d = _model_dir(root, m)
        for url, dest, size, sha in _model_files(manifest, root, [m]):
            _guard(_download_file, url, dest, size, sha, tracker)
        (d / COMPLETE).write_text(m["revision"], encoding="utf-8")


def download(manifest: dict, root: Path, on_progress: Progress) -> ModelPaths:
    """빠진 모델만 받는다 (윈도우 GPU 는 건드리지 않음 — 보통은 prepare 를 쓴다).
    on_progress 는 이 스레드에서 불린다. 실패하면 DownloadError (다시 부르면 .part 부터 이어 받음)."""
    todo = status(manifest, root)
    if todo:
        tracker = _new_tracker(_model_files(manifest, root, todo), on_progress)
        tracker.emit()
        _download_models(manifest, root, todo, tracker)
        tracker.emit(final=True)
    return paths(manifest, root)


def cleanup(manifest: dict, root: Path) -> None:
    """manifest 에 없는 모델 리비전·CUDA 폴더를 지운다 (지금 manifest 의 받다 만 폴더는 남김)."""
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
        keep = cuda_hash(manifest)
        for d in croot.iterdir():
            if d.is_dir() and d.name != keep:
                shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 장치 선택과 윈도우 GPU
# ---------------------------------------------------------------------------
def _is_windows() -> bool:
    return sys.platform == "win32"


def _gpu_ok(settings: dict, manifest: dict) -> bool:
    gpu = settings.get("gpu") or {}
    return bool(gpu.get("ok")) and gpu.get("key") == manifest_hash(manifest)


def device(settings: dict, manifest: dict) -> str:
    """맥은 "auto". 윈도우는 지금 manifest 로 GPU 시험에 성공했으면 "cuda", 아니면(기록이 옛 manifest 것이어도) "cpu".
    "cuda" 일 때 compute type 은 CUDA_COMPUTE_TYPE 을 쓴다."""
    if not _is_windows():
        return "auto"
    return "cuda" if _gpu_ok(settings, manifest) else "cpu"


def cuda_dir(manifest: dict, root: Path) -> Path:
    return Path(root) / "cuda" / cuda_hash(manifest)


def cuda_ready(manifest: dict, root: Path) -> bool:
    return (cuda_dir(manifest, root) / COMPLETE).exists()


def gpu_undecided(settings: dict, manifest: dict, root: Path) -> bool:
    """윈도우에서 prepare 를 다시 돌려야 하는가: 지금 manifest 로 GPU 를 아직 안 정했거나(첫 실행이 CUDA 받기·시험
    도중 끊김, 업데이트로 manifest 가 바뀜), GPU 로 정했는데 그 CUDA 폴더가 다 갖춰지지 않았을 때."""
    if not _is_windows():
        return False
    gpu = settings.get("gpu") or {}
    if gpu.get("key") != manifest_hash(manifest):
        return True
    return bool(gpu.get("ok")) and not cuda_ready(manifest, root)


def pipeline_env(manifest: dict, root: Path, settings: dict) -> dict:
    """파이프라인 하위 프로세스에 더할 환경변수 (runner.build_env(extra=...))."""
    if _is_windows() and _gpu_ok(settings, manifest):
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


def _extract_dlls(manifest: dict, root: Path) -> Path:
    out = cuda_dir(manifest, root)
    for _, wheel, _, _ in _cuda_files(manifest, root):
        with zipfile.ZipFile(wheel) as z:
            for info in z.infolist():
                name = info.filename.rsplit("/", 1)[-1]
                if info.is_dir() or not name.lower().endswith(".dll"):
                    continue
                with z.open(info) as src, open(out / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    shutil.rmtree(out / "_wheels", ignore_errors=True)
    (out / COMPLETE).write_text(cuda_hash(manifest), encoding="utf-8")
    return out


def _download_cuda(manifest: dict, root: Path, tracker: _Tracker) -> Path:
    if cuda_ready(manifest, root):
        return cuda_dir(manifest, root)
    for url, dest, size, sha in _cuda_files(manifest, root):
        _guard(_download_file, url, dest, size, sha, tracker)
    return _guard(_extract_dlls, manifest, root)


def download_cuda(manifest: dict, root: Path, on_progress: Progress) -> Path:
    """manifest 의 cuBLAS·cuDNN 휠을 받아 *.dll 만 cuda/<cuda_win 해시>/ 로 푼다. 실패하면 DownloadError."""
    if cuda_ready(manifest, root):
        return cuda_dir(manifest, root)
    tracker = _new_tracker(_cuda_files(manifest, root), on_progress)
    out = _download_cuda(manifest, root, tracker)
    tracker.emit(final=True)
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
    """`--probe-gpu` 하위 프로세스 본체 (app/__main__.py 가 부른다). 실제 실행과 같은 device·compute type. 성공하면 0."""
    manifest = manifest or load_manifest()
    _add_dll_dir(os.environ.get("YTEDIT_CUDA_DIR") or str(cuda_dir(manifest, root)))
    try:
        import numpy as np
        from faster_whisper import WhisperModel
        model = WhisperModel(paths(manifest, root).whisper, device="cuda", compute_type=CUDA_COMPUTE_TYPE)
        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="ko", vad_filter=False)
        list(segments)
    except Exception as e:  # noqa: BLE001
        print(f"GPU 시험 실패: {e}", file=sys.stderr)
        return 1
    return 0


def prepare(manifest: dict, root: Path, settings: dict, on_progress: Progress) -> ModelPaths:
    """첫 실행·업데이트 뒤 한 번에: 빠진 모델 받기 + (윈도우·NVIDIA·이 manifest 로 아직 안 정했으면) CUDA 휠 받기와
    GPU 시험 + 안 쓰는 폴더 지우기. 진행은 (done, total) 하나로 보고하고, total 엔 받을 CUDA 바이트도 들어간다.

    모델 받기 실패만 DownloadError 로 올린다 (화면의 다시 시도). CUDA 받기·GPU 시험 실패는 올리지 않고
    settings["gpu"] = {"key", "ok": False} 로 기록해 CPU 로 간다. settings 를 바꿀 수 있으니 호출한 쪽이 저장한다."""
    key = manifest_hash(manifest)
    decide_gpu = gpu_undecided(settings, manifest, root)
    nvidia = decide_gpu and has_nvidia()
    todo = status(manifest, root)
    files = _model_files(manifest, root, todo)
    if nvidia and not cuda_ready(manifest, root):
        files += _cuda_files(manifest, root)
    tracker = _new_tracker(files, on_progress)
    tracker.emit()

    _download_models(manifest, root, todo, tracker)

    if decide_gpu:
        ok = False
        if nvidia:
            try:
                _download_cuda(manifest, root, tracker)
                ok = probe_gpu(root, manifest)
            except DownloadError:
                ok = False
        settings["gpu"] = {"key": key, "ok": ok}
    tracker.emit(final=True)
    cleanup(manifest, root)
    return paths(manifest, root)
