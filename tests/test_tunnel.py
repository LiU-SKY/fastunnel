# -*- coding: utf-8 -*-
"""실제 터널 검증 (네트워크 필요).

cloudflared quick tunnel 을 진짜로 열고, 발급된 공개 주소로 접속해서
로컬 서버 응답이 돌아오는지까지 확인한다.

주소 확인에 util.probe_url 을 쓰는 이유: 리졸버가 이미 NXDOMAIN 을 캐시해 버린 망에서도
데이터 경로 자체는 검증할 수 있어야 하기 때문이다(DoH 로 IP 를 얻어 SNI 직결로 우회).
"""
from __future__ import annotations

import sys

from _harness import Results, free_port, isolated_home, local_server

isolated_home("tunnel")

from fastunnel import binaries, config as config_mod, util  # noqa: E402
from fastunnel.runner import SessionError, TunnelSession  # noqa: E402


def run() -> bool:
    r = Results("tunnel - 실제 터널 (네트워크)")
    util.set_verbosity(quiet=True)

    if not binaries.locate("cloudflared"):
        try:
            binaries.ensure("cloudflared", "auto")
        except Exception as exc:  # noqa: BLE001
            r.skip("cloudflared 준비", str(exc)[:60])
            return r.summary()

    port = free_port(8850)

    # ---------------- HTTP 터널: 공개 URL 로 실제 접속 ----------------
    with local_server(port):
        cfg = config_mod.from_dict({
            "ports": str(port), "proto": "http", "firewall": False,
            "notify": False, "update": "skip",
        })
        session = TunnelSession(cfg)
        try:
            session.prepare()
            session.start()
            ready = session.wait_ready(90)
            r.check("HTTP 터널 주소 확보", ready and session.online_count() == 1,
                    session.tunnels[0].address() if session.tunnels else "")
            if session.online_count():
                tunnel = session.tunnels[0]
                code, how = util.probe_url(tunnel.public_url)
                r.check("공개 URL 로 로컬 서버 응답", code == 200, "HTTP %s (%s)" % (code, how))
                # wait_ready 가 DNS 등록까지 기다렸으므로 이름 조회가 성공해야 한다.
                # (등록 전에 조회하면 NXDOMAIN 이 캐시되어 몇 분간 접속이 막힌다)
                host = tunnel.hostname()
                r.check("이름 조회 가능 (NXDOMAIN 캐시 없음)", util.local_resolves(host), host)
        except SessionError as exc:
            r.check("HTTP 터널 주소 확보", False, str(exc)[:70])
        finally:
            session.stop(notify=False)
        r.check("정리 후 프로세스 종료", all(t.status == "stopped" for t in session.tunnels))

    # ---------------- 포트 범위: 여러 개 동시 ----------------
    p1, p2 = free_port(8870), free_port(8880)
    ports = "%d,%d" % (p1, p2)
    cfg = config_mod.from_dict({
        "ports": ports, "proto": "tcp", "firewall": False,
        "notify": False, "update": "skip", "start_delay": 0.5,
    })
    session = TunnelSession(cfg)
    try:
        session.prepare()
        session.start()
        session.wait_ready(90)
        r.check("포트 2개 동시 터널", session.online_count() == 2,
                "%d/2 연결" % session.online_count())
        hints = [t.client_hint() for t in session.tunnels if t.client_hint()]
        r.check("TCP 접속 명령 안내", len(hints) == 2 and "cloudflared access tcp" in hints[0])
        state = session.save_state()
        r.check("상태 파일 저장", bool(state))
    except SessionError as exc:
        r.check("포트 2개 동시 터널", False, str(exc)[:70])
    finally:
        session.stop(notify=False)

    # ---------------- 오류 경로 ----------------
    try:
        TunnelSession(config_mod.from_dict({"ports": "abc"}))
        r.check("잘못된 포트는 세션 생성 실패", False)
    except SessionError as exc:
        r.check("잘못된 포트는 세션 생성 실패", True, str(exc)[:40])

    bad = TunnelSession(config_mod.from_dict({
        "ports": "9999", "bin_path": "/없는경로/없는파일", "update": "skip",
    }))
    try:
        bad.prepare()
        r.check("없는 실행 파일 경로 오류", False)
    except SessionError as exc:
        r.check("없는 실행 파일 경로 오류", bool(exc.hint), str(exc)[:50])

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
