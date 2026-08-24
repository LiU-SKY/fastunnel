# -*- coding: utf-8 -*-
"""네트워크 없이 도는 기본 검증: 포트 파싱 · 설정 병합 · 방화벽 계획/적용/원복."""
from __future__ import annotations

import io
import json
import os
import signal
import sys

from _harness import Results, isolated_home

isolated_home("core")

from fastunnel import config as config_mod, firewall as fw, util  # noqa: E402


def run() -> bool:
    r = Results("core - 포트/설정/방화벽")
    util.set_verbosity(quiet=True)

    # ---------------- 포트 파싱 ----------------
    r.check(
        "범위·개별·구분자 혼용",
        util.parse_ports("8000-8005,9000 3000:3002")
        == [3000, 3001, 3002, 8000, 8001, 8002, 8003, 8004, 8005, 9000],
    )
    r.check("역순 범위 정규화", util.parse_ports("8005-8000") == [8000, 8001, 8002, 8003, 8004, 8005])
    r.check("연속 구간 묶기", util.format_ranges([1, 2, 3, 7, 8, 10]) == "1-3,7-8,10")

    for bad, why in (("abc", "숫자 아님"), ("70000", "범위 초과"), ("0", "0번 포트"), ("1-9999", "너무 넓음")):
        try:
            util.parse_ports(bad)
            r.check("잘못된 입력 거부: %s" % why, False, "예외가 나지 않음")
        except util.PortSpecError as exc:
            r.check("잘못된 입력 거부: %s" % why, True, str(exc)[:40])

    # ---------------- 설정 병합 ----------------
    cfg = config_mod.from_dict({"ports": "8000", "firewall": "yes", "start_delay": "2.5"})
    r.check("문자열 불리언 정규화", cfg.bool("firewall") is True)
    r.check("숫자 문자열 정규화", cfg.get("start_delay") == 2.5)
    cfg2 = config_mod.from_dict({"ready_timeout": "이상한값"})
    r.check("잘못된 숫자는 기본값으로", cfg2.get("ready_timeout") == 90)
    masked = config_mod.from_dict({"discord_bot_token": "A" * 40}).masked()
    r.check("비밀값 마스킹", "A" * 40 not in str(masked["discord_bot_token"]), str(masked["discord_bot_token"]))

    # ---------------- 방화벽: 계획 문자열 ----------------
    for backend, expect in (
        ("netsh", "localport=8000-8002"),
        ("ufw", "8000:8002/tcp"),
        ("firewalld", "8000-8002/tcp"),
        ("iptables", "--dport 8000:8002"),
    ):
        manager = fw.FirewallManager([8000, 8001, 8002], "tcp", "both", True, tag="plan")
        manager.backend = backend
        joined = " ".join(" ".join(item[1]) for item in manager.plan())
        r.check("계획(%s)" % backend, expect in joined, joined[:70])

    # iptables 는 매치 확장이 -j 앞에 와야 한다 (삭제할 때 규칙이 일치하려면 중요)
    manager = fw.FirewallManager([8000], "tcp", "in", True, tag="plan")
    manager.backend = "iptables"
    cmd = manager.plan()[0][1]
    r.check("iptables 인자 순서", cmd.index("-m") < cmd.index("-j"), " ".join(cmd))

    # ---------------- 방화벽: 적용 → 상태 저장 → 원복 ----------------
    calls = []

    def fake_run_cmd(args, timeout=60, env=None):  # noqa: ARG001
        calls.append(list(args))
        return 0, "Ok.", ""

    real_run_cmd, real_is_admin = util.run_cmd, util.is_admin
    fw.util.run_cmd = fake_run_cmd
    fw.util.is_admin = lambda: True
    try:
        manager = fw.FirewallManager([8000, 8001, 8002, 9000], "tcp", "both", True, tag="selftest")
        manager.backend = "netsh"
        r.check("규칙 적용", manager.open() is True and len(manager.applied) == 4,
                "%d개" % len(manager.applied))

        state = os.path.join(util.state_dir(), "firewall-selftest.json")
        saved = json.load(io.open(state, encoding="utf-8")) if os.path.isfile(state) else {}
        r.check("상태 파일 기록", len(saved.get("rules", [])) == 4, saved.get("ports", ""))

        calls.clear()
        manager.close()
        r.check("원복 명령 실행", len(calls) == 4 and all(c[3] == "delete" for c in calls))
        r.check("상태 파일 제거", not os.path.exists(state))

        # 비정상 종료 시나리오
        crashed = fw.FirewallManager([5000, 5001], "tcp", "in", True, tag="crashed")
        crashed.backend = "netsh"
        crashed.open()
        removed = fw.cleanup()
        r.check("남은 규칙 일괄 정리", removed >= 1 and not fw.list_leftovers(), "%d개 제거" % removed)

        # 권한이 없으면 적용하지 않고 안내만
        fw.util.is_admin = lambda: False
        fw.util.has_sudo = lambda: False
        blocked = fw.FirewallManager([7000], "tcp", "in", True, tag="noperm")
        blocked.backend = "netsh"
        r.check("권한 없으면 적용 안 함", blocked.open() is False and not blocked.applied)
    finally:
        fw.util.run_cmd = real_run_cmd
        fw.util.is_admin = real_is_admin

    # ---------------- 공개 DNS 대기 (네트워크 없이 도는 부분만) ----------------
    from fastunnel.providers.base import Tunnel
    from fastunnel.runner import TunnelSession

    t = Tunnel(8000, "tcp", "cloudflared")
    t.set_url("tcp://abc-def-ghi.trycloudflare.com:7844")
    r.check("호스트 이름 추출(tcp)", t.hostname() == "abc-def-ghi.trycloudflare.com", t.hostname())
    t2 = Tunnel(8000, "http", "cloudflared")
    t2.set_url("https://abc-def.trycloudflare.com/")
    r.check("호스트 이름 추출(https)", t2.hostname() == "abc-def.trycloudflare.com", t2.hostname())
    r.check("주소 없으면 빈 문자열", Tunnel(1, "tcp", "x").hostname() == "")

    r.check("dns_wait 기본값", config_mod.from_dict({}).get("dns_wait") == 45)
    r.check("dns_wait 문자열 정규화", config_mod.from_dict({"dns_wait": "10"}).get("dns_wait") == 10)
    r.check("dns_wait 잘못된 값은 기본값",
            config_mod.from_dict({"dns_wait": "열"}).get("dns_wait") == 45)

    # dns_wait=0 이면 네트워크를 전혀 건드리지 않고 즉시 건너뛴다
    off = TunnelSession(config_mod.from_dict({"ports": "8000", "dns_wait": 0}))
    r.check("dns_wait=0 이면 대기 안 함", off.wait_public_dns() == "skip")
    # 주소가 없으면 확인할 것도 없다
    on = TunnelSession(config_mod.from_dict({"ports": "8000", "dns_wait": 45}))
    r.check("주소 없으면 대기 안 함", on.wait_public_dns() == "skip")

    real_resolve = util.resolve_public
    try:
        util.resolve_public = lambda host, timeout=8: ("missing", [])
        r.check("등록 전이면 timeout 반환", util.wait_public_dns("x.example", 0.2, 0.05) == "timeout")
        util.resolve_public = lambda host, timeout=8: ("ok", ["1.2.3.4"])
        r.check("등록되면 즉시 ok", util.wait_public_dns("x.example", 5, 0.05) == "ok")
        util.resolve_public = lambda host, timeout=8: ("unknown", [])
        r.check("DoH 막히면 unknown", util.wait_public_dns("x.example", 0.3, 0.05) == "unknown")
    finally:
        util.resolve_public = real_resolve

    r.check("잘못된 주소 probe", util.probe_url("://나쁜주소")[0] == 0)

    # ---------------- 시그널 핸들러 ----------------
    from fastunnel import cli

    cli._STOP["flag"] = False
    cli._install_signal_handlers()
    handler = signal.getsignal(signal.SIGINT)
    if callable(handler):
        handler(signal.SIGINT, None)
        r.check("Ctrl+C 종료 플래그", cli._STOP["flag"] is True)
    else:
        r.skip("Ctrl+C 종료 플래그", "핸들러를 설치할 수 없는 환경")
    cli._STOP["flag"] = False

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
