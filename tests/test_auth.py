# -*- coding: utf-8 -*-
"""접속 키 인증 검증 (네트워크 불필요).

실제 cloudflared 없이 관문(AuthGate)에 직접 붙어 본다. 관문 입장에서는 cloudflared 도
127.0.0.1 에서 오는 평범한 TCP 연결이라 결과가 같다.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

from _harness import Results, isolated_home

HOME = isolated_home("auth")

from fastunnel import auth, cli, config as config_mod, util  # noqa: E402
from fastunnel.providers.cloudflared import CloudflaredProvider, CloudflaredTunnel  # noqa: E402
from fastunnel.publish import address_payload  # noqa: E402
from fastunnel.runner import SessionError, TunnelSession  # noqa: E402


class EchoServer:
    """받은 것을 그대로 돌려주는 대상 서비스. 상대가 쓰기를 닫으면 남은 것을 보내고 닫는다."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(data)

    def close(self):
        self.sock.close()


def connect(gate) -> socket.socket:
    return socket.create_connection(("127.0.0.1", gate.port), timeout=5)


def closed_within(sock: socket.socket, seconds: float) -> bool:
    """상대가 seconds 안에 연결을 끊으면 True."""
    sock.settimeout(0.2)
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if sock.recv(4096) == b"":
                return True
        except socket.timeout:
            continue
        except OSError:
            return True
    return False


def fresh_registry() -> auth.Registry:
    path = auth.clients_path()
    if os.path.exists(path):
        os.remove(path)
    return auth.Registry()


