# -*- coding: utf-8 -*-
"""디스코드 명령 수신 검증.

진짜 디스코드에 붙지 않는다. 로컬에 디스코드 API 흉내를 내는 서버를 띄우고
`DiscordControl` 이 채널을 읽어 명령을 실행하고 답장까지 보내는지 확인한다.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from _harness import Results, free_port, isolated_home

isolated_home("control")

from fastunnel import config as config_mod, util  # noqa: E402
from fastunnel.control import ControlActions, DiscordControl, format_tunnels  # noqa: E402
from fastunnel.providers.cloudflared import CloudflaredTunnel  # noqa: E402

CHANNEL = "999"
BOT_ID = "bot-self"
GUILD = "guild-1"
OWNER = "owner-1"
ADMIN_ROLE = "role-admin"


class FakeDiscord:
    """`/users/@me`, 채널 메시지 조회/전송만 흉내 낸다."""

    def __init__(self, port: int, deny: bool = False):
        self.port = port
        self.deny = deny            # True 면 모든 조회에 401 을 준다 (토큰 거부 상황)
        self.messages = []          # 채널에 쌓인 메시지 (오래된 것부터)
        self.sent = []              # 봇이 보낸 답장
        self.auth = []              # 받은 Authorization 헤더
        self.members = {}           # user_id -> 역할 목록
        self.member_lookups = []    # 권한 조회가 몇 번 갔는지 (캐시 확인용)
        self._next = 1000
        self._server = None
        self._thread = None

    def push(self, content: str, author_id: str = "user-1", bot: bool = False) -> str:
        self._next += 1
        mid = str(self._next)
        self.messages.append({
            "id": mid,
            "content": content,
            "author": {"id": author_id, "bot": bot},
        })
        return mid

    # ------------------------------------------------------------------
    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.auth.append(self.headers.get("Authorization", ""))
                if outer.deny:
                    return self._json(401, {"message": "401: Unauthorized"})
                if self.path.startswith("/users/@me"):
                    return self._json(200, {"id": BOT_ID, "username": "dictrio"})
                # --- 서버 관리자 판정에 쓰는 엔드포인트들 ---
                if self.path == "/channels/%s" % CHANNEL:
                    return self._json(200, {"id": CHANNEL, "guild_id": GUILD})
                if self.path == "/guilds/%s" % GUILD:
                    return self._json(200, {"id": GUILD, "owner_id": OWNER})
                if self.path == "/guilds/%s/roles" % GUILD:
                    return self._json(200, [
                        {"id": ADMIN_ROLE, "name": "관리자", "permissions": "8"},
                        {"id": "role-plain", "name": "멤버", "permissions": "1024"},
                    ])
                if self.path.startswith("/guilds/%s/members/" % GUILD):
                    uid = self.path.rsplit("/", 1)[1]
                    outer.member_lookups.append(uid)
                    roles = outer.members.get(uid)
                    if roles is None:
                        return self._json(404, {"message": "Unknown Member"})
                    return self._json(200, {"user": {"id": uid}, "roles": roles})
                if "/messages" in self.path:
                    after = ""
                    if "after=" in self.path:
                        after = self.path.split("after=", 1)[1].split("&", 1)[0]
                    items = outer.messages
                    if after:
                        items = [m for m in items if int(m["id"]) > int(after)]
                    limit = 1 if "limit=1" in self.path else 20
                    # 디스코드는 최신순으로 준다. 정렬 처리까지 검증하려고 일부러 뒤집는다.
                    return self._json(200, list(reversed(items))[:limit])
                return self._json(404, {})

            def do_POST(self):  # noqa: N802
                outer.auth.append(self.headers.get("Authorization", ""))
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.sent.append(payload)
                return self._json(200, {"id": "reply"})

            def log_message(self, *args):  # noqa: A003
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def api(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def wait_reply(self, count: int = 1, timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.sent) >= count:
                return True
            time.sleep(0.1)
        return False


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

    def open(self, ports):
        self.calls.append("open:%s" % ports)
        return "포트 %s 로 다시 엽니다" % ports

    def shutdown(self):
        self.calls.append("shutdown")
        return "종료합니다"


def _control(fake, actions, **over):
    data = {
        "discord_bot_token": "test-token",
        "discord_channel_id": CHANNEL,
        "discord_prefix": "!ft",
        "discord_poll": 1,
    }
    data.update(over)
    return DiscordControl(config_mod.from_dict(data), actions, api=fake.api)


def run() -> bool:
    r = Results("control - 디스코드 명령")
    util.set_verbosity(quiet=True)

    # ---------------- 명령 해석 (네트워크 없이) ----------------
    actions = FakeActions()
    ctl = _control(type("X", (), {"api": ""})(), actions)
    ctl.api = "http://127.0.0.1:1"          # 쓰지 않는다

    def msg(content, author="user-1", bot=False):
        return {"id": "1", "content": content, "author": {"id": author, "bot": bot}}

    r.check("접두사 없으면 무시", ctl.handle(msg("그냥 잡담")) == "")
    r.check("빈 내용 무시", ctl.handle(msg("")) == "")
    r.check("봇 메시지 무시", ctl.handle(msg("!ft 상태", bot=True)) == "")
    r.check("도움말", "fastunnel 명령" in ctl.handle(msg("!ft 도움말")))
    r.check("접두사만 치면 도움말", "fastunnel 명령" in ctl.handle(msg("!ft")))
    r.check("모르는 명령 안내", "모르는 명령" in ctl.handle(msg("!ft 아무거나")))
    r.check("상태(한국어)", ctl.handle(msg("!ft 상태")) == "터널 1개 정상")
    r.check("상태(영어)", ctl.handle(msg("!FT status")) == "터널 1개 정상")
    r.check("주소", ctl.handle(msg("!ft 주소")).startswith("`example"))

    # 관리자 미등록 → 터널을 건드리는 명령은 거부
    before = list(actions.calls)
    reply = ctl.handle(msg("!ft 재시작"))
    r.check("관리자 없으면 재시작 거부", "관리자" in reply and actions.calls == before, reply[:40])
    r.check("관리자 없으면 종료 거부", "관리자" in ctl.handle(msg("!ft 종료")))
    r.check("조회 명령은 그대로 허용", ctl.handle(msg("!ft 상태")) == "터널 1개 정상")

    # 관리자 등록
    ctl_admin = _control(type("X", (), {"api": ""})(), actions, discord_admins=["user-1"])
    ctl_admin.api = "http://127.0.0.1:1"
    r.check("관리자는 재시작 가능", ctl_admin.handle(msg("!ft 재시작")) == "다시 엽니다")
    r.check("다른 사람은 거부", "권한이 없습니다" in ctl_admin.handle(msg("!ft 재시작", author="user-2")))
    r.check("열기 인자 전달", ctl_admin.handle(msg("!ft 열기 8000-8005")) == "포트 8000-8005 로 다시 엽니다")
    r.check("열기 인자 없으면 안내", "포트를 함께" in ctl_admin.handle(msg("!ft 열기")))
    r.check("동작 호출 기록", "open:8000-8005" in actions.calls)

    # ---------------- 실제 폴링 루프 ----------------
    port = free_port(9700)
    with FakeDiscord(port) as fake:
        actions2 = FakeActions()
        live = _control(fake, actions2, discord_admins=["user-1"])
        fake.push("시작 전에 있던 잡담")          # 이건 처리되면 안 된다
        r.check("시작 성공", live.start() is True)
        try:
            r.check("봇 토큰 헤더", any(a == "Bot test-token" for a in fake.auth))
            fake.push("!ft 상태")
            r.check("명령 수신 → 답장", fake.wait_reply(1), "%d건" % len(fake.sent))
            if fake.sent:
                r.check("답장 내용", fake.sent[0].get("content") == "터널 1개 정상")
                r.check("멘션 차단", fake.sent[0].get("allowed_mentions") == {"parse": []})
            r.check("이전 메시지는 무시", actions2.calls == ["status"], str(actions2.calls))

            fake.push("!ft 종료", author_id="user-9")     # 권한 없음
            r.check("권한 없는 종료 거부", fake.wait_reply(2) and "shutdown" not in actions2.calls)
        finally:
            live.stop()
        r.check("중지 후 스레드 종료", not live.started)

    # ---------------- 토큰이 거부되면 조용히 꺼진다 ----------------
    with FakeDiscord(free_port(9720), deny=True) as fake2:
        bad = _control(fake2, FakeActions())
        r.check("토큰 거부되면 start 실패", bad.start() is False)
        r.check("거부되면 스레드도 안 뜬다", not bad.started)

    # 설정이 모자라면 아예 시작하지 않는다
    no_token = DiscordControl(config_mod.from_dict({"discord_channel_id": "1"}), FakeActions())
    r.check("토큰 없으면 사용 불가", "discord_bot_token" in no_token.available())
    no_channel = DiscordControl(config_mod.from_dict({"discord_bot_token": "t"}), FakeActions())
    r.check("채널 없으면 사용 불가", "discord_channel_id" in no_channel.available())

    # ---------------- 디스코드 서버 관리자 판정 ----------------
    with FakeDiscord(free_port(9740)) as fake3:
        fake3.members = {
            "boss": [ADMIN_ROLE],        # 관리자 역할 보유
            "plain": ["role-plain"],     # 평범한 멤버
            OWNER: [],                   # 서버 소유자 (역할이 없어도 관리자)
        }
        actions3 = FakeActions()
        guild = _control(fake3, actions3)          # discord_admins 는 비워 둔다
        r.check("서버 관리자 허용이 기본값", guild.allow_guild_admins is True)
        r.check("시작 성공", guild.start() is True)
        try:
            r.check("서버 ID 확보", guild._guild_id == GUILD, guild._guild_id)
            r.check("소유자 확보", guild._owner_id == OWNER, guild._owner_id)
            r.check("관리자 역할 인식", guild._admin_role_ids() == {ADMIN_ROLE})

            r.check("관리자 역할이면 허용", guild.is_guild_admin("boss") is True)
            r.check("소유자면 허용", guild.is_guild_admin(OWNER) is True)
            r.check("평범한 멤버는 거부", guild.is_guild_admin("plain") is False)
            r.check("서버에 없는 사람은 거부", guild.is_guild_admin("nobody") is False)

            before = len(fake3.member_lookups)
            guild.is_guild_admin("boss")
            r.check("판정 결과는 캐시된다", len(fake3.member_lookups) == before)

            def gmsg(content, author):
                return {"id": "1", "content": content, "author": {"id": author, "bot": False}}

            r.check("관리자는 열기 명령 가능",
                    guild.handle(gmsg("!ft 열기 8000-8005", "boss")) == "포트 8000-8005 로 다시 엽니다")
            r.check("관리자는 재시작 가능", guild.handle(gmsg("!ft 재시작", "boss")) == "다시 엽니다")
            deny = guild.handle(gmsg("!ft 열기 8000", "plain"))
            r.check("일반 멤버는 거부", "권한이 없습니다" in deny, deny.replace(chr(10), " ")[:50])
            r.check("거부 문구에 서버 관리자 안내", "서버" in deny)
            r.check("조회는 누구나", guild.handle(gmsg("!ft 상태", "plain")) == "터널 1개 정상")
        finally:
            guild.stop()

        # 끄면 목록만 본다
        off = _control(fake3, FakeActions(), discord_allow_guild_admins=False,
                       discord_admins=["someone"])
        r.check("끄면 서버 관리자 무시", off.start() is True and off.is_guild_admin("boss") is False)
        off.stop()

    # ---------------- 설정 도구 (tools/discord-setup.py) ----------------
    # 채널 ID 를 찾을 때 토큰 탐지 휴리스틱이 같이 돌아서 토큰을 채널 ID 로 집고
    # 화면에 찍은 적이 있다. 다시는 안 되도록 여기서 잡는다.
    import importlib.util
    import os

    tool_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "discord-setup.py"
    )
    if os.path.isfile(tool_path):
        spec = importlib.util.spec_from_file_location("discord_setup", tool_path)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)

        fake_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Gabcde." + "x" * 40
        env = {"DISCORD_TOKEN": fake_token, "NODE_ENV": "production"}
        r.check("토큰은 이름이 달라도 찾는다",
                setup.pick(env, setup.TOKEN_KEYS, token_like=True)[1] == fake_token)
        r.check("채널 ID 자리에 토큰을 집지 않는다",
                setup.pick(env, setup.CHANNEL_KEYS) == ("", ""))
        r.check("채널 ID 가 있으면 그건 찾는다",
                setup.pick({"CHANNEL_ID": "123"}, setup.CHANNEL_KEYS)[1] == "123")
        r.check("마스킹에 토큰 원문이 안 남는다",
                fake_token not in setup.mask(fake_token), setup.mask(fake_token))
    else:
        r.skip("설정 도구 검증", "tools/discord-setup.py 없음")

    # ---------------- 표시 문자열 ----------------
    tunnel = CloudflaredTunnel(8000, "tcp", "cloudflared")
    tunnel.set_url("tcp://abc.trycloudflare.com")
    text = format_tunnels([tunnel])
    r.check("터널 표시에 주소 포함", "abc.trycloudflare.com" in text)
    r.check("터널 표시에 접속 명령 포함", "cloudflared access tcp" in text)
    r.check("빈 목록 안내", "없습니다" in format_tunnels([]))

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
