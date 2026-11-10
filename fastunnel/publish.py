"""현재 터널 주소를 '고정 URL' 에 게시한다.

왜 필요한가: quick tunnel 주소는 실행할 때마다 바뀐다. 접속하는 사람에게 매번 새 주소를
알려 줄 수는 없으니, **절대 바뀌지 않는 URL 하나**를 두고 그 내용만 갱신한다.
클라이언트(`client/mc-connect.ps1`)는 그 URL 만 보면 된다.

지금은 GitHub Gist 를 쓴다. 이유:
- URL 이 영구 고정이고 `raw.githubusercontent.com` 으로 누구나 인증 없이 읽는다
- 무료이고 전송량 제한이 사실상 없다
- 서버는 토큰 하나로 갱신만 하면 된다 (gist 권한만 있으면 충분)

게시에 실패해도 **터널은 절대 멈추지 않는다.** 알림과 같은 원칙이다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import util

GITHUB_API = "https://api.github.com"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def address_payload(tunnels: Sequence[Any], provider: str = "", note: str = "",
                    exact_ports: bool = False) -> Dict[str, Any]:
    """클라이언트가 읽을 내용. 형식을 단순하게 유지한다.

    첫 번째 터널을 host/port 로 올려 두는 이유: 클라이언트가 흔한 경우에
    `tunnels` 배열을 뒤지지 않아도 되게 하려는 것이다.

    `auth` 가 있으면 접속 도우미는 키(client.key)로 핸드셰이크를 한다. 키 자체는 여기 없다.
    `exact_ports` 가 있으면 도우미는 포트를 옆 번호로 비키지 않는다(주소에 포트가 박힌 웹 앱용).
    """
    items: List[Dict[str, Any]] = []
    for tunnel in tunnels:
        address = tunnel.address()
        if not address:
            continue
        host = address
        for scheme in ("tcp://", "https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
        host = host.split("/", 1)[0]
        if host.count(":") == 1:
            host = host.split(":", 1)[0]
        items.append({
            "port": tunnel.port,
            "proto": tunnel.proto,
            "address": address,
            "host": host,
            "status": tunnel.status,
            "client_hint": tunnel.client_hint(),
            "auth": bool(getattr(tunnel, "auth", False)),
        })

    # 종료된 터널도 목록에는 남기되, online 판정에서는 뺀다.
    # 클라이언트가 "주소는 있는데 죽은 서버"에 계속 붙으려 하면 안 되기 때문이다.
    live = [item for item in items if item["status"] == "online"]

    payload: Dict[str, Any] = {
        "updated": _now_iso(),
        "provider": provider,
        "online": bool(live),
        "note": note,
        "tunnels": items,
        "auth": any(item["auth"] for item in items),
        "exact_ports": bool(exact_ports),
    }
    if live:
        payload["host"] = live[0]["host"]
        payload["port"] = live[0]["port"]
        payload["address"] = live[0]["address"]
    return payload


class GistPublisher:
    """설정에 gist 정보가 있으면 켜지고, 없으면 아무것도 하지 않는다."""

    def __init__(self, config, api: str = GITHUB_API):
        self.api = api
        self.gist_id = str(config.get("publish_gist_id", "") or "").strip()
        self.token = str(config.get("publish_gist_token", "") or "").strip()
        self.filename = str(config.get("publish_gist_file", "") or "mc.json").strip()
        self.enabled = bool(self.gist_id and self.token)
        self._last = ""

    def raw_url(self, owner: str = "") -> str:
        """클라이언트에 알려 줄 고정 URL (owner 를 모르면 gist 페이지 주소)."""
        if owner:
            return "https://gist.githubusercontent.com/%s/%s/raw/%s" % (
                owner, self.gist_id, self.filename
            )
        return "https://gist.github.com/%s" % self.gist_id

    def publish(self, payload: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        # 시각(updated)은 부를 때마다 달라지므로 비교에서 뺀다. 안 그러면 내용이 그대로여도
        # 매번 gist 를 갱신해 히스토리만 쌓인다.
        signature = json.dumps(
            {k: v for k, v in payload.items() if k != "updated"},
            ensure_ascii=False, sort_keys=True,
        )
        if signature == self._last:
            return True
        status, response = util.http_json(
            "%s/gists/%s" % (self.api, self.gist_id),
            "PATCH",
            {"files": {self.filename: {"content": body}}},
            {
                "Authorization": "Bearer %s" % self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        if 200 <= status < 300:
            self._last = signature
            util.debug("주소 게시 완료 (gist %s)" % self.gist_id)
            return True

        snippet = " ".join(str(response).split())[:160]
        util.warn("주소 게시 실패 (status=%s) %s" % (status, util.dim(snippet)))
        if status == 401:
            util.info("  publish_gist_token 이 올바른지, gist 권한이 있는지 확인하세요.")
        elif status == 404:
            util.info("  publish_gist_id 를 찾을 수 없습니다. 토큰 주인의 gist 가 맞는지 확인하세요.")
        return False


def make(config) -> Optional[GistPublisher]:
    """설정이 갖춰졌을 때만 게시자를 만든다."""
    publisher = GistPublisher(config)
    return publisher if publisher.enabled else None
