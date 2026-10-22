"""설치 파일에 넣을 ffmpeg/ffprobe 정적 빌드를 받아 확인한다.

    python packaging/fetch_ffmpeg.py --platform mac --dest packaging/bin
    python packaging/fetch_ffmpeg.py --platform win --dest packaging/bin
    python packaging/fetch_ffmpeg.py --sources dist/sources     # GPL 대응 소스 아카이브 (릴리스에 첨부)

- URL 과 sha256 은 아래 SOURCES 에 고정한다. 해시가 다르면 실패.
- 받은 실행 파일을 직접 돌려 `-filters` 에 arnndn, `-encoders` 에 필요한 인코더가 있는지 본다 (없으면 실패).
  다른 OS 용을 받을 때(맥에서 윈도우용 등)는 돌릴 수 없으니 실행 파일 안 문자열과 (있으면) 빌드 README 로만 확인하고
  그렇다고 알린다.
- packaging/THIRD_PARTY.txt 의 ffmpeg 단락(BEGIN/END 표시 사이)을 SOURCES 로 다시 쓴다. 버전을 바꾸면
  이 파일을 고친 뒤 한 번 실행해 THIRD_PARTY.txt 도 같이 커밋한다 (tests/test_packaging.py 가 일치를 확인).

버전 올리는 법: 맥은 https://ffmpeg.martin-riedl.de/ 의 macOS arm64 release(ffmpeg.zip, ffprobe.zip 과 .sha256),
윈도우는 https://github.com/BtbN/FFmpeg-Builds/releases 의 월말 autobuild(오래 남는다; 매일 빌드는 2주쯤 뒤 지워짐)
에서 ffmpeg-n<버전>-…-win64-gpl-<가지>.zip (정적. -shared 아님; 자산 digest 가 sha256). 그 태그의 커밋에서
scripts.d/50-x264.sh·50-x265.sh 의 SCRIPT_COMMIT 과 FFmpeg 커밋(파일 이름의 g<해시>)으로 GPL_SOURCES 도 고친다.
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
DEFAULT_CACHE = HERE / ".ffmpeg-cache"   # 받은 zip (sha256 이 맞으면 다시 받지 않음). .gitignore 에 있음
BEGIN = "=== BEGIN ffmpeg (packaging/fetch_ffmpeg.py 가 쓰는 부분, 손으로 고치지 마세요) ==="
END = "=== END ffmpeg ==="

FFMPEG_VERSION = "9.0.2"
FFMPEG_SOURCE = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
_MAC_BASE = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
# BtbN FFmpeg-Builds: 월말 autobuild 태그(= 빌드한 FFmpeg-Builds 커밋), release/9.0 가지의 GPL 정적 빌드
_BTBN_TAG = "autobuild-2026-09-30-13-08"
_BTBN_COMMIT = "6c9aec5fc9a72ec3abedd1fa84db141fa18cf52b"
_WIN_FFMPEG_DESCRIBE = "n9.0.2-17-g2a571b6068"
_WIN_FFMPEG_COMMIT = "2a571b606854520cf89804d8030c8b328e621689"
_WIN_ZIP = f"ffmpeg-{_WIN_FFMPEG_DESCRIBE}-win64-gpl-9.0"
_X264_COMMIT = "0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee"
_WIN_X265_COMMIT = "020d7054dbecfd3be8e62efe33ff8a305d41856e"

SOURCES = {
    "mac": {
        "title": "macOS (Apple Silicon arm64)",
        "build": "ffmpeg 9.0.2 정적 빌드, Martin Riedl (https://ffmpeg.martin-riedl.de/)",
        "config": "--enable-gpl --enable-version3 (libx264, libx265, VideoToolbox 포함)",
        "license": "GNU GPL v3 (--enable-gpl --enable-version3)",
        "build_source": "빌드 스크립트: https://git.martin-riedl.de/ffmpeg/build-script\n"
                        f"    라이브러리 버전: {_MAC_BASE}/versions.txt",
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
        "build": f"ffmpeg {_WIN_FFMPEG_DESCRIBE} (release/9.0 가지) 정적 GPL 빌드, BtbN FFmpeg-Builds "
                 "(https://github.com/BtbN/FFmpeg-Builds)",
        "config": "--enable-gpl --enable-version3 (libx264, libx265 포함)",
        "license": "GNU GPL v3",
        "build_source": f"빌드 스크립트: https://github.com/BtbN/FFmpeg-Builds/tree/{_BTBN_COMMIT}\n"
                        "    (라이브러리별 소스 커밋은 scripts.d/*.sh 의 SCRIPT_COMMIT)",
        "archives": [
            {"url": f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{_BTBN_TAG}/{_WIN_ZIP}.zip",
             "sha256": "a0e45723c72141975f51d8666302e614711745f3102b704ca3f82c897a58d278",
             "members": {f"{_WIN_ZIP}/bin/ffmpeg.exe": "ffmpeg.exe",
                         f"{_WIN_ZIP}/bin/ffprobe.exe": "ffprobe.exe"}},
        ],
        "encoders": ["libx264", "libx265", "aac", "mov_text"],
    },
}

FILTERS = ["arnndn", "loudnorm", "afftdn", "highpass"]

# 정적 ffmpeg 빌드의 GPL 대응 소스. CI(.github/workflows/release.yml)가 받아 sha256 을 확인하고 매 릴리스에 첨부한다.
# 빌드에 든 버전은 이렇게 확인했다 (버전을 올리면 다시 확인):
# - 맥 FFmpeg: 9.0.2 릴리스 (태그 n9.0.2).
# - 맥 x264: 빌드 스크립트는 빌드 날(2026-09-20)의 master 를 받는다 → 0480cb05 (core 165, r3223).
# - 맥 x265: 빌드 스크립트 version/x265 = 4.2 → bitbucket get/4.2.tar.gz (커밋 e444744c). 그 압축본의
#   x265Version.txt 가 옛 값이라 실행 파일은 "4.0+1-6318f22" 로 표시된다. 아래 정식 릴리스 압축본과는
#   x265Version.txt 한 파일만 다르다 (자동 생성 압축본은 sha256 이 바뀔 수 있어 정식 릴리스 파일을 고정).
# - 윈도우: BtbN FFmpeg-Builds 커밋 _BTBN_COMMIT 의 addins/9.0.sh(release/9.0) + scripts.d/50-x264.sh
#   (SCRIPT_COMMIT 0480cb05, 맥과 같음) + scripts.d/50-x265.sh (Multicorewareinc/x265 020d7054; 실행 파일도
#   "4.3+49-020d7054"). FFmpeg 은 zip 이름의 g2a571b6068. 빌드 스크립트(소스를 고치는 sed 포함)도 함께 첨부한다.
#   GitHub 이 만들어 주는 커밋 압축본은 두 번 받아 sha256 이 같음을 확인했다.
GPL_SOURCES = [
    {"name": f"ffmpeg-{FFMPEG_VERSION}.tar.xz", "url": FFMPEG_SOURCE,
     "sha256": "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e",
     "what": "FFmpeg 9.0.2 (태그 n9.0.2) — 맥 빌드"},
    {"name": f"ffmpeg-{_WIN_FFMPEG_DESCRIBE}.tar.gz",
     "url": f"https://github.com/FFmpeg/FFmpeg/archive/{_WIN_FFMPEG_COMMIT}.tar.gz",
     "sha256": "f02fc0809a13f37e3d4e1f74254d07e035a5ef01a6d91e727b915b1764d97540",
     "what": f"FFmpeg {_WIN_FFMPEG_DESCRIBE} (release/9.0, 커밋 {_WIN_FFMPEG_COMMIT[:10]}) — 윈도우 빌드"},
    {"name": f"x264-{_X264_COMMIT}.tar.bz2",
     "url": f"https://code.videolan.org/videolan/x264/-/archive/{_X264_COMMIT}/x264-{_X264_COMMIT}.tar.bz2",
     "sha256": "f05c59f2e83d494c36307025dca2d3afc6b4d185f3a3453d06cc4fecd7094057",
     "what": "x264 core 165 r3223 (커밋 0480cb05) — 맥·윈도우 빌드"},
    {"name": "x265_4.2.tar.gz",
     "url": "https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.2.tar.gz",
     "sha256": "40b1ea0453e0309f0eba934e0ddf533f8f6295966679e8894e8f1c1c8d5e1210",
     "what": "x265 4.2 (태그 4.2, 커밋 e444744c) — 맥 빌드"},
    {"name": f"x265-{_WIN_X265_COMMIT}.tar.gz",
     "url": f"https://github.com/Multicorewareinc/x265/archive/{_WIN_X265_COMMIT}.tar.gz",
     "sha256": "ade2fd082d1a3d7adf89bb38eb54a5cbf6aad6c1c7ed130c89c7bf2cd626484d",
     "what": "x265 4.3+49 (커밋 020d7054) — 윈도우 빌드"},
    {"name": f"FFmpeg-Builds-{_BTBN_COMMIT}.tar.gz",
     "url": f"https://github.com/BtbN/FFmpeg-Builds/archive/{_BTBN_COMMIT}.tar.gz",
     "sha256": "81da4f7c878dc0f60e6b8d0049a112e32178e20f025d468fe4e99e77b2dfe0ec",
     "what": "BtbN FFmpeg-Builds 빌드 스크립트 (윈도우 빌드를 만든 커밋)"},
]


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


def fetch(plat: str, dest: Path, cache: Path = DEFAULT_CACHE) -> list[Path]:
    """임시 폴더에 풀어 확인까지 마친 뒤에만 dest 로 옮긴다. 실패하면 dest 엔 새로 생기는 것이 없다."""
    src = SOURCES[plat]
    with tempfile.TemporaryDirectory(prefix="ffmpeg-stage-") as tmp:
        stage = Path(tmp)
        staged, readme = [], None
        for a in src["archives"]:
            archive = download(a["url"], a["sha256"], cache)
            staged += extract(archive, a["members"], stage)
            if a.get("readme"):
                with zipfile.ZipFile(archive) as z:
                    readme = z.read(a["readme"]).decode("utf-8", "replace")
        if host_platform() == plat:
            verify_by_running(stage, plat)
        else:
            verify_offline(stage, plat, readme)
        dest.mkdir(parents=True, exist_ok=True)
        files = []
        for f in staged:
            target = dest / f.name
            shutil.move(str(f), str(target))
            files.append(target)
    return files


def fetch_sources(dest: Path, cache: Path = DEFAULT_CACHE) -> list[Path]:
    """GPL_SOURCES 를 받아(sha256 확인) dest 에 원래 이름으로 복사한다."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for s in GPL_SOURCES:
        got = download(s["url"], s["sha256"], cache)
        target = dest / s["name"]
        shutil.copyfile(got, target)
        out.append(target)
    return out


