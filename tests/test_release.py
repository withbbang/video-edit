"""릴리스 워크플로(.github/workflows/release.yml)와 packaging/release_version.py 의 기본 약속."""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rv = _load("release_version", ROOT / "packaging" / "release_version.py")
ff = _load("fetch_ffmpeg_for_release", ROOT / "packaging" / "fetch_ffmpeg.py")


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps_text(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) + str(s.get("with", "")) for s in job["steps"])


def test_trigger_and_runners(wf):
    on = wf.get("on", wf.get(True))  # PyYAML 은 키 on 을 True 로 읽는다
    assert on == {"push": {"tags": ["v*"]}}
    assert wf["jobs"]["mac"]["runs-on"] == "macos-14"
    assert wf["jobs"]["win"]["runs-on"] == "windows-latest"
    assert set(wf["jobs"]["release"]["needs"]) == {"mac", "win", "sources"}


def test_actions_pinned_to_commit_sha(wf):
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert all(re.search(r"@[0-9a-f]{40} # v\d", l) for l in text.splitlines() if "uses:" in l)


def test_every_job_checks_tag_against_version(wf):
    for name, job in wf["jobs"].items():
        assert "packaging/release_version.py \"$GITHUB_REF_NAME\"" in _steps_text(job), name


def test_build_jobs_test_fetch_and_smoke(wf):
    for name, plat in (("mac", "mac"), ("win", "win")):
        text = _steps_text(wf["jobs"][name])
        order = [text.index(k) for k in ("pytest", f"fetch_ffmpeg.py --platform {plat}", "PyInstaller",
                                         "smoke_frozen.py")]
        assert order == sorted(order), name
    assert "download.pytorch.org/whl/cpu" in _steps_text(wf["jobs"]["win"])
    assert "make_dmg.sh" in _steps_text(wf["jobs"]["mac"])
    assert "/DAppVersion=" in _steps_text(wf["jobs"]["win"])


def test_release_uploads_expected_assets(wf):
    step = next(s for s in wf["jobs"]["release"]["steps"] if "action-gh-release" in s.get("uses", ""))
    w = step["with"]
    assert w["draft"] is True and w["fail_on_unmatched_files"] is True
    files = w["files"]
    assert "${{ steps.ver.outputs.dmg }}" in files and "${{ steps.ver.outputs.setup }}" in files
    # GPL 대응 소스는 모두 첨부 패턴에 걸려야 한다
    patterns = [Path(l.strip()).name for l in files.splitlines() if "*" in l]
    import fnmatch
    for s in ff.GPL_SOURCES:
        assert any(fnmatch.fnmatch(s["name"], p) for p in patterns), s["name"]


def test_asset_names_match_updater(monkeypatch):
    from app import updates
    names = rv.asset_names("1.2.3")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert updates._asset_name("1.2.3") == names["dmg"] == "youtube-edit-1.2.3-mac-arm64.dmg"
    monkeypatch.setattr(sys, "platform", "win32")
    assert updates._asset_name("1.2.3") == names["setup"] == "youtube-edit-1.2.3-win-x64-setup.exe"
    assert updates.LATEST_URL == "https://api.github.com/repos/withbbang/video-edit/releases/latest"


def test_check_tag():
    from app import __version__
    assert rv.app_version() == __version__
    assert rv.check_tag("v1.2.3", "1.2.3")["prerelease"] == "false"
    info = rv.check_tag("v1.2.3-rc1", "1.2.3")
    assert info["prerelease"] == "true" and info["dmg"] == "youtube-edit-1.2.3-mac-arm64.dmg"
    for bad in ("v1.2.4", "1.2.3", "v1.2", "v1.2.3-", "v1.2.3rc1"):
        with pytest.raises(ValueError):
            rv.check_tag(bad, "1.2.3")


def test_main_writes_outputs_and_notes(tmp_path, monkeypatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    from app import __version__
    assert rv.main([f"v{__version__}", "--notes-out", str(tmp_path / "n.md")]) == 0
    assert f"version={__version__}\n" in out.read_text(encoding="utf-8")
    notes = (tmp_path / "n.md").read_text(encoding="utf-8")
    assert f"youtube-edit-{__version__}-mac-arm64.dmg" in notes and "{" not in notes
    assert "그래도 열기" in notes and "추가 정보" in notes and "Apple Silicon(M1 이후) 전용" in notes
    assert rv.main(["v999.0.0"]) == 1
