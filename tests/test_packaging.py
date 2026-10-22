import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fetch_ffmpeg", ROOT / "packaging" / "fetch_ffmpeg.py")
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)


def test_third_party_matches_sources():
    """SOURCES 를 바꾸고 fetch_ffmpeg 를 안 돌려 THIRD_PARTY.txt 가 옛 버전·주소로 남는 일을 막는다."""
    text = (ROOT / "packaging" / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    assert ff.third_party_block() in text
    for plat in ff.SOURCES.values():
        for a in plat["archives"]:
            assert a["url"] in text and a["sha256"] in text
    assert ff.FFMPEG_SOURCE in text


def test_update_third_party_rewrites_block(tmp_path):
    p = tmp_path / "T.txt"
    p.write_text(f"머리\n{ff.BEGIN}\n옛 내용\n{ff.END}\n꼬리\n", encoding="utf-8")
    assert ff.update_third_party(p) is True
    text = p.read_text(encoding="utf-8")
    assert text.startswith("머리\n") and text.endswith("\n꼬리\n") and "옛 내용" not in text
    assert ff.update_third_party(p) is False


def test_update_third_party_requires_markers(tmp_path):
    p = tmp_path / "T.txt"
    p.write_text("표시 없음", encoding="utf-8")
    with pytest.raises(ff.FetchError):
        ff.update_third_party(p)


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return path


def test_download_rejects_wrong_sha(tmp_path):
    src = _zip(tmp_path / "a.zip", {"ffmpeg": b"x"})
    with pytest.raises(ff.FetchError, match="sha256"):
        ff.download(src.as_uri(), "0" * 64, tmp_path / "cache")
    assert not list((tmp_path / "cache").glob("*"))


def test_download_caches_by_sha(tmp_path):
    src = _zip(tmp_path / "a.zip", {"ffmpeg": b"x"})
    sha = ff.sha256_of(src)
    got = ff.download(src.as_uri(), sha, tmp_path / "cache")
    src.unlink()  # 두 번째는 캐시에서
    assert ff.download(src.as_uri(), sha, tmp_path / "cache") == got


def test_extract_missing_member(tmp_path):
    src = _zip(tmp_path / "a.zip", {"other": b"x"})
    with pytest.raises(ff.FetchError, match="ffmpeg"):
        ff.extract(src, {"ffmpeg": "ffmpeg"}, tmp_path)


def test_verify_offline_needs_arnndn(tmp_path):
    names = ff.FILTERS + ff.SOURCES["win"]["encoders"]
    good = b"".join(n.encode() + b"\0" for n in names)
    (tmp_path / "ffmpeg.exe").write_bytes(good)
    ff.verify_offline(tmp_path, "win", "Filters:\n  arnndn  afftdn\n")
    (tmp_path / "ffmpeg.exe").write_bytes(good.replace(b"arnndn\0", b""))
    with pytest.raises(ff.FetchError, match="arnndn"):
        ff.verify_offline(tmp_path, "win", None)
