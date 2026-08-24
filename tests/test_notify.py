# -*- coding: utf-8 -*-
"""알림 전송 검증: 로컬 수신 서버로 실제 HTTP 요청을 보내 페이로드를 확인한다.

디스코드에 진짜로 보내지 않는다. 웹훅 URL 자리에 로컬 주소를 넣어
우리가 만드는 요청 본문이 올바른지만 본다.
"""
from __future__ import annotations

import sys
import time

from _harness import Results, free_port, isolated_home, webhook_receiver

isolated_home("notify")

from fastunnel import config as config_mod, util  # noqa: E402
from fastunnel.notify import Notifier  # noqa: E402
from fastunnel.providers.cloudflared import CloudflaredTunnel  # noqa: E402


def run() -> bool:
    r = Results("notify - 알림 페이로드")
    util.set_verbosity(quiet=True)

    port = free_port(9600)
    base = "http://127.0.0.1:%d" % port

    tunnel = CloudflaredTunnel(8000, "tcp", "cloudflared")
    tunnel.set_url("https://example-abc.trycloudflare.com")

    with webhook_receiver(port) as received:
        cfg = config_mod.from_dict({
            "notify": True,
            "discord_webhook": base + "/discord",
            "webhooks": [{"type": "raw", "url": base + "/raw"},
                         {"type": "slack", "url": base + "/slack"}],
        })
        notifier = Notifier(cfg, "cloudflared", "tcp")
        r.check("채널 인식", len(notifier.channels()) == 3, ", ".join(notifier.channels()))

        notifier.send_started([tunnel])
        notifier.send_stopped([tunnel])
        deadline = time.time() + 5
        while time.time() < deadline and len(received) < 6:
            time.sleep(0.1)

        r.check("시작/종료 알림 전송", len(received) == 6, "%d건 수신" % len(received))

        discord = [x for x in received if x["path"].startswith("/discord")]
        if discord:
            embed = discord[0]["body"]["embeds"][0]
            field = embed["fields"][0]
            r.check("디스코드 임베드 구조", "title" in embed and "color" in embed and field["name"] == "TCP 8000")
            r.check("주소 포함", "example-abc.trycloudflare.com" in field["value"])
            r.check("접속 명령 포함", "cloudflared access tcp" in field["value"])
        else:
            r.check("디스코드 임베드 구조", False, "요청이 오지 않음")

        raw = [x for x in received if x["path"] == "/raw"]
        if raw:
            body = raw[0]["body"]
            r.check("범용 웹훅 JSON", body["tunnels"][0]["port"] == 8000 and "event" in body)
        else:
            r.check("범용 웹훅 JSON", False, "요청이 오지 않음")

        slack = [x for x in received if x["path"] == "/slack"]
        r.check("슬랙 텍스트", bool(slack) and "TCP 8000" in slack[0]["body"]["text"])

    # 자리표시자가 남은 URL 은 실제 전송 대상에서 제외돼야 한다
    placeholder = Notifier(config_mod.from_dict({
        "notify": True,
        "discord_webhook": "https://discord.com/api/webhooks/<ID>/<TOKEN>",
    }))
    r.check("자리표시자 URL 차단", not placeholder.configured())

    # 알림을 꺼 두면 채널이 있어도 전송하지 않는다
    disabled = Notifier(config_mod.from_dict({"notify": False, "discord_webhook": base + "/x"}))
    r.check("알림 끄기 존중", not disabled.configured())

    # 전송이 실패해도 예외를 밖으로 던지지 않아야 한다 (터널은 계속 돌아야 하므로)
    dead = Notifier(config_mod.from_dict({
        "notify": True, "discord_webhook": "http://127.0.0.1:1/none",
    }))
    try:
        dead.send_started([tunnel])
        r.check("전송 실패해도 예외 없음", True)
    except Exception as exc:  # noqa: BLE001
        r.check("전송 실패해도 예외 없음", False, str(exc))

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
