"""알림 전송: 디스코드 웹훅 / 디스코드 봇 / 슬랙 / 텔레그램 / 범용 웹훅.

터널이 열리면 임시 도메인 주소를 묶어서 보내고, 주소가 바뀌거나 종료될 때도 알린다.
"""
from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import util

DISCORD_API = "https://discord.com/api/v10"

COLOR_START = 0x57F287   # 초록
COLOR_CHANGE = 0xFEE75C  # 노랑
COLOR_STOP = 0xED4245    # 빨강
COLOR_INFO = 0x5865F2    # 보라

MAX_FIELDS = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return "unknown-host"


def _valid_url(url: str, label: str) -> str:
    """설정 파일의 자리표시자(<ID> 등)를 실제 URL로 착각하지 않도록 걸러낸다."""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        util.warn("%s 값이 URL 형식이 아닙니다: %s" % (label, url[:60]))
        return ""
    if "<" in url or ">" in url:
        util.warn("%s 에 자리표시자가 남아 있습니다. 실제 값으로 바꿔주세요." % label)
        return ""
    return url


class Notifier:
    def __init__(self, config, provider: str = "", proto: str = "tcp"):
        self.config = config
        self.provider = provider
        self.proto = proto
        self.enabled = bool(config.bool("notify"))
        self.webhook = _valid_url(str(config.get("discord_webhook", "") or ""), "discord_webhook")
        self.bot_token = str(config.get("discord_bot_token", "") or "").strip()
        self.channel_id = str(config.get("discord_channel_id", "") or "").strip()
        self.mention = str(config.get("mention", "") or "").strip()
        self.extra: List[Dict[str, Any]] = list(config.get("webhooks", []) or [])
        self.host = _hostname()
        self._last_sent = 0.0

    # ------------------------------------------------------------------
    def channels(self) -> List[str]:
        names = []
        if self.webhook:
            names.append("디스코드 웹훅")
        if self.bot_token and self.channel_id:
            names.append("디스코드 봇")
        for item in self.extra:
            kind = str(item.get("type", "raw"))
            names.append({"slack": "슬랙", "telegram": "텔레그램"}.get(kind, "웹훅(%s)" % kind))
        return names

    def configured(self) -> bool:
        return self.enabled and bool(self.channels())

    # ------------------------------------------------------------------
    # 메시지 구성
    # ------------------------------------------------------------------
    def _tunnel_lines(self, tunnels: Sequence[Any]) -> List[str]:
        lines = []
        for tunnel in tunnels:
            if tunnel.public_url:
                lines.append("%s → %s" % (tunnel.label(), tunnel.address()))
            else:
                lines.append("%s → (%s)" % (tunnel.label(), tunnel.status_text()))
        return lines

    def _embed(self, title: str, color: int, tunnels: Sequence[Any], note: str = "") -> Dict[str, Any]:
        fields = []
        for tunnel in tunnels[:MAX_FIELDS]:
            value = tunnel.address() if tunnel.public_url else "(%s)" % tunnel.status_text()
            hint = tunnel.client_hint()
            if hint:
                value = "%s\n```%s```" % (value, hint)
            else:
                value = "`%s`" % value
            fields.append({"name": tunnel.label(), "value": value[:1024], "inline": not hint})
        embed: Dict[str, Any] = {
            "title": title,
            "color": color,
            "fields": fields,
            "timestamp": _now_iso(),
            "footer": {
                "text": "%s · %s · fastunnel" % (self.host, self.provider or "tunnel")
            },
        }
        if note:
            embed["description"] = note[:4000]
        if len(tunnels) > MAX_FIELDS:
            embed["description"] = (embed.get("description", "") + "\n외 %d개 생략" % (len(tunnels) - MAX_FIELDS)).strip()
        return embed

    def _plain(self, title: str, tunnels: Sequence[Any], note: str = "") -> str:
        lines = [title]
        if note:
            lines.append(note)
        lines.extend(self._tunnel_lines(tunnels))
        lines.append("host: %s" % self.host)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 전송
    # ------------------------------------------------------------------
    def _post_discord_webhook(self, payload: Dict[str, Any]) -> bool:
        url = self.webhook
        if "?" not in url:
            url += "?wait=false"
        return self._post_with_retry(url, payload, {}, "디스코드 웹훅")

    def _post_discord_bot(self, payload: Dict[str, Any]) -> bool:
        url = "%s/channels/%s/messages" % (DISCORD_API, self.channel_id)
        headers = {"Authorization": "Bot %s" % self.bot_token}
        body = {k: v for k, v in payload.items() if k != "username"}
        return self._post_with_retry(url, body, headers, "디스코드 봇")

    def _post_with_retry(
        self, url: str, payload: Dict[str, Any], headers: Dict[str, str], label: str
    ) -> bool:
        for attempt in range(3):
            status, body = util.http_json(url, "POST", payload, headers, timeout=15)
            if 200 <= status < 300:
                return True
            if status == 429:
                wait = 2.0
                if isinstance(body, dict):
                    try:
                        wait = float(body.get("retry_after", 2.0))
                    except (TypeError, ValueError):
                        wait = 2.0
                util.debug("%s 레이트리밋, %.1fs 대기" % (label, wait))
                time.sleep(min(wait + 0.5, 10))
                continue
            snippet = " ".join(str(body).split())[:180] if body else ""
            util.warn("%s 전송 실패 (status=%s) %s" % (label, status, util.dim(snippet)))
            if status in (401, 403):
                util.warn("  토큰/권한을 확인하세요. 봇이 채널에 접근할 수 있어야 합니다.")
            elif status == 404:
                util.warn("  웹훅 URL 또는 채널 ID가 올바른지 확인하세요.")
            return False
        util.warn("%s 전송 실패: 재시도 초과" % label)
        return False

    def _post_extra(self, item: Dict[str, Any], title: str, tunnels: Sequence[Any], note: str) -> bool:
        kind = str(item.get("type", "raw")).lower()
        text = self._plain(title, tunnels, note)
        if kind == "slack":
            url = _valid_url(str(item.get("url", "")), "webhooks[slack].url")
            if not url:
                return False
            return self._post_with_retry(url, {"text": text}, {}, "슬랙")
        if kind == "telegram":
            token = str(item.get("token", ""))
            chat_id = str(item.get("chat_id", ""))
            if not token or not chat_id:
                return False
            url = "https://api.telegram.org/bot%s/sendMessage" % token
            return self._post_with_retry(
                url, {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, {}, "텔레그램"
            )
        url = _valid_url(str(item.get("url", "")), "webhooks[%s].url" % kind)
        if not url:
            return False
        payload = {
            "event": title,
            "host": self.host,
            "provider": self.provider,
            "timestamp": _now_iso(),
            "tunnels": [t.to_dict() for t in tunnels],
        }
        headers = {str(k): str(v) for k, v in (item.get("headers") or {}).items()}
        return self._post_with_retry(url, payload, headers, "웹훅")

    # ------------------------------------------------------------------
    def send(self, title: str, tunnels: Sequence[Any], color: int = COLOR_INFO, note: str = "") -> None:
        if not self.configured():
            return
        content = self.mention if self.mention else None
        payload: Dict[str, Any] = {
            "username": "fastunnel",
            "embeds": [self._embed(title, color, tunnels, note)],
        }
        if content:
            payload["content"] = content
            payload["allowed_mentions"] = {"parse": ["users", "roles", "everyone"]}

        sent = []
        if self.webhook:
            sent.append(self._post_discord_webhook(payload))
        if self.bot_token and self.channel_id:
            sent.append(self._post_discord_bot(payload))
        for item in self.extra:
            sent.append(self._post_extra(item, title, tunnels, note))

        if any(sent):
            util.ok("알림 전송 완료 %s" % util.dim("(%s)" % ", ".join(self.channels())))
        elif sent:
            util.warn("알림을 전송하지 못했습니다.")

    # 편의 메서드 -------------------------------------------------------
    def send_started(self, tunnels: Sequence[Any], note: str = "") -> None:
        online = [t for t in tunnels if t.public_url]
        self.send(
            "터널 %d개 열림" % len(online) if online else "터널 시작 (주소 대기 중)",
            tunnels,
            COLOR_START,
            note,
        )

    def send_changed(self, tunnels: Sequence[Any], note: str = "") -> None:
        self.send("터널 주소 변경됨", tunnels, COLOR_CHANGE, note)

    def send_stopped(self, tunnels: Sequence[Any], note: str = "") -> None:
        self.send("터널 종료됨", tunnels, COLOR_STOP, note or "모든 터널이 닫혔습니다.")

    def send_test(self) -> bool:
        if not self.configured():
            util.error("설정된 알림 채널이 없습니다. discord_webhook 또는 봇 토큰/채널 ID를 설정하세요.")
            return False

        class _Fake:
            public_url = "tcp://example.trycloudflare.com:12345"
            port = 8000
            proto = "tcp"

            def label(self):
                return "TCP 8000 (테스트)"

            def address(self):
                return self.public_url

            def status_text(self):
                return "test"

            def client_hint(self):
                return ""

            def to_dict(self):
                return {"port": 8000, "url": self.public_url}

        self.send("알림 테스트", [_Fake()], COLOR_INFO, "fastunnel 알림 설정이 정상 동작합니다.")
        return True
