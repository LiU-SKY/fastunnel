"""디스코드 게이트웨이 클라이언트 (슬래시 명령 수신용).

슬래시 명령(인터랙션)은 두 가지 방법으로만 받을 수 있다.
1. 인터랙션 엔드포인트 URL — 고정된 공개 HTTPS 주소 + Ed25519 서명 검증이 필요하다.
   우리 주소는 실행할 때마다 바뀌므로 쓸 수 없다.
2. **게이트웨이(WebSocket)** — 이쪽을 쓴다.

메시지 내용을 읽는 게 아니라 인터랙션만 받으므로 **인텐트가 하나도 필요 없다**(intents=0).
`!ft` 접두사 명령이 MESSAGE CONTENT INTENT 때문에 막히던 것과 달리, 슬래시 명령은
설정만 맞으면 바로 동작하는 이유다.

재접속은 단순하게 처리한다. 끊기면 조금 기다렸다가 새로 IDENTIFY 한다.
(RESUME 은 구현하지 않는다. 인터랙션은 놓쳐도 사용자가 다시 치면 그만이다.)
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import util
from .ws import WebSocket, WebSocketError

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# 재접속 간격. 디스코드는 IDENTIFY 를 자주 하면 막으므로 넉넉히 잡는다.
RECONNECT_MIN = 5.0
RECONNECT_MAX = 60.0


class DiscordGateway:
    """인터랙션만 받아서 콜백으로 넘겨 주는 얇은 게이트웨이 클라이언트."""

    def __init__(self, token: str, on_interaction: Callable[[Dict[str, Any]], None],
                 intents: int = 0, url: str = GATEWAY_URL):
        self.token = token
        self.on_interaction = on_interaction
        self.intents = intents
        self.url = url

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[WebSocket] = None
        self._seq: Optional[int] = None
        self._send_lock = threading.Lock()
        self.connected = threading.Event()
        self.last_error = ""

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discord-gateway", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 닫기 실패는 종료를 막지 않는다
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def wait_ready(self, timeout: float = 20.0) -> bool:
        return self.connected.wait(timeout)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        delay = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                self._session()
                delay = RECONNECT_MIN          # 정상적으로 붙었었다면 간격을 되돌린다
            except Exception as exc:  # noqa: BLE001 - 어떤 이유로 끊겨도 다시 붙는다
                self.last_error = str(exc)[:200]
                util.debug("게이트웨이 끊김: %s" % self.last_error)
            finally:
                self.connected.clear()
            if self._stop.is_set():
                break
            self._stop.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    def _session(self) -> None:
        ws = WebSocket.connect(self.url, timeout=30)
        self._ws = ws
        self._seq = None
        heartbeat_stop = threading.Event()
        heartbeat: Optional[threading.Thread] = None
        try:
            hello = self._expect(ws, OP_HELLO, timeout=20)
            interval = float((hello.get("d") or {}).get("heartbeat_interval", 41250)) / 1000.0

            self._send(ws, {"op": OP_IDENTIFY, "d": {
                "token": self.token,
                "intents": self.intents,
                "properties": {"os": "linux", "browser": "fastunnel", "device": "fastunnel"},
            }})

            def beat():
                # 첫 하트비트는 지터를 줘서 몰리지 않게 한다(디스코드 권장).
                heartbeat_stop.wait(interval * 0.5)
                while not heartbeat_stop.is_set():
                    try:
                        self._send(ws, {"op": OP_HEARTBEAT, "d": self._seq})
                    except Exception:  # noqa: BLE001 - 실패하면 수신 쪽이 끊김을 알아챈다
                        return
                    heartbeat_stop.wait(interval)

            heartbeat = threading.Thread(target=beat, name="discord-heartbeat", daemon=True)
            heartbeat.start()

            while not self._stop.is_set():
                raw = ws.recv_text(timeout=1.0)
                if raw is None:
                    continue
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                self._handle(ws, packet)
        finally:
            heartbeat_stop.set()
            if heartbeat and heartbeat.is_alive():
                heartbeat.join(timeout=2)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    def _handle(self, ws: WebSocket, packet: Dict[str, Any]) -> None:
        op = packet.get("op")
        if packet.get("s") is not None:
            self._seq = packet["s"]

        if op == OP_DISPATCH:
            name = packet.get("t")
            if name == "READY":
                self.connected.set()
                user = (packet.get("d") or {}).get("user") or {}
                util.debug("게이트웨이 연결됨: %s" % user.get("username"))
            elif name == "INTERACTION_CREATE":
                data = packet.get("d") or {}
                try:
                    self.on_interaction(data)
                except Exception as exc:  # noqa: BLE001 - 명령 처리 실패가 연결을 끊어선 안 된다
                    util.debug("인터랙션 처리 오류: %s" % exc)
            return

        if op == OP_INVALID_SESSION:
            raise WebSocketError("세션이 거부되었습니다(토큰/인텐트 확인).")
        if op == OP_RECONNECT:
            raise WebSocketError("디스코드가 재접속을 요청했습니다.")
        if op == OP_HEARTBEAT:
            self._send(ws, {"op": OP_HEARTBEAT, "d": self._seq})

    # ------------------------------------------------------------------
    def _expect(self, ws: WebSocket, op: int, timeout: float) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ws.recv_text(timeout=1.0)
            if raw is None:
                continue
            try:
                packet = json.loads(raw)
            except ValueError:
                continue
            if packet.get("op") == op:
                return packet
        raise WebSocketError("op=%s 를 기다리다 시간이 지났습니다." % op)

    def _send(self, ws: WebSocket, packet: Dict[str, Any]) -> None:
        with self._send_lock:
            ws.send_text(json.dumps(packet))
