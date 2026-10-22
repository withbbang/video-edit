"""GitHub 릴리스로 새 버전을 확인하고 설치 파일을 받는다 (stdlib 만 사용)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__

LATEST_URL = "https://api.github.com/repos/withbbang/video-edit/releases/latest"
TIMEOUT = 10
_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _headers() -> dict:
    return {"Accept": "application/vnd.github+json",
            "User-Agent": f"youtube-edit/{__version__}"}


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def newer(candidate: str, current: str) -> bool:
    return _parse(candidate) > _parse(current)


def _asset_name(ver: str) -> str | None:
    if sys.platform == "darwin":
        return f"youtube-edit-{ver}-mac-arm64.dmg"
    if sys.platform == "win32":
        return f"youtube-edit-{ver}-win-x64-setup.exe"
    return None


def check(current: str, fetch=_fetch_json) -> dict | None:
    try:
        rel = fetch(LATEST_URL)
        if rel.get("draft") or rel.get("prerelease"):
            return None
        m = _TAG.fullmatch(rel["tag_name"])
        if not m:
            return None
        ver = ".".join(m.groups())
        if not newer(ver, current):
            return None
        want = _asset_name(ver)
        for a in rel["assets"]:
            if a["name"] == want:
                return {"version": ver, "notes": rel.get("body") or "",
                        "url": a["browser_download_url"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


def download(url: str, dest_dir: Path) -> Path:
    dest_dir = Path(dest_dir)
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    if not name:
        raise OSError(f"파일 이름을 알 수 없는 주소: {url}")
    dest = dest_dir / name
    part = dest_dir / (name + ".part")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.iterdir():   # 예전에 받은 설치 파일(과 받다 만 .part)은 지운다
            if old.is_file():
                try:
                    old.unlink()
                except OSError:
                    pass
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open", str(path)])
