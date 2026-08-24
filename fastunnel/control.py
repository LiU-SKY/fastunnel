"""디스코드 봇으로 명령 받기.

명령을 받는 길이 두 개다.

1. **슬래시 명령 `/ft …`** (권장) — `slash.py` + `gateway.py`.
   인텐트가 필요 없어서 설정만 맞으면 바로 동작한다.
2. **접두사 명령 `!ft …`** — 이 파일의 채널 폴링(`discord_poll`, 기본 3초).
   **MESSAGE CONTENT INTENT 가 켜져 있어야 한다.** 꺼져 있으면 메시지 내용이 빈 문자열로
   와서 무슨 수를 써도 읽을 수 없다. 그래서 이쪽만 믿으면 안 된다.

권한: 포트를 열고 닫는 명령은 **디스코드 서버 관리자**(Administrator 권한 또는 서버 소유자)이거나
`discord_admins` 에 등록된 사용자만 쓸 수 있다. 둘 다 아니면 조회 명령만 받는다. 채널에 있는 아무나 이 PC 의 포트를 열 수 있으면 안 되기 때문이다.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from . import util

DISCORD_API = "https://discord.com/api/v10"

ADMINISTRATOR = 1 << 3      # 디스코드 권한 비트: 관리자

# 권한 확인은 REST 호출이라 매번 하면 레이트리밋에 걸린다. 짧게 캐시한다.
PERM_CACHE_TTL = 60.0
ROLE_CACHE_TTL = 300.0

# 명령 이름 → 표준 동작. 한국어/영어를 같이 받는다.
ALIASES = {
    "help": "help", "도움말": "help", "명령": "help", "?": "help",
    "status": "status", "상태": "status", "st": "status",
    "url": "url", "주소": "url", "addr": "url",
    "restart": "restart", "재시작": "restart",
    "open": "open", "열기": "open",
    "stop": "stop", "종료": "stop", "끄기": "stop",
}

# 터널을 건드리는 명령. 관리자만 쓸 수 있다.
MUTATING = {"restart", "open", "stop"}

HELP_TEXT = (
    "**fastunnel 명령**\n"
    "`{p} 상태` — 지금 열려 있는 터널 목록\n"
    "`{p} 주소` — 공개 주소만 다시 보기\n"
    "`{p} 재시작` — 터널을 다시 열기 (주소가 바뀝니다) *관리자*\n"
    "`{p} 열기 8000-8005 tcp` — 지정한 포트/프로토콜로 다시 열기 *관리자*\n"
    "`{p} 종료` — 터널을 닫고 프로그램 종료 *관리자*\n"
    "`{p} 도움말` — 이 도움말"
)


class ControlActions:
    """프런트엔드가 구현하는 동작. 기본값은 전부 '지원하지 않음'이다.

    반환 문자열이 그대로 디스코드 채널에 답장으로 올라간다.
    """

    def status(self) -> str:
        return "상태를 알 수 없습니다."

    def addresses(self) -> str:
        return self.status()

    def restart(self) -> str:
        return "이 실행 방식에서는 재시작 명령을 지원하지 않습니다."

    def open(self, ports: str, proto: str = "") -> str:  # noqa: ARG002
        return "이 실행 방식에서는 포트 변경 명령을 지원하지 않습니다."

    def shutdown(self) -> str:
        return "이 실행 방식에서는 종료 명령을 지원하지 않습니다."


class DiscordControl:
    """채널을 폴링해서 명령을 실행하고 답장한다."""

    def __init__(self, config, actions: ControlActions, api: str = DISCORD_API):
        self.actions = actions
        self.api = api
        self.token = str(config.get("discord_bot_token", "") or "").strip()
        self.channel_id = str(config.get("discord_channel_id", "") or "").strip()
        self.prefix = str(config.get("discord_prefix", "!ft") or "!ft").strip()
        self.admins = [
            str(x).strip() for x in (config.get("discord_admins", []) or []) if str(x).strip()
        ]
        self.allow_guild_admins = bool(config.bool("discord_allow_guild_admins"))
        self.use_slash = bool(config.bool("discord_slash"))
        try:
            self.poll_interval = max(1.0, float(config.get("discord_poll", 3) or 3))
        except (TypeError, ValueError):
            self.poll_interval = 3.0

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_id = ""
        self._self_id = ""
        self._guild_id = ""
        self._owner_id = ""
        self._admin_roles_cache = set()
        self._admin_roles_at = 0.0
        self._perm_cache = {}
        self.slash = None
        self.started = False

    # 슬래시 명령 쪽에서도 쓰는 상수
    MUTATING_ACTIONS = MUTATING

    # ------------------------------------------------------------------
    def available(self) -> str:
        """쓸 수 있으면 빈 문자열, 아니면 왜 안 되는지."""
        if not self.token:
            return "discord_bot_token 이 없습니다."
        if not self.channel_id:
            return "discord_channel_id 가 없습니다."
        if not self.prefix:
            return "discord_prefix 가 비어 있습니다."
        return ""

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bot %s" % self.token}

    # ------------------------------------------------------------------
    def start(self) -> bool:
        reason = self.available()
        if reason:
            util.debug("디스코드 명령 수신 비활성: %s" % reason)
            return False
        if not self._prime():
            return False
        self._thread = threading.Thread(target=self._loop, name="discord-control", daemon=True)
        self._thread.start()
        self.started = True
        util.ok("디스코드 명령 수신 시작 (%s 도움말)" % self.prefix)

        if self.use_slash:
            from .slash import SlashCommands      # 게이트웨이는 필요할 때만 불러온다

            slash = SlashCommands(self)
            try:
                if slash.start():
                    self.slash = slash
            except Exception as exc:  # noqa: BLE001 - 슬래시가 안 되어도 터널은 돌아야 한다
                util.warn("슬래시 명령을 켜지 못했습니다: %s" % str(exc)[:150])
        return True

    def stop(self) -> None:
        if self.slash is not None:
            self.slash.stop()
            self.slash = None
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=self.poll_interval + 2)
        self.started = False

    # ------------------------------------------------------------------
    def _prime(self) -> bool:
        """봇 자신의 ID 를 알아 두고, 이미 쌓여 있던 메시지는 건너뛴다."""
        status, body = util.http_json(
            "%s/users/@me" % self.api, headers=self._headers(), timeout=12
        )
        if status in (401, 403):
            util.warn("디스코드 봇 토큰이 거부되었습니다 (status=%s). 명령 수신을 끕니다." % status)
            return False
        if isinstance(body, dict):
            self._self_id = str(body.get("id") or "")

        status, body = util.http_json(
            "%s/channels/%s/messages?limit=5" % (self.api, self.channel_id),
            headers=self._headers(),
            timeout=12,
        )
        if status == 403:
            util.warn("봇이 채널을 읽을 수 없습니다. 채널 권한과 MESSAGE CONTENT INTENT 를 확인하세요.")
            return False
        if status == 404:
            util.warn("discord_channel_id 를 찾을 수 없습니다: %s" % self.channel_id)
            return False
        if not (200 <= status < 300):
            util.warn("디스코드 채널을 읽지 못했습니다 (status=%s). 명령 수신을 끕니다." % status)
            return False
        if isinstance(body, list) and body:
            self._last_id = str(body[0].get("id") or "")
            self._warn_if_content_hidden(body)

        if self.allow_guild_admins:
            self._load_guild()
        return True

    def _warn_if_content_hidden(self, messages: List[Dict[str, Any]]) -> None:
        """MESSAGE CONTENT INTENT 가 꺼져 있으면 접두사 명령이 영영 안 먹는다.

        사람이 쓴 메시지인데 내용이 전부 비어 오면 그 상황으로 본다.
        조용히 안 되는 것보다 시작할 때 알려 주는 편이 낫다.
        """
        human = [m for m in messages if not ((m.get("author") or {}).get("bot"))]
        if not human:
            return
        if any(str(m.get("content") or "").strip() for m in human):
            return
        util.warn(
            "메시지 내용을 읽을 수 없습니다 (MESSAGE CONTENT INTENT 꺼짐). "
            "`%s` 접두사 명령은 동작하지 않습니다." % self.prefix
        )
        if self.use_slash:
            util.info("  슬래시 명령 %s 을 쓰세요. 인텐트 없이 동작합니다." % util.bold("/ft"))
        else:
            util.info("  Developer Portal → Bot → MESSAGE CONTENT INTENT 를 켜세요.")

    # ------------------------------------------------------------------
    # 서버 관리자 판정
    # ------------------------------------------------------------------
    def _load_guild(self) -> None:
        """채널이 속한 서버와 소유자를 알아 둔다. 실패하면 서버 관리자 판정을 쓰지 않는다."""
        status, body = util.http_json(
            "%s/channels/%s" % (self.api, self.channel_id), headers=self._headers(), timeout=12
        )
        if 200 <= status < 300 and isinstance(body, dict):
            self._guild_id = str(body.get("guild_id") or "")
        if not self._guild_id:
            util.debug("채널의 서버를 알 수 없어 서버 관리자 판정을 쓰지 않습니다.")
            return
        status, body = util.http_json(
            "%s/guilds/%s" % (self.api, self._guild_id), headers=self._headers(), timeout=12
        )
        if 200 <= status < 300 and isinstance(body, dict):
            self._owner_id = str(body.get("owner_id") or "")

    def _admin_role_ids(self) -> set:
        """관리자 권한이 붙은 역할 ID 모음. 조회에 실패하면 직전 값을 그대로 쓴다."""
        now = time.time()
        if self._admin_roles_at and now - self._admin_roles_at < ROLE_CACHE_TTL:
            return self._admin_roles_cache
        status, body = util.http_json(
            "%s/guilds/%s/roles" % (self.api, self._guild_id), headers=self._headers(), timeout=12
        )
        if not (200 <= status < 300) or not isinstance(body, list):
            return self._admin_roles_cache
        found = set()
        for role in body:
            if not isinstance(role, dict):
                continue
            try:
                bits = int(str(role.get("permissions") or "0"))
            except ValueError:
                bits = 0
            if bits & ADMINISTRATOR:
                found.add(str(role.get("id")))
        self._admin_roles_cache = found
        self._admin_roles_at = now
        return found

    def is_guild_admin(self, user_id: str) -> bool:
        """디스코드 서버에서 관리자 권한을 가졌거나 서버 소유자인지."""
        if not (self.allow_guild_admins and self._guild_id and user_id):
            return False
        now = time.time()
        cached = self._perm_cache.get(user_id)
        if cached and now < cached[0]:
            return cached[1]

        verdict = user_id == self._owner_id
        if not verdict:
            status, body = util.http_json(
                "%s/guilds/%s/members/%s" % (self.api, self._guild_id, user_id),
                headers=self._headers(),
                timeout=12,
            )
            if 200 <= status < 300 and isinstance(body, dict):
                admin_roles = self._admin_role_ids()
                verdict = any(str(r) in admin_roles for r in (body.get("roles") or []))
            elif status in (401, 403):
                util.debug("멤버 권한을 조회할 수 없습니다 (status=%s)" % status)
        self._perm_cache[user_id] = (now + PERM_CACHE_TTL, verdict)
        return verdict

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 명령 수신 실패가 터널을 죽여선 안 된다
                util.debug("디스코드 명령 수신 오류: %s" % exc)
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        url = "%s/channels/%s/messages?limit=20" % (self.api, self.channel_id)
        if self._last_id:
            url += "&after=%s" % self._last_id
        status, body = util.http_json(url, headers=self._headers(), timeout=12)

        if status == 429:
            wait = 5.0
            if isinstance(body, dict):
                try:
                    wait = float(body.get("retry_after", 5.0))
                except (TypeError, ValueError):
                    wait = 5.0
            self._stop.wait(min(wait + 0.5, 30))
            return
        if status in (401, 403):
            util.warn("디스코드 명령 수신 권한이 사라졌습니다 (status=%s). 중단합니다." % status)
            self._stop.set()
            return
        if not (200 <= status < 300) or not isinstance(body, list):
            return

        # 응답은 최신순이라 오래된 것부터 처리하도록 뒤집는다.
        for message in sorted(body, key=lambda m: _as_int(m.get("id"))):
            mid = str(message.get("id") or "")
            if mid:
                self._last_id = mid
            reply = self.handle(message)
            if reply:
                self.reply(reply)

    # ------------------------------------------------------------------
    def handle(self, message: Dict[str, Any]) -> str:
        """메시지 하나를 보고 답장 문자열을 만든다. 명령이 아니면 빈 문자열."""
        author = message.get("author") or {}
        author_id = str(author.get("id") or "")
        if author.get("bot") or (self._self_id and author_id == self._self_id):
            return ""      # 자기 자신 / 다른 봇의 말은 무시 (되먹임 방지)

        content = str(message.get("content") or "").strip()
        if not content:
            return ""      # MESSAGE CONTENT INTENT 가 꺼져 있으면 내용이 비어서 온다
        if not content.lower().startswith(self.prefix.lower()):
            return ""

        rest = content[len(self.prefix):].strip()
        if not rest:
            return HELP_TEXT.format(p=self.prefix)
        parts = rest.split(None, 1)
        name = ALIASES.get(parts[0].lower())
        args = parts[1].strip() if len(parts) > 1 else ""
        # `열기 25565 tcp` 처럼 뒤에 프로토콜을 붙일 수 있게 한다
        proto = ""
        if name == "open" and args:
            bits = args.split()
            if len(bits) > 1 and bits[-1].lower() in ("tcp", "http", "https"):
                proto = bits[-1].lower()
                args = " ".join(bits[:-1])
        if not name:
            return "모르는 명령입니다: `%s`\n%s" % (
                parts[0][:40], HELP_TEXT.format(p=self.prefix)
            )

        if name in MUTATING and not self._allowed(author_id):
            return self.deny_text(parts[0], author_id)

        return self.run_action(name, args, proto)

    def deny_text(self, typed: str, author_id: str) -> str:
        """권한이 없을 때 보여 줄 안내. 접두사 명령과 슬래시 명령이 같이 쓴다."""
        how = "디스코드 서버 **관리자 권한**이 있거나 `discord_admins` 에 등록된 사람"
        if not self.allow_guild_admins:
            how = "`discord_admins` 에 등록된 사람"
        return (
            "`%s` 명령 권한이 없습니다.\n%s만 쓸 수 있습니다.\n(당신의 ID: `%s`)"
            % (typed, how, author_id or "확인 불가")
        )

    def _allowed(self, author_id: str) -> bool:
        if not author_id:
            return False
        if author_id in self.admins:
            return True
        return self.is_guild_admin(author_id)

    def run_action(self, name: str, args: str, proto: str = "") -> str:
        try:
            if name == "help":
                return HELP_TEXT.format(p=self.prefix)
            if name == "status":
                return self.actions.status()
            if name == "url":
                return self.actions.addresses()
            if name == "restart":
                return self.actions.restart()
            if name == "stop":
                return self.actions.shutdown()
            if name == "open":
                if not args:
                    return "포트를 함께 적어 주세요. 예: `%s 열기 8000-8005`" % self.prefix
                return self.actions.open(args, proto)
        except Exception as exc:  # noqa: BLE001 - 명령 하나가 실패해도 수신은 계속되어야 한다
            util.debug("명령 처리 오류(%s): %s" % (name, exc))
            return "명령을 처리하지 못했습니다: %s" % str(exc)[:150]
        return ""

    # ------------------------------------------------------------------
    def reply(self, text: str) -> bool:
        if not text:
            return False
        payload = {
            "content": text[:1900],
            "allowed_mentions": {"parse": []},   # 답장이 @everyone 을 울리지 않도록
        }
        status, _body = util.http_json(
            "%s/channels/%s/messages" % (self.api, self.channel_id),
            "POST",
            payload,
            self._headers(),
            timeout=12,
        )
        if not (200 <= status < 300):
            util.debug("디스코드 답장 실패 (status=%s)" % status)
            return False
        return True


def _as_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def format_tunnels(tunnels: List[Any], header: str = "", only_address: bool = False) -> str:
    """터널 목록을 디스코드에 보여 줄 문자열로."""
    if not tunnels:
        return "열려 있는 터널이 없습니다."
    lines = [header] if header else []
    for tunnel in tunnels:
        address = tunnel.address() or "(주소 대기 중)"
        if only_address:
            lines.append("`%s`" % address if tunnel.address() else address)
            continue
        lines.append("**%s** %s — `%s`" % (tunnel.label(), tunnel.status_text(), address))
        hint = tunnel.client_hint()
        if hint:
            lines.append("   접속: `%s`" % hint)
    return "\n".join(lines)
