import io
import sys
from pathlib import Path

import pytest

from app import updates


def REL(tag, *, mac=True, win=True, draft=False, pre=False, body="notes"):
    v = tag.lstrip("v")
    assets = []
    if mac:
        n = f"youtube-edit-{v}-mac-arm64.dmg"
        assets.append({"name": n, "browser_download_url": f"https://x/{n}"})
    if win:
        n = f"youtube-edit-{v}-win-x64-setup.exe"
        assets.append({"name": n, "browser_download_url": f"https://x/{n}"})
    return {"tag_name": tag, "body": body, "draft": draft,
            "prerelease": pre, "assets": assets}


def raising(exc):
    def f(url):
        raise exc
    return f


@pytest.fixture(autouse=True)
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def test_newer():
    r = updates.check("0.1.0", fetch=lambda u: REL("v0.2.0"))
    assert r == {"version": "0.2.0", "notes": "notes",
                 "url": "https://x/youtube-edit-0.2.0-mac-arm64.dmg"}


def test_same_or_older_none():
    assert updates.check("0.2.0", fetch=lambda u: REL("v0.2.0")) is None
    assert updates.check("0.3.0", fetch=lambda u: REL("v0.2.0")) is None


@pytest.mark.parametrize("exc", [OSError(), ValueError(), TimeoutError()])
def test_check_offline_returns_none(exc):
    assert updates.check("0.1.0", fetch=raising(exc)) is None


def test_bad_shapes_none():
    for bad in [None, [], {}, {"tag_name": 3}, {"tag_name": "v1.0.0", "assets": "x"}]:
        assert updates.check("0.1.0", fetch=lambda u, b=bad: b) is None


def test_picks_platform_asset(monkeypatch):
    assert updates.check("0.1.0", fetch=lambda u: REL("v0.2.0"))["url"].endswith("mac-arm64.dmg")
    monkeypatch.setattr(sys, "platform", "win32")
    assert updates.check("0.1.0", fetch=lambda u: REL("v0.2.0"))["url"].endswith("win-x64-setup.exe")


def test_no_asset_for_platform_none():
    assert updates.check("0.1.0", fetch=lambda u: REL("v0.2.0", mac=False)) is None


def test_draft_prerelease_and_odd_tags_ignored():
    assert updates.check("0.1.0", fetch=lambda u: REL("v0.2.0", draft=True)) is None
    assert updates.check("0.1.0", fetch=lambda u: REL("v0.2.0", pre=True)) is None
    for tag in ["v0.2.0-rc1", "0.2.0", "v0.2", "v1.2.3.4"]:
        assert updates.check("0.1.0", fetch=lambda u, t=tag: REL(t)) is None


def test_version_compare():
    assert updates.newer("0.10.0", "0.9.9")
    assert not updates.newer("0.9.9", "0.10.0")
    assert not updates.newer("1.0.0", "1.0.0")


def test_fetch_json_headers(monkeypatch):
    seen = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return R(b'{"a": 1}')

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake)
    assert updates._fetch_json("https://u") == {"a": 1}
    h = {k.lower(): v for k, v in seen["req"].header_items()}
    assert h["accept"] == "application/vnd.github+json"
    assert h["user-agent"].startswith("youtube-edit/")
    assert seen["timeout"] == 10


def test_download_part_then_rename(monkeypatch, tmp_path):
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(updates.urllib.request, "urlopen",
                        lambda req, timeout=None: R(b"abc" * 1000))
    p = updates.download("https://x/dir/app.dmg", tmp_path / "sub")
    assert p == tmp_path / "sub" / "app.dmg"
    assert p.read_bytes() == b"abc" * 1000
    assert not list((tmp_path / "sub").glob("*.part"))


def test_download_failure_oserror(monkeypatch, tmp_path):
    def boom(req, timeout=None):
        raise OSError("net")
    monkeypatch.setattr(updates.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        updates.download("https://x/app.dmg", tmp_path)
    assert not list(tmp_path.glob("*.part"))


def test_open_file(monkeypatch):
    calls = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda a: calls.append(a))
    updates.open_file(Path("/t/a.dmg"))
    assert calls == [["open", "/t/a.dmg"]]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(updates.os, "startfile", lambda p: calls.append(("sf", p)), raising=False)
    updates.open_file(Path("C:/a.exe"))
    assert calls[-1][0] == "sf"


def test_download_removes_older_installers(monkeypatch, tmp_path):
    """새 설치 파일을 받기 전에 예전에 받은 것(과 받다 만 .part)을 지운다 (final-review M4)."""
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    d = tmp_path / "updates"
    d.mkdir()
    (d / "youtube-edit-0.9.0-mac-arm64.dmg").write_bytes(b"old")
    (d / "youtube-edit-0.9.5-mac-arm64.dmg.part").write_bytes(b"half")
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda req, timeout=None: R(b"new"))
    p = updates.download("https://x/youtube-edit-1.0.0-mac-arm64.dmg", d)
    assert sorted(x.name for x in d.iterdir()) == [p.name] and p.read_bytes() == b"new"
