# -*- coding: utf-8 -*-
"""테스트 공용 도구 (외부 의존성 없음).

pytest 를 쓰지 않는다. 이 프로젝트는 '표준 라이브러리만'이 원칙이라
테스트도 `python tests/run_all.py` 한 줄로 돌아가야 한다.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ----------------------------------------------------------------------
# 결과 수집
# ----------------------------------------------------------------------


class Results:
    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failed: list = []
        self.skipped: list = []
        print("\n=== %s ===" % title)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("  [PASS] %s%s" % (name, (" - " + detail) if detail else ""))
        else:
            self.failed.append(name)
            print("  [FAIL] %s%s" % (name, (" - " + detail) if detail else ""))
        return bool(condition)

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(name)
        print("  [SKIP] %s - %s" % (name, reason))

    def summary(self) -> bool:
        print("  -> 성공 %d · 실패 %d · 건너뜀 %d" % (self.passed, len(self.failed), len(self.skipped)))
        return not self.failed


# ----------------------------------------------------------------------
# 포트 / 로컬 서버
# ----------------------------------------------------------------------


def free_port(start: int = 8800) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("빈 포트를 찾지 못했습니다.")


@contextlib.contextmanager
def local_server(port: int):
    """테스트용 로컬 HTTP 서버를 잠깐 띄운다."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.2)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


class _HookHandler(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        type(self).received.append({"path": self.path, "body": body})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):  # noqa: A003
        pass


@contextlib.contextmanager
def webhook_receiver(port: int):
    """알림 전송을 받아 보는 로컬 수신기. 받은 요청 목록을 돌려준다."""

    handler = type("Handler", (_HookHandler,), {"received": []})
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler.received
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------------
# 기타
# ----------------------------------------------------------------------


def isolated_home(tmp_name: str) -> str:
    """테스트가 사용자의 ~/.fastunnel 을 건드리지 않도록 별도 홈을 쓴다."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fastunnel-test-%s" % tmp_name)
    os.makedirs(path, exist_ok=True)
    os.environ["FASTUNNEL_HOME"] = path
    return path


def has_display() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def has_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False
