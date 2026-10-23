import http.client
import json
import sys
from urllib.parse import urlsplit

import pytest

from app import window


def _req(base, path, method="GET", headers=None, skip_host=False):
    u = urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    if skip_host:
        conn.putrequest(method, path, skip_host=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
    else:
        conn.request(method, path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


@pytest.fixture
def served(tmp_path):
    data = bytes(range(256)) * 40   # 10240 바이트
    d = tmp_path / "jobs" / "0123456789ab"
    d.mkdir(parents=True)
    (d / "preview.mp4").write_bytes(data)
    (tmp_path / "state.json").write_text("{}")
    (tmp_path / "jobs" / "0123456789ab" / "other.mp4").write_bytes(b"x")
    return window.serve_previews(tmp_path), data


def test_preview_full(served):
    base, data = served
    assert base.startswith("http://127.0.0.1:")
    r, body = _req(base, "/0123456789ab/preview.mp4")
    assert r.status == 200
    assert body == data
    assert r.getheader("Content-Type") == "video/mp4"
    assert r.getheader("Accept-Ranges") == "bytes"


def test_preview_range(served):
    base, data = served
    r, body = _req(base, "/0123456789ab/preview.mp4", headers={"Range": "bytes=100-199"})
    assert r.status == 206
    assert body == data[100:200]
    assert r.getheader("Content-Range") == f"bytes 100-199/{len(data)}"
    r, body = _req(base, "/0123456789ab/preview.mp4", headers={"Range": "bytes=10000-"})
    assert r.status == 206 and body == data[10000:]
    r, body = _req(base, "/0123456789ab/preview.mp4", headers={"Range": "bytes=-40"})
    assert r.status == 206 and body == data[-40:]
    r, body = _req(base, "/0123456789ab/preview.mp4", headers={"Range": "bytes=99999-"})
    assert r.status == 416
    assert r.getheader("Content-Range") == f"bytes */{len(data)}"


def test_preview_head(served):
    base, data = served
    r, body = _req(base, "/0123456789ab/preview.mp4", method="HEAD")
    assert r.status == 200 and body == b""
    assert r.getheader("Content-Length") == str(len(data))


@pytest.mark.parametrize("path", [
    "/0123456789ab/other.mp4",
    "/state.json",
    "/jobs/0123456789ab/preview.mp4",
    "/0123456789AB/preview.mp4",
    "/ffffffffffff/preview.mp4",
    "/../state.json",
    "/0123456789ab/../../state.json",
    "/%2e%2e/state.json",
    "/0123456789ab/preview.mp4/../../../state.json",
])
def test_preview_other_paths_404(served, path):
    base, _ = served
    r, _ = _req(base, path)
    assert r.status == 404


@pytest.mark.parametrize("host", ["evil.example:80", "127.0.0.1", "127.0.0.1:1", "attacker.localhost", None])
def test_preview_rejects_other_hosts(served, host):
    base, _ = served
    headers = {"Host": host} if host else {}
    r, _ = _req(base, "/0123456789ab/preview.mp4", headers=headers, skip_host=True)
    assert r.status == 403


@pytest.fixture
def preview_folder(served, tmp_path):
    d = tmp_path / "jobs" / "0123456789ab" / "preview"
    d.mkdir()
    for n in ("video.mp4", "video.index.json", "audio_full.m4a", "audio_full.index.json",
              "audio_cut.0123456789abcdef.m4a", "audio_cut.0123456789abcdef.index.json",
              "audio_cut.zz.m4a", "x.mp4"):
        (d / n).write_bytes(n.encode())
    return served[0]


@pytest.mark.parametrize("name,ctype", [("video.mp4", "video/mp4"), ("audio_full.m4a", "audio/mp4"),
                                        ("audio_full.index.json", "application/json"),
                                        ("audio_cut.0123456789abcdef.m4a", "audio/mp4"),
                                        ("audio_cut.0123456789abcdef.index.json", "application/json")])
def test_preview_dir_files(preview_folder, name, ctype):
    r, body = _req(preview_folder, f"/0123456789ab/preview/{name}")
    assert r.status == 200 and body == name.encode()
    assert r.getheader("Content-Type") == ctype
    r, body = _req(preview_folder, f"/0123456789ab/preview/{name}", headers={"Range": "bytes=1-2"})
    assert r.status == 206 and body == name.encode()[1:3]


@pytest.mark.parametrize("name", ["../state.json", "audio_cut.zz.m4a", "x.mp4", "audio_cut.0123456789ABCDEF.m4a",
                                  "video.meta.json", "", "missing.mp4"])
def test_preview_dir_rejects_other_names(preview_folder, name):
    r, _ = _req(preview_folder, f"/0123456789ab/preview/{name}")
    assert r.status == 404


def test_preview_dir_missing_file_404(served):
    r, _ = _req(served[0], "/0123456789ab/preview/video.mp4")
    assert r.status == 404


def test_preview_accepts_localhost_name(served):
    base, data = served
    port = urlsplit(base).port
    r, body = _req(base, "/0123456789ab/preview.mp4", headers={"Host": f"localhost:{port}"}, skip_host=True)
    assert r.status == 200 and body == data


# 화면은 file:// (Origin null) 에서 뜨고, MediaSource 재생기는 fetch + Range 로 조각을 받는다 → CORS 필요
def test_preview_cors_for_null_origin(preview_folder):
    r, body = _req(preview_folder, "/0123456789ab/preview/video.mp4", headers={"Origin": "null", "Range": "bytes=1-2"})
    assert r.status == 206 and body == b"id"
    assert r.getheader("Access-Control-Allow-Origin") == "null"
    assert r.getheader("Access-Control-Expose-Headers") == "Content-Range, Content-Length, Accept-Ranges"
    assert r.getheader("Vary") == "Origin"
    r, _ = _req(preview_folder, "/0123456789ab/preview/missing.mp4", headers={"Origin": "null"})
    assert r.status == 404 and r.getheader("Access-Control-Allow-Origin") == "null"


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1", "*", None])
def test_preview_no_cors_for_other_origins(preview_folder, origin):
    headers = {"Range": "bytes=1-2"} | ({"Origin": origin} if origin else {})
    r, _ = _req(preview_folder, "/0123456789ab/preview/video.mp4", headers=headers)
    assert r.status == 206
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert r.getheader("Access-Control-Expose-Headers") is None


def test_preview_preflight(preview_folder):
    r, body = _req(preview_folder, "/0123456789ab/preview/video.mp4", method="OPTIONS",
                   headers={"Origin": "null", "Access-Control-Request-Method": "GET",
                            "Access-Control-Request-Headers": "range"})
    assert r.status == 204 and body == b""
    assert r.getheader("Access-Control-Allow-Origin") == "null"
    assert r.getheader("Access-Control-Allow-Methods") == "GET, HEAD"
    assert r.getheader("Access-Control-Allow-Headers") == "Range"
    assert r.getheader("Access-Control-Max-Age") == "600"
    assert r.getheader("Content-Length") == "0"


def test_preview_preflight_rejects(preview_folder):
    r, _ = _req(preview_folder, "/0123456789ab/preview/video.mp4", method="OPTIONS",
                headers={"Host": "evil.example:80", "Origin": "null"}, skip_host=True)
    assert r.status == 403
    r, _ = _req(preview_folder, "/0123456789ab/preview/../state.json", method="OPTIONS", headers={"Origin": "null"})
    assert r.status == 404
    r, _ = _req(preview_folder, "/0123456789ab/preview/video.mp4", method="OPTIONS",
                headers={"Origin": "https://evil.example"})
    assert r.status == 403 and r.getheader("Access-Control-Allow-Origin") is None


def test_parse_range():
    assert window.parse_range(None, 10) is None
    assert window.parse_range("bytes=0-4", 10) == (0, 4)
    assert window.parse_range("bytes=5-", 10) == (5, 9)
    assert window.parse_range("bytes=5-100", 10) == (5, 9)
    assert window.parse_range("bytes=-3", 10) == (7, 9)
    assert window.parse_range("bytes=-30", 10) == (0, 9)
    assert window.parse_range("bytes=10-", 10) == window._BAD_RANGE
    assert window.parse_range("bytes=4-2", 10) == window._BAD_RANGE
    assert window.parse_range("bytes=0-1,4-5", 10) is None
    assert window.parse_range("items=0-1", 10) is None
    assert window.parse_range("bytes=a-b", 10) is None


class FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, s):
        self.scripts.append(s)


def test_bridge_queues_until_loaded():
    b = window.Bridge()
    b.push("job", {"id": "x"})       # 창도 없고 화면도 아직
    w = FakeWindow()
    b.window = w
    b.push("models", {"done": 1, "total": 2})
    assert w.scripts == []
    b.loaded()
    assert len(w.scripts) == 2
    b.push("job", {"name": "강의 03 (최종).mp4"})
    assert len(w.scripts) == 3
    assert w.scripts[0].startswith('window.onAppEvent && window.onAppEvent("job", ')
    payload = w.scripts[2].split(", ", 1)[1].rstrip(")")
    assert json.loads(payload) == {"name": "강의 03 (최종).mp4"}
    assert "강" not in w.scripts[2]   # ASCII 로만 (스크립트 문자열 안전)


def test_bridge_swallows_eval_errors():
    class Broken:
        def evaluate_js(self, s):
            raise RuntimeError("closed")
    b = window.Bridge()
    b.window = Broken()
    b.loaded()
    b.push("job", {})   # 예외가 밖으로 나오지 않는다


def test_bridge_close_drops_pushes():
    b = window.Bridge()
    w = FakeWindow()
    b.window = w
    b.push("job", {})
    b.close()
    b.loaded()
    b.push("job", {})
    assert w.scripts == []


def test_dropped_paths():
    e = {"dataTransfer": {"files": [
        {"name": "a.mp4", "pywebviewFullPath": "/x/바탕 화면/a.mp4"},
        {"name": "b.mp4"},
        "junk",
    ]}}
    assert window.dropped_paths(e) == ["/x/바탕 화면/a.mp4"]
    assert window.dropped_paths({}) == []
    assert window.dropped_paths(None) == []


def test_file_types_parse():
    webview_util = pytest.importorskip("webview.util")
    for f in window.FILE_TYPES:
        webview_util.parse_file_type(f)


def test_dialogs_use_window(monkeypatch):
    calls = []

    class W:
        def create_file_dialog(self, kind, **kw):
            calls.append((kind, kw))
            return ("/a/b.mp4", "/a/c.mov") if kw.get("allow_multiple") else ("/save/here",)

    webview = pytest.importorskip("webview")
    b = window.Bridge()
    b.window = W()
    d = window.Dialogs(b)
    assert d.open_files() == ["/a/b.mp4", "/a/c.mov"]
    assert d.choose_folder() == "/save/here"
    assert calls[0][0] == webview.OPEN_DIALOG and calls[0][1]["allow_multiple"] is True
    assert calls[1][0] == webview.FOLDER_DIALOG


def test_build_wires_api(tmp_path):
    pushed = []
    store, queue, api = window.build(tmp_path, lambda e, d: pushed.append((e, d)), None, "http://h:1")
    assert api.list_jobs() == []
    assert queue.store is store
    assert store.path == tmp_path / "state.json"


def test_build_prunes_caches_of_removed_jobs(tmp_path, monkeypatch):
    """켤 때 목록에 없는 영상의 캐시 폴더를 지운다 (final-review I4)."""
    from app import paths
    from app.store import Store
    monkeypatch.setenv("YTEDIT_DATA_DIR", str(tmp_path))
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    s = Store(tmp_path / "state.json")
    job = s.add(src)
    s.save()
    keep, orphan = paths.job_dir(src), tmp_path / "jobs" / "fedcba987654"
    for d in (keep, orphan):
        (d / "work").mkdir(parents=True)
    window.build(tmp_path, lambda e, d: None, None, "http://h:1")
    assert keep.is_dir() and not orphan.exists() and keep.name == job.id


def test_ui_index_exists():
    assert window.ui_index().is_file()
    assert sys.modules["app.window"] is window
