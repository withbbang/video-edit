"""창(pywebview)과 미리보기 사본 서버.

run() 이 저장된 목록을 불러 대기열·Api 를 만들고 창을 띄운다. 화면(app/ui)은 pywebview.api 로 Api 를 부르고,
파이썬 쪽 알림은 window.onAppEvent(event, data) 로 받는다 (Bridge.push).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import api as api_mod
from . import models, paths
from .jobqueue import Queue
from .store import Store, prune_job_dirs

log = logging.getLogger(__name__)

TITLE = "youtube-edit"
SIZE = (1100, 720)
MIN_SIZE = (900, 600)
SHUTDOWN_WAIT = 3.0
FILE_TYPES = ("영상과 오디오 (" + ";".join(f"*{e}" for e in sorted(api_mod.MEDIA_EXTS)) + ")",
              "모든 파일 (*.*)")

# ---------------------------------------------------------------------------
# 미리보기 사본 서버: <data_root>/jobs/<id>/ 의 preview.mp4(예전 방식)와 preview/ 폴더의 정해진 파일만 내보낸다
# ---------------------------------------------------------------------------
_PREVIEW_PATH = re.compile(
    r"/([0-9a-f]{12})/(preview\.mp4|preview/(?:video\.mp4|video\.index\.json|audio_full\.m4a|audio_full\.index\.json"
    r"|audio_cut\.[0-9a-f]{16}\.m4a|audio_cut\.[0-9a-f]{16}\.index\.json))")
_CONTENT_TYPES = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".json": "application/json"}
_CHUNK = 1 << 16
_BAD_RANGE = "bad"


def parse_range(header: str | None, size: int):
    """Range 헤더 → None(전체) | (start, end 포함) | _BAD_RANGE(416). 여러 구간 요청은 전체로 답한다."""
    if not header:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    if not dash:
        return None
    try:
        if first.strip() == "":
            n = int(last)
            if n <= 0:
                return _BAD_RANGE
            return (max(size - n, 0), size - 1) if size else _BAD_RANGE
        start = int(first)
        end = int(last) if last.strip() else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return _BAD_RANGE
    return start, min(end, size - 1)


def _handler(root: Path):
    jobs = Path(root) / "jobs"

    class PreviewHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "youtube-edit"

        def log_message(self, fmt, *args):  # noqa: D102 — 콘솔을 어지럽히지 않는다
            log.debug("preview: " + fmt, *args)

        def do_GET(self):  # noqa: N802
            self._serve(body=True)

        def do_HEAD(self):  # noqa: N802
            self._serve(body=False)

        def _file(self) -> Path | None:
            path = urllib.parse.urlsplit(self.path).path
            m = _PREVIEW_PATH.fullmatch(path)
            if not m:
                return None
            f = jobs / m.group(1) / m.group(2)
            return f if f.is_file() else None

        def _host_ok(self) -> bool:
            """DNS 리바인딩 막기: 이 서버 주소로 온 요청만 받는다."""
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").strip().lower()
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _serve(self, body: bool) -> None:
            if not self._host_ok():
                self.send_error(403)
                return
            f = self._file()
            if f is None:
                self.send_error(404)
                return
            try:
                fh = open(f, "rb")
            except OSError:
                self.send_error(404)
                return
            with fh:
                size = os.fstat(fh.fileno()).st_size
                rng = parse_range(self.headers.get("Range"), size)
                if rng == _BAD_RANGE:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if rng is None:
                    start, end = 0, size - 1
                    self.send_response(200)
                else:
                    start, end = rng
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                length = max(end - start + 1, 0)
                self.send_header("Content-Type", _CONTENT_TYPES[f.suffix])
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not body:
                    return
                fh.seek(start)
                left = length
                try:
                    while left > 0:
                        chunk = fh.read(min(_CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True   # 재생기가 탐색하면서 끊는 건 흔하다

    return PreviewHandler


def serve_previews(data_root: Path) -> str:
    """127.0.0.1 임의 포트에 미리보기 서버를 띄우고 기본 URL 을 돌려준다 (데몬 스레드).
    미리보기 주소는 f"{base}/{job_id}/preview/<이름>" (예전 방식은 f"{base}/{job_id}/preview.mp4")."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Path(data_root)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="preview-server").start()
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# 창 ↔ 화면
# ---------------------------------------------------------------------------
class Bridge:
    """push(event, data) → window.onAppEvent(event, data). 화면이 뜨기 전 알림은 모아 뒀다가 뜨면 보낸다."""

    def __init__(self, max_pending: int = 500):
        self.window = None
        self._loaded = False
        self._closed = False
        self._pending: deque[str] = deque(maxlen=max_pending)
        self._lock = threading.Lock()

    @staticmethod
    def script(event: str, data) -> str:
        return (f"window.onAppEvent && window.onAppEvent({json.dumps(event)}, "
                f"{json.dumps(data, ensure_ascii=True)})")

    def push(self, event: str, data) -> None:
        s = self.script(event, data)
        with self._lock:
            if self._closed:
                return
            if not self._loaded or self.window is None:
                self._pending.append(s)
                return
            window = self.window
        self._eval(window, s)

    def loaded(self) -> None:
        with self._lock:
            self._loaded = True
            pending = list(self._pending)
            self._pending.clear()
            window = self.window
        for s in pending:
            self._eval(window, s)

    def close(self, *_args) -> None:
        """창이 닫히면 알림을 버린다 (닫힌 창의 evaluate_js 는 멈출 수 있다)."""
        with self._lock:
            self._closed = True
            self._pending.clear()

    @staticmethod
    def _eval(window, script: str) -> None:
        try:
            window.evaluate_js(script)
        except Exception:  # noqa: BLE001 — 창이 닫히는 중이면 알림은 버린다
            log.debug("evaluate_js 실패", exc_info=True)


