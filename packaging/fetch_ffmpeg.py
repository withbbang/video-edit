"""설치 파일에 넣을 ffmpeg/ffprobe 정적 빌드를 받아 확인한다.

    python packaging/fetch_ffmpeg.py --platform mac --dest packaging/bin
    python packaging/fetch_ffmpeg.py --platform win --dest packaging/bin

- URL 과 sha256 은 아래 SOURCES 에 고정한다. 해시가 다르면 실패.
- 받은 실행 파일을 직접 돌려 `-filters` 에 arnndn, `-encoders` 에 필요한 인코더가 있는지 본다 (없으면 실패).
  다른 OS 용을 받을 때(맥에서 윈도우용 등)는 돌릴 수 없으니 실행 파일 안 문자열과 빌드 README 로만 확인하고
  그렇다고 알린다.
- packaging/THIRD_PARTY.txt 의 ffmpeg 단락(BEGIN/END 표시 사이)을 SOURCES 로 다시 쓴다. 버전을 바꾸면
  이 파일을 고친 뒤 한 번 실행해 THIRD_PARTY.txt 도 같이 커밋한다 (tests/test_packaging.py 가 일치를 확인).

버전 올리는 법: 맥은 https://ffmpeg.martin-riedl.de/ 의 macOS arm64 release(ffmpeg.zip, ffprobe.zip 과 .sha256),
윈도우는 https://github.com/GyanD/codexffmpeg/releases 의 <버전>-essentials_build.zip (릴리스 페이지의 sha256).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
THIRD_PARTY = HERE / "THIRD_PARTY.txt"
BEGIN = "=== BEGIN ffmpeg (packaging/fetch_ffmpeg.py 가 쓰는 부분, 손으로 고치지 마세요) ==="
END = "=== END ffmpeg ==="

FFMPEG_VERSION = "9.0.2"
FFMPEG_SOURCE = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
_MAC_BASE = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
_WIN_ZIP = "ffmpeg-9.0.2-essentials_build"

SOURCES = {
    "mac": {
        "title": "macOS (Apple Silicon arm64)",
        "build": "ffmpeg 9.0.2 정적 빌드, Martin Riedl (https://ffmpeg.martin-riedl.de/)",
        "config": "--enable-gpl --enable-version3 (libx264, libx265, VideoToolbox 포함)",
        "license": "GNU GPL v3 (--enable-gpl --enable-version3)",
        "build_source": "빌드 스크립트: https://git.martin-riedl.de/ffmpeg/build-script",
        "archives": [
            {"url": f"{_MAC_BASE}/ffmpeg.zip",
             "sha256": "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924",
             "members": {"ffmpeg": "ffmpeg"}},
            {"url": f"{_MAC_BASE}/ffprobe.zip",
             "sha256": "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6",
             "members": {"ffprobe": "ffprobe"}},
        ],
        "encoders": ["libx264", "libx265", "h264_videotoolbox", "hevc_videotoolbox", "aac", "mov_text"],
    },
    "win": {
        "title": "Windows (x64)",
        "build": "ffmpeg 9.0.2 essentials 정적 빌드, Gyan Doshi (https://www.gyan.dev/ffmpeg/builds/)",
        "config": "--enable-gpl --enable-version3 (libx264, libx265 포함)",
        "license": "GNU GPL v3",
        "build_source": "FFmpeg 소스 커밋: https://github.com/FFmpeg/FFmpeg/commit/946fcce07b",
        "archives": [
            {"url": f"https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/{_WIN_ZIP}.zip",
             "sha256": "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
             "members": {f"{_WIN_ZIP}/bin/ffmpeg.exe": "ffmpeg.exe",
                         f"{_WIN_ZIP}/bin/ffprobe.exe": "ffprobe.exe"},
             "readme": f"{_WIN_ZIP}/README.txt"},
        ],
        "encoders": ["libx264", "libx265", "aac", "mov_text"],
    },
}

FILTERS = ["arnndn", "loudnorm", "afftdn", "highpass"]


class FetchError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, sha256: str, cache: Path) -> Path:
    """cache/<sha256 앞 16자>-<이름> 에 받는다. 이미 있고 해시가 맞으면 다시 받지 않는다."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{sha256[:16]}-{url.rsplit('/', 1)[-1]}"
    if target.is_file() and sha256_of(target) == sha256:
        return target
    print(f"내려받는 중: {url}", flush=True)
    fd, tmp = tempfile.mkstemp(dir=cache, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "youtube-edit-packaging/1"}), timeout=60) as resp:
            shutil.copyfileobj(resp, out, 1 << 20)
        got = sha256_of(Path(tmp))
        if got != sha256:
            raise FetchError(f"sha256 가 다릅니다: {url}\n  기대 {sha256}\n  실제 {got}")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def extract(archive: Path, members: dict[str, str], dest: Path) -> list[Path]:
    out = []
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        for member, name in members.items():
            if member not in names:
                raise FetchError(f"{archive.name} 안에 {member} 가 없습니다")
            target = dest / name
            with z.open(member) as src, open(target, "wb") as f:
                shutil.copyfileobj(src, f)
            target.chmod(0o755)
            out.append(target)
    return out


def host_platform() -> str | None:
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mac"
    if sys.platform == "win32":
        return "win"
    return None


def _listed(text: str, name: str) -> bool:
    return any(name in line.split() for line in text.splitlines())


