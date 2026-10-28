"""앱이 쓰는 폴더 위치. 파이프라인을 import 하지 않는다."""
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "youtube-edit"


@dataclass
class ModelPaths:
    whisper: str
    ctc: str


def data_dir() -> Path:
    override = os.environ.get("YTEDIT_DATA_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_save_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("USERPROFILE") or Path.home()) / "Videos"
    else:
        base = Path.home() / "Movies"
    return base / APP_NAME


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def fonts_dir() -> Path:
    """자막 꾸미기·새기기 글꼴 (화면의 @font-face 와 파이프라인의 libass 가 같은 파일을 쓴다)."""
    return bundle_dir() / "app" / "ui" / "fonts"


def job_dir(src: Path) -> Path:
    key = os.path.normcase(os.path.abspath(src))
    return data_dir() / "jobs" / hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def ffmpeg_bin_dir() -> Path | None:
    d = bundle_dir() / "bin"
    return d if getattr(sys, "frozen", False) and d.is_dir() else None