class Dialogs:
    """Api 가 쓰는 파일·폴더 선택 창."""

    def __init__(self, bridge: Bridge):
        self._bridge = bridge

    def open_files(self) -> list[str] | None:
        import webview
        res = self._bridge.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True,
                                                     file_types=FILE_TYPES)
        return [str(p) for p in res] if res else None

    def choose_folder(self) -> str | None:
        import webview
        res = self._bridge.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return None
        return str(res[0] if isinstance(res, (list, tuple)) else res)


def dropped_paths(event: dict) -> list[str]:
    """pywebview drop 이벤트에서 실제 파일 경로들."""
    files = ((event or {}).get("dataTransfer") or {}).get("files") or []
    return [f["pywebviewFullPath"] for f in files
            if isinstance(f, dict) and f.get("pywebviewFullPath")]


def build(data_root: Path, push, dialogs, preview_base: str):
    """Store·Queue·Api 를 만든다 (창 없이도 쓸 수 있게 분리)."""
    data_root = Path(data_root)
    store = Store.load(data_root / "state.json")
    prune_job_dirs(store, data_root / "jobs")   # 목록에서 빠진 영상의 캐시를 지운다
    manifest = models.load_manifest()
    queue = None
    execute = api_mod.make_execute(store, manifest, data_root, lambda: queue)
    queue = Queue(store, execute, api_mod.make_on_change(push))
    api = api_mod.Api(store, queue, push, dialogs, manifest=manifest, data_root=data_root,
                      preview_base=preview_base)
    return store, queue, api


def ui_index() -> Path:
    return paths.bundle_dir() / "app" / "ui" / "index.html"


def run(debug: bool = False) -> None:
    import webview
    from webview.dom import DOMEventHandler

    logging.basicConfig(level=logging.INFO)
    data_root = paths.data_dir()
    bridge = Bridge()
    base = serve_previews(data_root)
    _store, queue, api = build(data_root, bridge.push, Dialogs(bridge), base)

    window = webview.create_window(TITLE, url=str(ui_index()), js_api=api, width=SIZE[0], height=SIZE[1],
                                   min_size=MIN_SIZE)
    bridge.window = window
    drop_ready = threading.Event()

    def on_drop(e) -> None:
        files = dropped_paths(e)
        if files:
            api.add_files(files)
            bridge.push("jobs", {})

    def on_loaded() -> None:
        bridge.loaded()
        if not drop_ready.is_set():
            drop_ready.set()
            try:
                window.dom.document.events.drop += DOMEventHandler(on_drop, prevent_default=True)
            except Exception:  # noqa: BLE001 — 끌어다 놓기가 안 돼도 파일 선택은 된다
                log.exception("끌어다 놓기 등록 실패")

    def stop() -> None:
        # 창을 닫으면 돌던 파이프라인(과 ffmpeg)을 멈추고 그 영상은 대기로 저장한다. 여러 번 불려도 된다.
        bridge.close()
        api.shutdown()
        if not queue.shutdown(SHUTDOWN_WAIT):
            log.warning("처리를 %s초 안에 멈추지 못했습니다", SHUTDOWN_WAIT)

    window.events.loaded += on_loaded
    window.events.closed += stop          # 앱 종료(⌘Q) 경로에서도 창 닫힘은 온다
    try:
        webview.start(debug=debug or bool(os.environ.get("YTEDIT_DEBUG")))
    finally:
        stop()
