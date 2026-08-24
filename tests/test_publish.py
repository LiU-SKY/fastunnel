# -*- coding: utf-8 -*-
"""주소 게시 검증.

진짜 GitHub 에 쓰지 않는다. 로컬에 gist API 흉내를 내는 서버를 띄우고
PATCH 본문이 올바른지, 실패해도 예외가 새지 않는지 본다.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from _harness import Results, free_port, isolated_home

isolated_home("publish")

from fastunnel import config as config_mod, util  # noqa: E402
from fastunnel.providers.cloudflared import CloudflaredTunnel  # noqa: E402
from fastunnel.publish import GistPublisher, address_payload, make  # noqa: E402

GIST_ID = "abc123"


class FakeGist:
    def __init__(self, port: int, code: int = 200):
        self.port = port
        self.code = code
        self.patches = []
        self._server = None

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_PATCH(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.patches.append((self.path, dict(self.headers), body))
                payload = json.dumps({"id": GIST_ID}).encode()
                self.send_response(outer.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # noqa: A003
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def api(self):
        return "http://127.0.0.1:%d" % self.port


def _tunnel(port=25565, url="tcp://abc-def.trycloudflare.com", status="online"):
    t = CloudflaredTunnel(port, "tcp", "cloudflared")
    if url:
        t.set_url(url)
    t.status = status
    return t


def run() -> bool:
    r = Results("publish - 주소 게시")
    util.set_verbosity(quiet=True)

    # ---------------- 게시할 내용 만들기 ----------------
    payload = address_payload([_tunnel()], "cloudflared")
    r.check("온라인 표시", payload["online"] is True)
    r.check("호스트에서 스킴 제거", payload["host"] == "abc-def.trycloudflare.com", payload["host"])
    r.check("포트 포함", payload["port"] == 25565)
    r.check("터널 목록 포함", len(payload["tunnels"]) == 1)
    r.check("갱신 시각 포함", bool(payload.get("updated")))

    stopped = address_payload([_tunnel(status="stopped")], "cloudflared", "종료됨")
    r.check("종료된 터널은 online=false", stopped["online"] is False)
    r.check("종료돼도 목록은 남는다", len(stopped["tunnels"]) == 1)
    r.check("종료면 host 키 없음", "host" not in stopped)
    r.check("메모 전달", stopped["note"] == "종료됨")

    r.check("주소 없으면 빈 목록", address_payload([_tunnel(url="")])["tunnels"] == [])

    # ---------------- 설정이 없으면 꺼진다 ----------------
    r.check("설정 없으면 게시자 없음", make(config_mod.from_dict({})) is None)
    r.check("토큰만 있으면 안 됨",
            make(config_mod.from_dict({"publish_gist_token": "t"})) is None)
    r.check("id 만 있으면 안 됨",
            make(config_mod.from_dict({"publish_gist_id": GIST_ID})) is None)
    both = make(config_mod.from_dict({"publish_gist_id": GIST_ID, "publish_gist_token": "t"}))
    r.check("둘 다 있으면 켜짐", both is not None and both.enabled)

    # 토큰은 비밀값이라 마스킹되어야 한다
    masked = config_mod.from_dict({"publish_gist_token": "T" * 40}).masked()
    r.check("토큰 마스킹", "T" * 40 not in str(masked.get("publish_gist_token")),
            str(masked.get("publish_gist_token")))

    # ---------------- 실제 전송 ----------------
    with FakeGist(free_port(9800)) as fake:
        cfg = config_mod.from_dict({
            "publish_gist_id": GIST_ID, "publish_gist_token": "tok", "publish_gist_file": "mc.json",
        })
        pub = GistPublisher(cfg, api=fake.api)
        ok = pub.publish(address_payload([_tunnel()], "cloudflared"))
        r.check("게시 성공", ok is True)
        r.check("PATCH 1건", len(fake.patches) == 1)
        if fake.patches:
            path, headers, body = fake.patches[0]
            r.check("gist 경로", path == "/gists/%s" % GIST_ID, path)
            r.check("토큰 헤더", headers.get("Authorization") == "Bearer tok")
            content = json.loads(body["files"]["mc.json"]["content"])
            r.check("파일 이름", "mc.json" in body["files"])
            r.check("내용에 주소", content["host"] == "abc-def.trycloudflare.com")

        # 같은 내용은 다시 쓰지 않는다 (gist 히스토리를 더럽히지 않으려고)
        pub.publish(address_payload([_tunnel()], "cloudflared"))
        r.check("같은 내용이면 재전송 안 함", len(fake.patches) == 1, "%d건" % len(fake.patches))

        # 주소가 바뀌면 다시 쓴다
        pub.publish(address_payload([_tunnel(url="tcp://다른주소.trycloudflare.com")], "cloudflared"))
        r.check("바뀌면 다시 씀", len(fake.patches) == 2, "%d건" % len(fake.patches))

    # ---------------- 실패해도 예외가 새지 않는다 ----------------
    with FakeGist(free_port(9820), code=401) as fake401:
        pub = GistPublisher(config_mod.from_dict({
            "publish_gist_id": GIST_ID, "publish_gist_token": "bad"}), api=fake401.api)
        r.check("401 이면 False", pub.publish({"a": 1}) is False)

    dead = GistPublisher(config_mod.from_dict({
        "publish_gist_id": GIST_ID, "publish_gist_token": "t"}), api="http://127.0.0.1:1")
    r.check("서버가 없어도 예외 없음", dead.publish({"a": 1}) is False)

    # ---------------- 세션에 붙어 있는지 ----------------
    from fastunnel.runner import TunnelSession

    session = TunnelSession(config_mod.from_dict({"ports": "25565"}))
    r.check("게시자 없으면 조용히 False", session.publish_address() is False)

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