def run() -> bool:
    r = Results("auth - 접속 키 인증")
    util.set_verbosity(quiet=True)
    auth.FAIL_DELAY = 0.05
    auth.WATCH_INTERVAL = 0.2

    # ---------------- 등록부 ----------------
    reg = fresh_registry()
    key_kim = reg.add("kim")
    r.check("키 생성: 32바이트", len(key_kim) == 32)
    r.check("등록부에 저장", "kim" in auth.Registry().active_ids())
    try:
        reg.add("kim")
        r.check("같은 이름 중복 등록 거부", False)
    except auth.AuthError:
        r.check("같은 이름 중복 등록 거부", True)
    for bad in ("Kim", "a b", "", "x" * 33, "../evil"):
        try:
            reg.add(bad)
            r.check("잘못된 이름 거부: %r" % bad, False)
        except auth.AuthError:
            r.check("잘못된 이름 거부: %r" % bad, True)
    stored = json.load(open(auth.clients_path(), encoding="utf-8"))
    r.check("등록부 형식", stored.get("version") == 1 and "key" in stored["clients"]["kim"])
    if not util.IS_WINDOWS:
        mode = os.stat(auth.clients_path()).st_mode & 0o777
        r.check("등록부 권한 600", mode == 0o600, oct(mode))
    else:
        r.skip("등록부 권한 600", "윈도우는 사용자 프로필 ACL 로 보호")

    key_lee = reg.add("lee")
    keyfile = os.path.join(HOME, "client.key")
    auth.write_client_key(keyfile, "lee", key_lee)
    r.check("키 파일 왕복", auth.read_client_key(keyfile) == ("lee", key_lee))

    # ---------------- 관문 + 대상 ----------------
    echo = EchoServer()
    gate = auth.AuthGate(auth.Registry(), "127.0.0.1", echo.port)
    gate.start()
    # 관문은 '원격 포트' 번호로 서명을 묶는다. 여기서는 대상 포트가 곧 원격 포트다.
    port = echo.port

    sock = connect(gate)
    try:
        auth.client_handshake(sock, "kim", key_kim, port)
        sock.sendall(b"hello")
        got = sock.recv(100)
        r.check("올바른 키: 핸드셰이크 후 데이터 왕복", got == b"hello", repr(got))
    except auth.AuthError as exc:
        r.check("올바른 키: 핸드셰이크 후 데이터 왕복", False, str(exc))
    finally:
        sock.close()

    # 쓰기를 닫아도(half-close) 돌아올 데이터는 끝까지 받는다
    sock = connect(gate)
    auth.client_handshake(sock, "kim", key_kim, port)
    payload = os.urandom(300000)
    sock.sendall(payload)
    sock.shutdown(socket.SHUT_WR)
    received = b""
    sock.settimeout(5)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        received += chunk
    sock.close()
    r.check("큰 데이터 + 쓰기 닫기 후에도 전부 돌아옴", received == payload, "%d바이트" % len(received))

    def denied(client_id, key, port_value=port) -> bool:
        s = connect(gate)
        try:
            auth.client_handshake(s, client_id, key, port_value)
            return False
        except auth.AuthError:
            return True
        finally:
            s.close()

    before = echo.connections
    r.check("틀린 키 거부", denied("kim", os.urandom(32)))
    r.check("없는 이름 거부", denied("nobody", key_kim))
    r.check("다른 사람 키로 사칭 거부", denied("kim", key_lee))
    r.check("다른 포트용 서명 거부", denied("kim", key_kim, port + 1))
    r.check("거부된 연결은 대상에 닿지 않음", echo.connections == before)

    # 엿들은 핸드셰이크 재전송: 첫 연결의 서버 nonce 로 만든 메시지를 두 번째 연결에 보낸다
    s1 = connect(gate)
    hello1 = auth.recv_exact(s1, 36)
    cn = os.urandom(32)
    message = bytes([3]) + b"kim" + cn + auth.client_mac(key_kim, port, hello1[4:], cn, "kim")
    s2 = connect(gate)
    auth.recv_exact(s2, 36)
    s2.sendall(message)
    s2.settimeout(3)
    status = auth.recv_exact(s2, 1)
    r.check("재전송 공격 거부", status == auth.DENIED)
    s1.close()
    s2.close()

    # 인증 없이 바로 앱 데이터를 보내는 경우(맨 cloudflared 로 붙은 사람)
    s = connect(gate)
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    r.check("핸드셰이크 없이 보낸 데이터 거부", closed_within(s, 3))
    s.close()

    # 아무 말도 안 하는 연결은 제한 시간 뒤 끊긴다
    old = auth.HANDSHAKE_TIMEOUT
    auth.HANDSHAKE_TIMEOUT = 0.5
    s = connect(gate)
    r.check("말 없는 연결은 제한 시간 뒤 끊김", closed_within(s, 3))
    s.close()
    auth.HANDSHAKE_TIMEOUT = old

    # ---------------- 회수 ----------------
    live = connect(gate)
    auth.client_handshake(live, "kim", key_kim, port)
    live.sendall(b"ping")
    live.settimeout(3)
    r.check("회수 전 연결 동작", live.recv(10) == b"ping")
    time.sleep(0.05)  # mtime 해상도가 낮은 파일 시스템 대비
    r.check("revoke 성공", auth.Registry().revoke("kim"))
    r.check("열린 연결이 회수 후 끊김", closed_within(live, 3))
    live.close()
    r.check("회수 후 새 연결 거부", denied("kim", key_kim))
    r.check("다른 사람은 그대로 접속", not denied("lee", key_lee))
    r.check("회수 기록이 남음", any(row[0] == "kim" and row[1] == "회수됨" for row in auth.Registry().rows()))
    r.check("두 번 revoke 는 False", auth.Registry().revoke("kim") is False)

    # 등록부가 망가지면 모두 거부한다
    with open(auth.clients_path(), "w", encoding="utf-8") as fh:
        fh.write("{ 망가진 파일")
    time.sleep(0.5)
    r.check("망가진 등록부: 모두 거부", denied("lee", key_lee))

    gate.stop()
    echo.close()

    # ---------------- 세션·게시·제공자 연결 ----------------
    def session_error(data) -> str:
        try:
            TunnelSession(config_mod.from_dict(data))
            return ""
        except SessionError as exc:
            return str(exc)

    r.check("--auth + http 거부", "TCP" in session_error({"ports": "8000", "proto": "http", "auth": True}))
    r.check("--auth + ngrok 거부",
            "cloudflared" in session_error({"ports": "8000", "provider": "ngrok", "auth": True}))
    r.check("--auth + tcp + cloudflared 허용",
            session_error({"ports": "8000", "proto": "tcp", "auth": True}) == "")

    fresh_registry()
    session = TunnelSession(config_mod.from_dict({"ports": "8000", "auth": True, "firewall": False}))
    session.provider = CloudflaredProvider("cloudflared", [8000], "tcp", "localhost", {})
    session.firewall = None
    try:
        session._start_gates()
        r.check("키가 하나도 없으면 터널을 열지 않음", False)
    except SessionError as exc:
        r.check("키가 하나도 없으면 터널을 열지 않음", "접속 키" in str(exc))

    auth.Registry().add("park")
    session._start_gates()
    gate_port = session.provider.options["gates"][8000]
    args = session.provider._args_for(8000)
    r.check("cloudflared 가 관문을 가리킴", "tcp://127.0.0.1:%d" % gate_port in args, " ".join(args[-3:]))
    tunnel = session.provider.tunnels[0]
    r.check("터널에 인증 표시", tunnel.auth is True)
    tunnel.set_url("https://abc-def.trycloudflare.com")
    r.check("안내 문구가 접속 도우미를 가리킴", "접속 도우미" in tunnel.client_hint())
    payload = address_payload([tunnel], "cloudflared", exact_ports=True)
    r.check("게시 내용에 auth·exact_ports",
            payload["auth"] is True and payload["exact_ports"] is True and payload["tunnels"][0]["auth"] is True)
    r.check("게시 내용에 키 없음", "key" not in json.dumps(payload))
    session._stop_gates()
    session.provider.stop()

    plain = CloudflaredTunnel(9000, "tcp", "cloudflared")
    plain.set_url("https://ghi.trycloudflare.com")
    r.check("인증 없는 터널은 예전 안내 그대로", "cloudflared access tcp" in plain.client_hint())
    r.check("인증 없는 게시 내용", address_payload([plain])["auth"] is False)

    # ---------------- CLI ----------------
    out = os.path.join(tempfile.gettempdir(), "fastunnel-test-auth-bundle")
    shutil.rmtree(out, ignore_errors=True)
    code = cli.main(["client", "add", "choi", "--out", out])
    r.check("client add 종료 코드", code == 0)
    r.check("폴더에 키 파일", os.path.isfile(os.path.join(out, auth.KEY_FILENAME)))
    for name in ("connect.cmd", "connect.ps1", "relay.cs", "server.txt"):
        r.check("폴더에 %s" % name, os.path.isfile(os.path.join(out, name)))
    with open(os.path.join(out, "connect.ps1"), "rb") as fh:
        r.check("connect.ps1 BOM 유지", fh.read(3) == b"\xef\xbb\xbf")
    r.check("키 파일이 등록부와 일치",
            auth.read_client_key(os.path.join(out, auth.KEY_FILENAME))[1] == auth.Registry().key_for("choi"))
    r.check("client revoke", cli.main(["client", "revoke", "choi"]) == 0)
    r.check("없는 이름 revoke 는 실패", cli.main(["client", "revoke", "ghost"]) == 1)
    r.check("client list", cli.main(["client", "list"]) == 0)
    shutil.rmtree(out, ignore_errors=True)

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