def verify_by_running(dest: Path, plat: str) -> None:
    exe = ".exe" if plat == "win" else ""
    ffmpeg, ffprobe = dest / f"ffmpeg{exe}", dest / f"ffprobe{exe}"

    def out(*args: str) -> str:
        r = subprocess.run([str(ffmpeg), "-hide_banner", *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise FetchError(f"{ffmpeg.name} {' '.join(args)} 실패 ({r.returncode}): {r.stderr.strip()[-300:]}")
        return r.stdout

    filters, encoders = out("-filters"), out("-encoders")
    missing = [f for f in FILTERS if not _listed(filters, f)]
    missing += [e for e in SOURCES[plat]["encoders"] if not _listed(encoders, e)]
    if missing:
        raise FetchError(f"받은 ffmpeg 에 없는 필터/인코더: {', '.join(missing)}")
    r = subprocess.run([str(ffprobe), "-version"], capture_output=True, text=True)
    if r.returncode != 0 or FFMPEG_VERSION not in r.stdout.splitlines()[0]:
        raise FetchError(f"ffprobe 확인 실패: {(r.stdout or r.stderr).strip()[:200]}")
    if plat == "mac" and shutil.which("otool"):
        for b in (ffmpeg, ffprobe):
            libs = subprocess.run(["otool", "-L", str(b)], capture_output=True, text=True).stdout.splitlines()[1:]
            extra = [l.strip() for l in libs if not l.strip().startswith(("/usr/lib/", "/System/"))]
            if extra:
                raise FetchError(f"{b.name} 이 시스템 밖 라이브러리를 씁니다 (정적 빌드가 아님): {extra}")
    print(f"확인: {ffmpeg.name} 실행 — 필터 {', '.join(FILTERS)} / 인코더 {', '.join(SOURCES[plat]['encoders'])} 있음")


def verify_offline(dest: Path, plat: str, readme: str | None) -> None:
    """다른 OS 용 실행 파일은 돌릴 수 없으니 실행 파일 안 문자열과 빌드 README 로만 본다."""
    exe = ".exe" if plat == "win" else ""
    data = (dest / f"ffmpeg{exe}").read_bytes()
    names = FILTERS + SOURCES[plat]["encoders"]
    missing = [n for n in names if n.encode() + b"\0" not in data]
    if readme is not None and not _listed(readme, "arnndn"):
        missing.append("arnndn (README 필터 목록)")
    if missing:
        raise FetchError(f"받은 ffmpeg 에 없는 필터/인코더: {', '.join(missing)}")
    print(f"주의: 이 컴퓨터에선 {plat} 용 ffmpeg 를 실행할 수 없어 실행 파일 안 이름과 README 로만 확인했습니다 "
          f"({', '.join(names)})")


def fetch(plat: str, dest: Path) -> list[Path]:
    src = SOURCES[plat]
    dest.mkdir(parents=True, exist_ok=True)
    cache = dest / ".cache"
    files, readme = [], None
    for a in src["archives"]:
        archive = download(a["url"], a["sha256"], cache)
        files += extract(archive, a["members"], dest)
        if a.get("readme"):
            with zipfile.ZipFile(archive) as z:
                readme = z.read(a["readme"]).decode("utf-8", "replace")
    if host_platform() == plat:
        verify_by_running(dest, plat)
    else:
        verify_offline(dest, plat, readme)
    return files


def third_party_block() -> str:
    lines = [BEGIN, "",
             f"FFmpeg {FFMPEG_VERSION} (ffmpeg, ffprobe 실행 파일)",
             "  이 프로그램은 FFmpeg 프로젝트(https://ffmpeg.org/)의 ffmpeg/ffprobe 를 별도 실행 파일로 함께 배포합니다.",
             "  이 FFmpeg 빌드는 GNU General Public License 버전 3 으로 배포됩니다. 라이선스 전문은 함께 들어 있는",
             "  LICENSE 파일(GPL-3.0)과 https://www.gnu.org/licenses/gpl-3.0.html 에 있습니다.",
             f"  FFmpeg 소스 코드: {FFMPEG_SOURCE}",
             "  (FFmpeg 저장소: https://git.ffmpeg.org/ffmpeg.git , 라이선스 안내: https://ffmpeg.org/legal.html)",
             ""]
    for plat in ("mac", "win"):
        s = SOURCES[plat]
        lines += [f"  [{s['title']}]",
                  f"    빌드: {s['build']}",
                  f"    구성: {s['config']}",
                  f"    라이선스: {s['license']}",
                  f"    {s['build_source']}"]
        for a in s["archives"]:
            lines += [f"    받은 파일: {a['url']}", f"      sha256 {a['sha256']}"]
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def update_third_party(path: Path = THIRD_PARTY) -> bool:
    """BEGIN/END 사이를 다시 쓴다. 바뀌었으면 True."""
    text = path.read_text(encoding="utf-8")
    try:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
    except ValueError:
        raise FetchError(f"{path} 에 ffmpeg 단락 표시({BEGIN!r} … {END!r})가 없습니다") from None
    new = head + third_party_block() + tail
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", choices=sorted(SOURCES), required=True)
    ap.add_argument("--dest", type=Path, default=HERE / "bin")
    ap.add_argument("--third-party", type=Path, default=THIRD_PARTY)
    a = ap.parse_args(argv)
    try:
        files = fetch(a.platform, a.dest)
        changed = update_third_party(a.third_party)
    except (FetchError, OSError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    for f in files:
        print(f"  {f} ({f.stat().st_size / 1e6:.1f} MB)")
    print(f"{a.third_party.name}: {'ffmpeg 단락을 고쳤습니다 — 커밋하세요' if changed else '이미 맞음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
