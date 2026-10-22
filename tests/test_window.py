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


def test_preview_accepts_localhost_name(served):
    base, data = served
    port = urlsplit(base).port
    r, body = _req(base, "/0123456789ab/preview.mp4", headers={"Host": f"localhost:{port}"}, skip_host=True)
    assert r.status == 200 and body == data


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


def test_ui_index_exists():
    assert window.ui_index().is_file()
    assert sys.modules["app.window"] is window
