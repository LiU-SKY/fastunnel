# -*- coding: utf-8 -*-
"""슬래시 명령 검증: WebSocket 프레임 코덱 + 인터랙션 처리.

게이트웨이에 실제로 붙지는 않는다. 직접 만든 WebSocket 프레임 코덱이 규격대로
동작하는지, 인터랙션 페이로드에서 명령·인자·권한을 제대로 뽑는지를 본다.
"""
from __future__ import annotations

import struct
import sys

from _harness import Results, isolated_home

isolated_home("slash")

from fastunnel import config as config_mod, util, ws as ws_mod  # noqa: E402
from fastunnel.control import ControlActions, DiscordControl  # noqa: E402
from fastunnel.slash import ADMINISTRATOR, COMMANDS, LEGACY_NAMES, SlashCommands  # noqa: E402


class FakeSock:
    """실제 소켓 대신 바이트만 주고받는다."""

    def __init__(self, incoming: bytes = b""):
        self.out = b""
        self.inp = incoming
        self.pos = 0

    def sendall(self, data):
        self.out += data

    def recv(self, count):
        chunk = self.inp[self.pos:self.pos + count]
        self.pos += len(chunk)
        return chunk

    def settimeout(self, _timeout):
        pass

    def close(self):
        pass


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """서버가 보내는 프레임(마스킹 없음)."""
    b0 = (0x80 if fin else 0) | opcode
    size = len(payload)
    if size < 126:
        head = struct.pack("!BB", b0, size)
    elif size < 65536:
        head = struct.pack("!BBH", b0, 126, size)
    else:
        head = struct.pack("!BBQ", b0, 127, size)
    return head + payload


class FakeActions(ControlActions):
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append("status")
        return "터널 1개 정상"

    def addresses(self):
        self.calls.append("addresses")
        return "`example.trycloudflare.com`"

    def restart(self):
        self.calls.append("restart")
        return "다시 엽니다"

    def open(self, ports, proto=""):
        self.calls.append("open:%s/%s" % (ports, proto))
        return "포트 %s 로 다시 엽니다" % ports

    def shutdown(self):
        self.calls.append("shutdown")
        return "종료합니다"


def interaction(sub: str, ports: str = "", user: str = "u1", perms: str = "0", proto: str = ""):
    data = {"name": "ft", "options": [{"name": sub, "type": 1}]}
    opts = []
    if proto:
        opts.append({"name": "proto", "type": 3, "value": proto})
    if ports:
        opts.append({"name": "port", "type": 3, "value": ports})
    if opts:
        data["options"][0]["options"] = opts
    return {
        "id": "int-1",
        "token": "int-token",
        "type": 2,
        "data": data,
        "member": {"permissions": perms, "user": {"id": user}},
    }