def third_party_block() -> str:
    lines = [BEGIN, "",
             f"FFmpeg {FFMPEG_VERSION} (ffmpeg, ffprobe 실행 파일)",
             "  이 프로그램은 FFmpeg 프로젝트(https://ffmpeg.org/)의 ffmpeg/ffprobe 를 별도 실행 파일로 함께 배포합니다.",
             "  이 FFmpeg 빌드는 GNU General Public License 버전 3 으로 배포됩니다. 라이선스 전문은 함께 들어 있는",
             "  LICENSE 파일(GPL-3.0)과 https://www.gnu.org/licenses/gpl-3.0.html 에 있습니다.",
             "  아래 소스 아카이브는 이 앱의 각 GitHub 릴리스(https://github.com/withbbang/video-edit/releases)에",
             "  설치 파일과 함께 첨부됩니다 (괄호 안은 원래 받은 곳):"]
    for s in GPL_SOURCES:
        lines += [f"    {s['name']} — {s['what']}",
                  f"      sha256 {s['sha256']}",
                  f"      ({s['url']})"]
    lines += ["  그 밖에 정적 빌드에 들어간 라이브러리와 버전은 아래 빌드 정보(맥: versions.txt,",
              "  윈도우: 빌드 스크립트의 scripts.d/*.sh)에 있습니다.",
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
    ap.add_argument("--platform", choices=sorted(SOURCES))
    ap.add_argument("--sources", type=Path, metavar="DIR",
                    help="ffmpeg 대신 GPL 대응 소스 아카이브(GPL_SOURCES)를 받아 DIR 에 둔다")
    ap.add_argument("--dest", type=Path, default=HERE / "bin")
    ap.add_argument("--third-party", type=Path, default=THIRD_PARTY)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="받은 zip 보관 폴더")
    a = ap.parse_args(argv)
    if a.sources is not None:
        try:
            files = fetch_sources(a.sources, a.cache)
        except (FetchError, OSError) as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1
        for f in files:
            print(f"  {f} ({f.stat().st_size / 1e6:.1f} MB)")
        return 0
    if a.platform is None:
        ap.error("--platform 또는 --sources 가 필요합니다")
    try:
        files = fetch(a.platform, a.dest, a.cache)
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
