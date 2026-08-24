"""슬래시 명령(`/fastunnel …`) 등록과 처리.

접두사 명령(`!ft …`)은 **MESSAGE CONTENT INTENT** 가 켜져 있어야 한다. 꺼져 있으면
봇에게 메시지 내용이 빈 문자열로 오기 때문에 무슨 수를 써도 읽을 수 없다.
슬래시 명령은 그 인텐트가 필요 없어서, 설정만 맞으면 바로 동작한다.

인터랙션은 게이트웨이로 받는다(`gateway.py` 참고). 권한 판정은 인터랙션에 같이 오는
`member.permissions` 비트로 하므로 추가 조회가 필요 없다.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import util
from .gateway import DiscordGateway

ADMINISTRATOR = 1 << 3

SUB_COMMAND = 1
STRING = 3

# 인터랙션 종류 / 응답 종류
PING = 1
APPLICATION_COMMAND = 2
PONG = 1
CHANNEL_MESSAGE = 4

EPHEMERAL = 64          # 명령을 친 사람에게만 보이는 답장

# 슬래시 명령 이름은 소문자 영문으로 두고 설명을 한국어로 단다.
# (한글 이름도 규격상 되지만 클라이언트마다 입력이 번거로워서 영문으로 통일했다)
COMMANDS: List[Dict[str, Any]] = [
    {
        "name": "fastunnel",
        "description": "터널 관리",
        "options": [
            {"type": SUB_COMMAND, "name": "status", "description": "열려 있는 터널 목록 보기"},
            {"type": SUB_COMMAND, "name": "url", "description": "공개 주소만 보기"},
            {"type": SUB_COMMAND, "name": "restart",
             "description": "터널을 다시 열기 (주소가 바뀝니다) · 서버 관리자"},
            {"type": SUB_COMMAND, "name": "open",
             "description": "지정한 포트로 다시 열기 · 서버 관리자",
             "options": [{"type": STRING, "name": "ports",
                          "description": "예: 8000-8005,9000", "required": True}]},
            {"type": SUB_COMMAND, "name": "stop",
             "description": "터널을 닫고 종료 · 서버 관리자", },
            {"type": SUB_COMMAND, "name": "help", "description": "명령 목록 보기"},
        ],
    }
]

# 슬래시 하위 명령 → control 이 아는 동작 이름
ACTIONS = {
    "status": "status",
    "url": "url",
    "restart": "restart",
    "open": "open",
    "stop": "stop",
    "help": "help",
}


class SlashCommands:
    """DiscordControl 에 붙어서 슬래시 명령을 담당한다."""

    def __init__(self, control):
        self.control = control
        self.api = control.api
        self.app_id = ""
        self.gateway: Optional[DiscordGateway] = None
        self.registered = 0

    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return self.control._headers()

    def load_app_id(self) -> str:
        """애플리케이션 ID. 봇 사용자 ID 와 같은 게 보통이지만 정식 경로로 확인한다."""
        status, body = util.http_json(
            "%s/oauth2/applications/@me" % self.api, headers=self._headers(), timeout=12
        )
        if 200 <= status < 300 and isinstance(body, dict) and body.get("id"):
            self.app_id = str(body["id"])
        else:
            self.app_id = str(self.control._self_id or "")
        return self.app_id

    def register(self) -> int:
        """명령을 등록한다. 서버(길드) 범위로 넣어야 즉시 반영된다.

        길드를 모르면 전역으로 넣는데, 전역은 반영에 최대 1시간 걸릴 수 있다.
        """
        if not self.app_id:
            self.load_app_id()
        if not self.app_id:
            util.warn("애플리케이션 ID 를 알 수 없어 슬래시 명령을 등록하지 못했습니다.")
            return 0

        guild_id = getattr(self.control, "_guild_id", "")
        if guild_id:
            url = "%s/applications/%s/guilds/%s/commands" % (self.api, self.app_id, guild_id)
            scope = "서버"
        else:
            url = "%s/applications/%s/commands" % (self.api, self.app_id)
            scope = "전역(반영에 시간이 걸립니다)"

        status, body = util.http_json(url, "PUT", COMMANDS, self._headers(), timeout=20)
        if 200 <= status < 300 and isinstance(body, list):
            self.registered = len(body)
            util.ok("슬래시 명령 %d개 등록 (%s)" % (self.registered, scope))
            return self.registered

        if status == 403:
            util.warn("슬래시 명령을 등록할 권한이 없습니다. 봇 초대 링크에 "
                      "applications.commands 범위가 들어갔는지 확인하세요.")
        else:
            snippet = " ".join(str(body).split())[:200]
            util.warn("슬래시 명령 등록 실패 (status=%s) %s" % (status, util.dim(snippet)))
        return 0

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if self.register() <= 0:
            return False
        self.gateway = DiscordGateway(self.control.token, self.on_interaction, intents=0)
        self.gateway.start()
        if not self.gateway.wait_ready(25):
            util.warn("게이트웨이 연결이 늦어집니다. 붙는 대로 슬래시 명령이 동작합니다. %s"
                      % util.dim(self.gateway.last_error))
        else:
            util.ok("슬래시 명령 대기 중 (/fastunnel)")
        return True

    def stop(self) -> None:
        if self.gateway:
            self.gateway.stop()
            self.gateway = None

    # ------------------------------------------------------------------
    def on_interaction(self, payload: Dict[str, Any]) -> None:
        if payload.get("type") == PING:
            return
        if payload.get("type") != APPLICATION_COMMAND:
            return

        data = payload.get("data") or {}
        if str(data.get("name") or "") != "fastunnel":
            return

        name, args = self._read_options(data)
        member = payload.get("member") or {}
        user = member.get("user") or payload.get("user") or {}
        user_id = str(user.get("id") or "")

        action = ACTIONS.get(name, "")
        if not action:
            self.respond(payload, "모르는 명령입니다.", ephemeral=True)
            return

        if action in self.control.MUTATING_ACTIONS and not self.allowed(member, user_id):
            self.respond(payload, self.control.deny_text(name, user_id), ephemeral=True)
            return

        reply = self.control.run_action(action, args)
        # 조회 명령은 본인에게만 보여 채널을 덜 어지럽힌다. 터널을 여닫는 건 모두가 보게 둔다.
        self.respond(payload, reply or "처리했습니다.", ephemeral=action in ("status", "url", "help"))

    @staticmethod
    def _read_options(data: Dict[str, Any]):
        """`/fastunnel open ports:8000` 에서 (하위명령, 인자문자열) 을 뽑는다."""
        options = data.get("options") or []
        if not options:
            return "help", ""
        sub = options[0] or {}
        name = str(sub.get("name") or "")
        args = ""
        for option in (sub.get("options") or []):
            if str(option.get("name")) == "ports":
                args = str(option.get("value") or "").strip()
        return name, args

    def allowed(self, member: Dict[str, Any], user_id: str) -> bool:
        """인터랙션에 딸려 온 권한 비트로 판정한다. 추가 조회가 필요 없다."""
        if user_id and user_id in self.control.admins:
            return True
        if not self.control.allow_guild_admins:
            return False
        try:
            bits = int(str(member.get("permissions") or "0"))
        except ValueError:
            bits = 0
        if bits & ADMINISTRATOR:
            return True
        # DM 처럼 member 가 없거나 비트가 안 오면 기존 경로로 한 번 더 본다
        return self.control.is_guild_admin(user_id)

    # ------------------------------------------------------------------
    def respond(self, payload: Dict[str, Any], text: str, ephemeral: bool = False) -> bool:
        """3초 안에 답해야 한다. 오래 걸리는 일은 이미 큐에 넣고 바로 답한다."""
        body: Dict[str, Any] = {"content": text[:1900], "allowed_mentions": {"parse": []}}
        if ephemeral:
            body["flags"] = EPHEMERAL
        url = "%s/interactions/%s/%s/callback" % (
            self.api, payload.get("id"), payload.get("token")
        )
        status, _ = util.http_json(url, "POST", {"type": CHANNEL_MESSAGE, "data": body}, {}, 12)
        if not (200 <= status < 300):
            util.debug("인터랙션 응답 실패 (status=%s)" % status)
            return False
        return True


def commands_json() -> str:
    """등록될 명령 정의(디버깅/문서용)."""
    return json.dumps(COMMANDS, ensure_ascii=False, indent=2)