def run() -> bool:
    r = Results("slash - 슬래시 명령")
    util.set_verbosity(quiet=True)

    # ---------------- WebSocket 프레임 코덱 ----------------
    for size, label in ((5, "짧은"), (200, "중간(2바이트 길이)"), (70000, "긴(8바이트 길이)")):
        sock = FakeSock()
        sender = ws_mod.WebSocket(sock, "h")
        text = "가" * size
        sender.send_text(text)
        back = ws_mod.WebSocket(FakeSock(sock.out), "h")
        opcode, fin, payload = back._read_frame()
        r.check("보내기 왕복: %s" % label,
                opcode == ws_mod.OP_TEXT and fin and payload.decode("utf-8") == text)

    sock = FakeSock()
    ws_mod.WebSocket(sock, "h").send_text("x")
    r.check("클라이언트 프레임은 마스킹된다", bool(sock.out[1] & 0x80))

    reader = ws_mod.WebSocket(FakeSock(server_frame(1, "안녕".encode("utf-8"))), "h")
    r.check("텍스트 수신", reader.recv_text(1) == "안녕")

    frames = server_frame(1, b"AB", fin=False) + server_frame(0, b"CD", fin=True)
    r.check("조각난 프레임 합치기", ws_mod.WebSocket(FakeSock(frames), "h").recv_text(1) == "ABCD")

    sock = FakeSock(server_frame(9, b"hi") + server_frame(1, b"ok"))
    reader = ws_mod.WebSocket(sock, "h")
    got = reader.recv_text(1)
    r.check("ping 은 삼키고 pong 을 보낸다", got == "ok" and len(sock.out) > 0)

    closed = ws_mod.WebSocket(FakeSock(server_frame(8, b"")), "h")
    try:
        closed.recv_text(1)
        r.check("close 프레임이면 예외", False)
    except ws_mod.WebSocketError:
        r.check("close 프레임이면 예외", True)

    # ---------------- 명령 정의 ----------------
    top = COMMANDS[0]
    subs = [o["name"] for o in top["options"]]
    r.check("명령 이름", top["name"] == "ft", top["name"])
    open_sub = [o for o in top["options"] if o["name"] == "open"][0]
    names = [o["name"] for o in open_sub["options"]]
    r.check("open 은 proto/port 를 받는다", names == ["proto", "port"], ",".join(names))
    choices = [c["value"] for c in open_sub["options"][0].get("choices", [])]
    r.check("프로토콜 선택지", choices == ["tcp", "http"], ",".join(choices))
    r.check("하위 명령 6개", sorted(subs) == ["help", "open", "restart", "status", "stop", "url"],
            ",".join(sorted(subs)))
    r.check("open 인자는 모두 필수",
            all(o["required"] for o in open_sub["options"]))

    # ---------------- 인터랙션 처리 ----------------
    actions = FakeActions()
    cfg = config_mod.from_dict({
        "discord_bot_token": "t", "discord_channel_id": "1", "discord_slash": True,
    })
    control = DiscordControl(cfg, actions, api="http://127.0.0.1:1")
    slash = SlashCommands(control)

    sent = []
    real_http = util.http_json

    def fake_http(url, method="GET", payload=None, headers=None, timeout=15):
        sent.append((url, payload))
        return 204, None

    util.http_json = fake_http
    try:
        name, args, proto = slash._read_options(
            interaction("open", "8000-8005", proto="tcp")["data"])
        r.check("하위 명령 해석", name == "open" and args == "8000-8005" and proto == "tcp",
                "%s %s %s" % (name, args, proto))
        r.check("인자 없는 명령",
                slash._read_options(interaction("status")["data"]) == ("status", "", ""))
        r.check("옵션이 아예 없으면 도움말",
                slash._read_options({"name": "ft"}) == ("help", "", ""))

        # 권한: 관리자 비트가 있으면 통과
        r.check("관리자 비트면 허용",
                slash.allowed({"permissions": str(ADMINISTRATOR)}, "u1") is True)
        r.check("권한 비트가 없으면 거부", slash.allowed({"permissions": "1024"}, "u1") is False)
        r.check("목록에 있으면 허용",
                SlashCommands(DiscordControl(
                    config_mod.from_dict({"discord_bot_token": "t", "discord_channel_id": "1",
                                          "discord_admins": ["u9"]}),
                    actions, api="http://127.0.0.1:1")).allowed({"permissions": "0"}, "u9") is True)

        # 조회 명령은 누구나
        del sent[:]
        slash.on_interaction(interaction("status", perms="0"))
        r.check("조회는 권한 없이도 실행", "status" in actions.calls)
        r.check("응답을 보냈다", len(sent) == 1 and "/callback" in sent[0][0])
        body = sent[0][1]["data"]
        r.check("조회 응답은 본인에게만", body.get("flags") == 64)
        r.check("응답 내용", body.get("content") == "터널 1개 정상")

        # 권한 없는 사람의 열기 명령은 거부
        del sent[:]
        before = list(actions.calls)
        slash.on_interaction(interaction("open", "8000", perms="0"))
        r.check("권한 없으면 열기 거부", actions.calls == before)
        r.check("거부 안내를 보냈다", "권한이 없습니다" in sent[0][1]["data"]["content"])

        # 관리자면 실행된다. 답장은 친 사람에게만 보인다(채널에는 알림만 올라간다).
        del sent[:]
        slash.on_interaction(interaction("open", "8000-8005", perms=str(ADMINISTRATOR), proto="tcp"))
        r.check("관리자는 열기 가능", "open:8000-8005/tcp" in actions.calls, str(actions.calls[-1:]))
        r.check("여닫는 명령 답장도 본인에게만", sent[0][1]["data"].get("flags") == 64)

        # 예전 이름으로 온 인터랙션도 계속 받아 준다 (등록이 아직 안 지워졌을 수 있다)
        del sent[:]
        legacy = interaction("status")
        legacy["data"]["name"] = LEGACY_NAMES[0]
        slash.on_interaction(legacy)
        r.check("예전 이름도 처리", len(sent) == 1)

        # 명령 등록은 POST 로 해야 한다. PUT 은 남의 명령까지 지운다.
        del sent[:]
        slash.app_id = "app-1"
        control._guild_id = "guild-1"
        methods = []
        def watch_http(url, method="GET", payload=None, headers=None, timeout=15):
            methods.append(method)
            sent.append((url, payload))
            return 200, {"id": "cmd-1", "name": "fastunnel"}
        util.http_json = watch_http
        count = slash.register()
        util.http_json = fake_http
        r.check("명령 1개 등록", count == 1, str(count))
        # 등록은 POST, 그 뒤 예전 이름을 지우려고 목록을 GET 한다. PUT 은 절대 쓰지 않는다.
        r.check("PUT 을 쓰지 않는다 (남의 명령 삭제 방지)",
                "PUT" not in methods and methods[0] == "POST", ",".join(methods))
        r.check("서버 범위로 등록", "/guilds/guild-1/commands" in sent[0][0], sent[0][0])
        r.check("보낸 건 명령 하나(목록 아님)", isinstance(sent[0][1], dict))

        # PING 인터랙션은 조용히 무시
        del sent[:]
        slash.on_interaction({"type": 1, "id": "x", "token": "y"})
        r.check("PING 은 무시", not sent)

        # 다른 애플리케이션 명령은 건드리지 않는다
        del sent[:]
        slash.on_interaction({"type": 2, "id": "x", "token": "y", "data": {"name": "other"}})
        r.check("남의 명령은 무시", not sent)
    finally:
        util.http_json = real_http

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
