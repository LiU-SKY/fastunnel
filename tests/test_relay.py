# -*- coding: utf-8 -*-
"""접속 도우미 중계기(client/relay.cs) 검증 - 윈도우 전용, 네트워크 불필요.

받는 사람 PC 와 같은 조건(윈도우 파워셸 5.1 의 Add-Type)으로 relay.cs 를 컴파일하고,
cloudflared 대신 파이썬 관문(auth.AuthGate)에 바로 붙여 본다. 두 구현의 핸드셰이크 형식이
어긋나면 여기서 잡힌다.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time

from _harness import ROOT, Results, isolated_home

HOME = isolated_home("relay")

from fastunnel import auth, util  # noqa: E402
from test_auth import EchoServer, fresh_registry  # noqa: E402

PROBE = os.path.join(ROOT, "tests", "relay_probe.ps1")


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_relay(local_port: int, gate_port: int, remote_port: int, keyfile: str, seconds: int = 20):
    proc = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PROBE,
         "-LocalPort", str(local_port), "-GatePort", str(gate_port), "-RemotePort", str(remote_port),
         "-KeyFile", keyfile, "-Seconds", str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines = {}
    deadline = time.time() + 60   # 첫 컴파일이 느리다
    while time.time() < deadline and len(lines) < 2:
        raw = proc.stdout.readline()
        if not raw:
            break
        text = raw.decode("utf-8", "replace").strip()
        if "=" in text:
            name, value = text.split("=", 1)
            lines[name] = value
    return proc, lines


def roundtrip(host: str, port: int, data: bytes) -> bytes:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect((host, port))
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        got = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        return got


def run() -> bool:
    r = Results("relay - 접속 도우미 중계기 (relay.cs)")
    util.set_verbosity(quiet=True)
    if os.name != "nt" or not shutil.which("powershell.exe"):
        r.skip("중계기 전체", "윈도우 파워셸이 필요합니다")
        return r.summary()

    # 테스트 벡터: relay.cs 의 MAC 이 명세(docs/AUTH_PROTOCOL.md)와 같은가
    vec = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         os.path.join(ROOT, "tests", "relay_vectors.ps1")],
        capture_output=True, timeout=120,
    )
    lines = [ln for ln in vec.stdout.decode("utf-8", "replace").splitlines() if ln.startswith("VECTOR ")]
    r.check("relay.cs MAC = 테스트 벡터 (%d개)" % len(lines),
            len(lines) >= 3 and all(ln.endswith(" True") for ln in lines),
            "; ".join(lines) or vec.stderr.decode("utf-8", "replace")[:200])

    fresh_registry()
    registry = auth.Registry()
    key = registry.add("relay-kim")
    good = os.path.join(HOME, "good.key")
    auth.write_client_key(good, "relay-kim", key)
    # 등록된 이름이지만 키가 다르다(키 파일을 고쳐 쓴 경우)
    auth.Registry().add("relay-lee")
    bad = os.path.join(HOME, "bad.key")
    auth.write_client_key(bad, "relay-lee", os.urandom(32))

    echo = EchoServer()
    gate = auth.AuthGate(auth.Registry(), "127.0.0.1", echo.port)
    gate.start()

    local = free_local_port()
    proc, info = start_relay(local, gate.port, echo.port, good)
    try:
        r.check("윈도우 파워셸 5.1 에서 relay.cs 컴파일", "PROBE" in info,
                proc.stderr.read().decode("utf-8", "replace")[:300] if "PROBE" not in info else "")
        r.check("올바른 키: 핸드셰이크 ok", info.get("PROBE") == "ok", info.get("PROBE", ""))
        payload = os.urandom(200000)
        r.check("127.0.0.1 로 데이터 왕복", roundtrip("127.0.0.1", local, payload) == payload)
        if info.get("IPV6") == "True":
            r.check("::1 로도 데이터 왕복", roundtrip("::1", local, b"v6") == b"v6")
        else:
            r.skip("::1 로도 데이터 왕복", "이 PC 는 IPv6 루프백을 열 수 없습니다")
        # 브라우저처럼 동시에 여러 연결
        import threading

        results = []

        def worker(i):
            data = ("conn-%d-" % i).encode() * 2000
            results.append(roundtrip("127.0.0.1", local, data) == data)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        r.check("동시 연결 12개", len(results) == 12 and all(results), "%d/12" % sum(results))

        # 회수하면 중계기를 거친 새 연결도 거부된다
        auth.Registry().revoke("relay-kim")
        time.sleep(1.5)
        try:
            got = roundtrip("127.0.0.1", local, b"after-revoke")
        except OSError:
            got = b""
        r.check("회수 뒤 중계기 경유 연결 거부", got == b"", repr(got[:20]))
    finally:
        proc.kill()

    local2 = free_local_port()
    proc2, info2 = start_relay(local2, gate.port, echo.port, bad, seconds=5)
    try:
        r.check("틀린 키: 핸드셰이크 denied", info2.get("PROBE") == "denied", info2.get("PROBE", ""))
    finally:
        proc2.kill()

    gate.stop()
    echo.close()
    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
