"""릴리스 태그와 app/__init__.py 의 __version__ 을 맞춰 보고, 설치 파일 이름과 릴리스 노트를 만든다.

    python packaging/release_version.py v0.1.0                     # 맞으면 version=0.1.0 출력, 다르면 종료 코드 1
    python packaging/release_version.py v0.1.0-rc1 --notes-out dist/release-notes.md

- 태그는 `v<__version__>` 또는 시험용 `v<__version__>-<접미사>`(예: v0.1.0-rc1). 접미사가 있으면 prerelease.
  앱의 업데이트 확인(app/updates.py)은 prerelease·draft 와 `vX.Y.Z` 꼴이 아닌 태그를 무시한다.
- 설치 파일 이름은 접미사 없는 버전으로 짓는다 (app/updates.py 의 _asset_name 과 같아야 함).
- GITHUB_OUTPUT 이 있으면 version, prerelease, dmg, setup 을 거기에 쓴다 (GitHub Actions).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES_TEMPLATE = Path(__file__).resolve().parent / "release_notes.md"
_TAG = re.compile(r"v(\d+\.\d+\.\d+)(-[0-9A-Za-z][0-9A-Za-z.]*)?")


def app_version(root: Path = ROOT) -> str:
    text = (root / "app" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise ValueError("app/__init__.py 에 __version__ 이 없습니다")
    return m.group(1)


def asset_names(version: str) -> dict:
    return {"dmg": f"youtube-edit-{version}-mac-arm64.dmg",
            "setup": f"youtube-edit-{version}-win-x64-setup.exe"}


def check_tag(tag: str, version: str) -> dict:
    """태그가 버전과 맞으면 {version, prerelease, dmg, setup}. 아니면 ValueError."""
    m = _TAG.fullmatch(tag)
    if not m:
        raise ValueError(f"태그 {tag!r} 는 vX.Y.Z 또는 vX.Y.Z-접미사 꼴이 아닙니다")
    if m.group(1) != version:
        raise ValueError(f"태그 {tag} 와 app/__init__.py 의 __version__ = {version!r} 가 다릅니다")
    return {"version": version, "prerelease": "true" if m.group(2) else "false", **asset_names(version)}


def render_notes(info: dict, tag: str, template: Path = NOTES_TEMPLATE) -> str:
    text = template.read_text(encoding="utf-8")
    for k, v in {**info, "tag": tag}.items():
        text = text.replace("{" + k + "}", v)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tag")
    ap.add_argument("--notes-out", type=Path)
    a = ap.parse_args(argv)
    try:
        info = check_tag(a.tag, app_version())
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    lines = [f"{k}={v}" for k, v in info.items()]
    print("\n".join(lines))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if a.notes_out:
        a.notes_out.parent.mkdir(parents=True, exist_ok=True)
        a.notes_out.write_text(render_notes(info, a.tag), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
