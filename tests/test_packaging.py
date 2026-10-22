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


def _fake_source(monkeypatch, tmp_path, payload: bytes):
    src = _zip(tmp_path / "f.zip", {"bin/ffmpeg.exe": payload, "bin/ffprobe.exe": b"probe"})
    monkeypatch.setitem(ff.SOURCES, "win", dict(ff.SOURCES["win"], archives=[
        {"url": src.as_uri(), "sha256": ff.sha256_of(src),
         "members": {"bin/ffmpeg.exe": "ffmpeg.exe", "bin/ffprobe.exe": "ffprobe.exe"}}]))
    monkeypatch.setattr(ff, "host_platform", lambda: None)  # 실행하지 않고 문자열로 확인


def test_fetch_failure_leaves_dest_untouched(monkeypatch, tmp_path):
    _fake_source(monkeypatch, tmp_path, b"no filters here")
    dest = tmp_path / "bin"
    dest.mkdir()
    (dest / "old.txt").write_text("x")
    with pytest.raises(ff.FetchError):
        ff.fetch("win", dest, tmp_path / "cache")
    assert [p.name for p in dest.iterdir()] == ["old.txt"]


def test_fetch_success_moves_into_dest(monkeypatch, tmp_path):
    names = ff.FILTERS + ff.SOURCES["win"]["encoders"]
    _fake_source(monkeypatch, tmp_path, b"".join(n.encode() + b"\0" for n in names))
    dest = tmp_path / "new" / "bin"
    files = ff.fetch("win", dest, tmp_path / "cache")
    assert sorted(p.name for p in dest.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert all(f.parent == dest for f in files)


def test_third_party_lists_gpl_sources():
    text = (ROOT / "packaging" / "THIRD_PARTY.txt").read_text(encoding="utf-8")
    for s in ff.GPL_SOURCES:
        assert s["name"] in text and s["sha256"] in text and s["url"] in text
    assert "요청" not in text.split(ff.BEGIN)[1].split(ff.END)[0]  # "요청하면 드림" 이 아니라 실제 첨부


def test_gpl_sources_cover_both_builds():
    """윈도우 BtbN zip 이름의 FFmpeg 커밋과 GPL_SOURCES 의 FFmpeg 압축본이 같은 빌드를 가리킨다."""
    names = [s["name"] for s in ff.GPL_SOURCES]
    assert len(set(names)) == len(names)
    win_zip = ff.SOURCES["win"]["archives"][0]["url"]
    assert "-win64-gpl-" in win_zip and "shared" not in win_zip and "/latest/" not in win_zip
    assert f"ffmpeg-{ff._WIN_FFMPEG_DESCRIBE}.tar.gz" in names
    assert ff._WIN_FFMPEG_COMMIT.startswith(ff._WIN_FFMPEG_DESCRIBE.rsplit("-g", 1)[1])
    assert f"x265-{ff._WIN_X265_COMMIT}.tar.gz" in names and f"FFmpeg-Builds-{ff._BTBN_COMMIT}.tar.gz" in names


def test_fetch_sources_copies_with_release_names(tmp_path, monkeypatch):
    calls = []

    def fake_download(url, sha256, cache):
        calls.append((url, sha256))
        p = cache / f"{sha256[:16]}-x"
        cache.mkdir(parents=True, exist_ok=True)
        p.write_bytes(url.encode())
        return p

    monkeypatch.setattr(ff, "download", fake_download)
    files = ff.fetch_sources(tmp_path / "out", tmp_path / "cache")
    assert [f.name for f in files] == [s["name"] for s in ff.GPL_SOURCES]
    assert calls == [(s["url"], s["sha256"]) for s in ff.GPL_SOURCES]
    assert files[0].read_bytes() == ff.GPL_SOURCES[0]["url"].encode()


def test_app_pins_ctranslate2_to_cuda_manifest():
    """앱 빌드는 manifest 의 CUDA 휠(cuBLAS 12 · cuDNN 9)과 맞춘 ctranslate2 로 묶는다. CLI 는 묶지 않는다 (final-review M10)."""
    import re
    app_req = (ROOT / "requirements-app.txt").read_text(encoding="utf-8")
    cli_req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^ctranslate2==4\.8\.3$", app_req, re.M)
    assert "ctranslate2" not in cli_req
    import json
    man = json.loads((ROOT / "app" / "manifest.json").read_text(encoding="utf-8"))
    assert {w["name"] for w in man["cuda_win"]} == {"nvidia-cublas-cu12", "nvidia-cudnn-cu12"}
    assert next(w for w in man["cuda_win"] if w["name"] == "nvidia-cudnn-cu12")["version"].startswith("9.")
