# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 설정. 저장소 루트에서:
#   python packaging/fetch_ffmpeg.py --platform mac --dest packaging/bin    (윈도우는 --platform win)
#   pyinstaller --noconfirm packaging/youtube-edit.spec
# 결과: 맥 dist/youtube-edit.app, 윈도우 dist/youtube-edit/youtube-edit.exe (onedir)
#
# - 진입: packaging/launcher.py → app.__main__.main() (`python -m app` 과 같음). app/__main__.py 는 상대
#   import 를 써서 직접 진입 스크립트로 쓸 수 없다.
# - scripts/pipeline.py 는 소스 파일 그대로 넣는다 (파이프라인 ROOT = 번들 폴더라 models/sh.rnnn, fillers.json 을
#   ROOT 기준으로 찾는다). 그래서 PyInstaller 가 pipeline.py 의 import 를 따라가지 않으므로, 그 import 를 ast 로 읽어
#   hiddenimports 에 넣는다.
# - 모델은 넣지 않는다 (처음 실행 때 받음). torch 는 CPU 판 (윈도우 CI 는 --index-url https://download.pytorch.org/whl/cpu
#   로 설치한 환경에서 이 spec 을 그대로 쓴다).
import ast
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

sys.path.insert(0, str(ROOT))
from app import __version__  # noqa: E402

BUNDLE_ID = "com.withbbang.youtube-edit"


def pipeline_imports() -> list[str]:
    """scripts/pipeline.py 가 (함수 안까지 포함해) import 하는 모듈 이름."""
    tree = ast.parse((ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    names.discard("__future__")
    return sorted(names)


def transformers_modules() -> list[str]:
    """transformers 는 lazy import 라 쓰는 모델(wav2vec2)과 auto 만 넣고 나머지 모델 수백 개는 뺀다."""
    keep = ("transformers.models.wav2vec2", "transformers.models.auto")
    return collect_submodules(
        "transformers",
        filter=lambda n: not n.startswith("transformers.models.") or n.startswith(keep),
    )


# 실행 파일만 넣는다 (다른 OS 용을 같은 폴더에 받아 두었어도 섞이지 않게)
ffmpeg_names = ["ffmpeg.exe", "ffprobe.exe"] if IS_WIN else ["ffmpeg", "ffprobe"]
ffmpeg_bins = [(str(ROOT / "packaging" / "bin" / n), "bin") for n in ffmpeg_names]
missing = [src for src, _ in ffmpeg_bins if not Path(src).is_file()]
if missing:
    raise SystemExit(f"ffmpeg 가 없습니다: {missing}\n  먼저 python packaging/fetch_ffmpeg.py --platform "
                     f"{'win' if IS_WIN else 'mac'} --dest packaging/bin")

datas = [
    (str(ROOT / "app" / "ui"), "app/ui"),
    (str(ROOT / "app" / "manifest.json"), "app"),
    (str(ROOT / "fillers.json"), "."),
    (str(ROOT / "models" / "sh.rnnn"), "models"),
    (str(ROOT / "scripts" / "pipeline.py"), "scripts"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "packaging" / "THIRD_PARTY.txt"), "."),
]
datas += collect_data_files("faster_whisper")          # silero VAD 모델(onnx)
# pipeline.probe_whisper_windows 가 importlib.metadata.version("ctranslate2") 를 읽는다. transformers 는 버전 확인용
for dist in ("ctranslate2", "faster-whisper", "tokenizers", "huggingface-hub", "safetensors", "transformers",
             "torch", "numpy", "tqdm", "regex", "requests", "packaging", "filelock", "pyyaml"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # noqa: BLE001 — 설치 안 된 선택 패키지
        pass

hiddenimports = pipeline_imports() + transformers_modules() + collect_submodules("faster_whisper")

# 쓰지 않는 무거운 모듈. torch/transformers 가 선택적으로만 부르는 것들이다.
excludes = [
    "tkinter", "_tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "_pytest",
    "tensorboard", "torch.utils.tensorboard", "tensorflow", "flax", "jax", "keras",
    "torchvision", "torchaudio", "pandas", "scipy", "sklearn", "PIL.ImageQt", "PyQt5", "PyQt6",
    "PySide2", "PySide6", "gi",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=ffmpeg_bins,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="youtube-edit",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64" if IS_MAC else None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="youtube-edit")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="youtube-edit.app",
        bundle_identifier=BUNDLE_ID,
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "youtube-edit",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "12.0",
            "LSArchitecturePriority": ["arm64"],
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "GPL-3.0 — https://github.com/withbbang/video-edit",
        },
    )
