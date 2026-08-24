"""아주 작은 WebSocket 클라이언트 (RFC 6455).

표준 라이브러리에 WebSocket 클라이언트가 없어서 직접 만들었다. 디스코드 게이트웨이에
붙어 슬래시 명령(인터랙션)을 받는 데만 쓴다. 그래서 필요한 것만 구현했다.

- 텍스트 프레임만 보낸다 (디스코드는 JSON 문자열)
- 조각난 프레임(continuation) 은 합친다
- ping 은 자동으로 pong 한다
- 압축(permessage-deflate) 은 쓰지 않는다 (`compress` 옵션을 안 붙이면 디스코드가 안 쓴다)
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Optional, Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # RFC 6455 가 정한 상수

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """열려 있는 연결 하나. 스레드 하나에서만 recv 하도록 쓴다."""

    def __init__(self, sock, host: str):
        self.sock = sock
        self.host = host
        self.closed = False

    # ------------------------------------------------------------------
    @classmethod
    def connect(cls, url: str, timeout: float = 30.0) -> "WebSocket":
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme in ("wss", "https")
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (path, host, key)
        raw.sendall(request.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                raise WebSocketError("핸드셰이크 중 연결이 끊겼습니다.")
            header += chunk
            if len(header) > 65536:
                raw.close()
                raise WebSocketError("핸드셰이크 응답이 너무 큽니다.")

        head, _, rest = header.partition(b"\r\n\r\n")
        text = head.decode("latin-1")
        first = text.split("\r\n", 1)[0]
        if "101" not in first:
            raw.close()
            raise WebSocketError("업그레이드 거부: %s" % first[:80])

        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode()
        got = ""
        for line in text.split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        if got != expected:
            raw.close()
            raise WebSocketError("Sec-WebSocket-Accept 불일치")

        ws = cls(raw, host)
        ws._buffer = rest        # 헤더 뒤에 딸려 온 데이터가 있으면 보관
        return ws

    # ------------------------------------------------------------------
    _buffer = b""

    def _read_exact(self, count: int) -> bytes:
        """정확히 count 바이트를 읽는다. 타임아웃이면 socket.timeout 이 올라간다."""
        out = b""
        if self._buffer:
            take = self._buffer[:count]
            self._buffer = self._buffer[len(take):]
            out += take
        while len(out) < count:
            chunk = self.sock.recv(count - len(out))
            if not chunk:
                raise WebSocketError("연결이 끊겼습니다.")
            out += chunk
        return out

    def _read_frame(self) -> Tuple[int, bool, bytes]:
        b0, b1 = struct.unpack("!BB", self._read_exact(2))
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        if length > 16 * 1024 * 1024:
            raise WebSocketError("프레임이 너무 큽니다: %d" % length)
        mask_key = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked and payload:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))
        return opcode, fin, payload

    # ------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketError("이미 닫힌 연결입니다.")
        header = struct.pack("!B", 0x80 | opcode)
        length = len(payload)
        # 클라이언트가 보내는 프레임은 반드시 마스킹해야 한다 (RFC 6455)
        if length < 126:
            header += struct.pack("!B", 0x80 | length)
        elif length < 65536:
            header += struct.pack("!BH", 0x80 | 126, length)
        else:
            header += struct.pack("!BQ", 0x80 | 127, length)
        mask_key = os.urandom(4)
        masked = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))
        self.sock.sendall(header + mask_key + masked)

    def recv_text(self, timeout: Optional[float] = None) -> Optional[str]:
        """텍스트 메시지 하나를 받는다. 시간 안에 안 오면 None.

        ping/pong 같은 제어 프레임은 여기서 삼킨다.
        """
        self.sock.settimeout(timeout)
        chunks = []
        try:
            while True:
                opcode, fin, payload = self._read_frame()
                if opcode == OP_PING:
                    self._send(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    self.closed = True
                    raise WebSocketError("상대가 연결을 닫았습니다.")
                if opcode in (OP_TEXT, OP_BINARY, OP_CONTINUATION):
                    chunks.append(payload)
                    if fin:
                        return b"".join(chunks).decode("utf-8", errors="replace")
                    continue
                # 모르는 opcode 는 무시한다
        except socket.timeout:
            return None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send(OP_CLOSE, struct.pack("!H", 1000))
        except Exception:  # noqa: BLE001 - 닫는 중 실패는 의미가 없다
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
