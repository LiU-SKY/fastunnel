"""터널 클라이언트 인증 (`run --auth`).

왜 필요한가: TCP 터널은 `cloudflared access tcp --hostname <주소>` 만 있으면 **누구나** 붙는다.
주소는 gist 로 공개 게시되고, 접속 클라이언트 폴더는 한 번 나가면 회수할 수 없다.
그래서 "주소를 아는 사람"이 아니라 "등록된 키를 가진 사람"만 포트에 닿게 한다.

구조
- 포트마다 **인증 관문**(AuthGate)이 127.0.0.1 의 임의 포트에서 받는다.
  cloudflared 는 실제 포트 대신 관문을 가리킨다. 관문은 핸드셰이크에 성공한 연결만
  실제 포트로 잇고, 그 뒤로는 바이트를 그대로 나른다.
- 키는 사람마다 하나(`fastunnel client add <이름>`). 등록부는 `~/.fastunnel/clients.json`.
  `client revoke` 로 지우면 새 연결은 물론 **열려 있던 연결도 끊는다**(관문이 등록부를 지켜본다).

핸드셰이크 (연결마다, 앱 바이트보다 먼저. 서버가 먼저 말한다)
  1. 서버 → 클라이언트 : b"FTA1" + 서버 nonce(32)
  2. 클라이언트 → 서버 : id 길이(1) + id(UTF-8) + 클라이언트 nonce(32)
                         + HMAC-SHA256(키, b"FTA1|c|<포트>|" + 서버 nonce + 클라이언트 nonce + id)
  3. 서버 → 클라이언트 : 0x00 + HMAC-SHA256(키, b"FTA1|s|<포트>|" + …같은 재료)   (성공)
                         0x01                                                    (거부)
  - 서버 nonce 가 연결마다 새로 나오므로, 엿들은 핸드셰이크를 다시 보내도 통하지 않는다.
  - 3단계의 서버 HMAC 으로 클라이언트도 상대가 진짜 서버인지 확인한다(gist 가 바뀌어도 안전).
  - <포트>는 원격(실제) 포트 번호다. 한 포트의 핸드셰이크를 다른 포트에 쓸 수 없다.
  - 키는 선 위로 지나가지 않는다.

일부러 하지 않은 것
- **id 별 잠금.** 키가 256비트라 맞혀 볼 수 없다. 잠금은 이름만 아는 사람이 정상 사용자를
  막는 수단이 될 뿐이다. 대신 실패한 연결은 잠깐 붙잡아 두고(FAIL_DELAY) 동시 핸드셰이크 수를 제한한다.
- **암호화.** TLS 는 Cloudflare 에지에서 끝나므로 터널 안의 내용은 Cloudflare 가 볼 수 있다.
  표준 라이브러리만으로는 AES 도 인증서 생성도 없어서 1차 범위는 인증까지다(README 보안 절).

클라이언트 쪽 구현은 `client/relay.cs`(윈도우 접속 도우미)와 이 파일의 `client_handshake`(테스트·파이썬용).
두 곳의 형식을 함께 바꾸지 않으면 서로 붙지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from . import util

MAGIC = b"FTA1"
NONCE_LEN = 32
MAC_LEN = 32
KEY_LEN = 32
OK = b"\x00"
DENIED = b"\x01"

HANDSHAKE_TIMEOUT = 5.0      # 이 안에 클라이언트가 다 말하지 않으면 끊는다
FAIL_DELAY = 0.5             # 거부한 연결을 바로 놓지 않고 잠깐 붙잡는다(대량 시도 완화)
MAX_PENDING = 32             # 동시에 진행 중인 핸드셰이크 상한(관문 하나당)
WATCH_INTERVAL = 1.0         # 등록부 변경(회수)을 확인하는 간격(초)
BUFFER = 65536

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CLIENTS_FILENAME = "clients.json"
KEY_FILENAME = "client.key"


class AuthError(RuntimeError):
    """사용자에게 보여 줄 수 있는 인증 오류."""


# ----------------------------------------------------------------------
# 프로토콜
# ----------------------------------------------------------------------


def _material(side: bytes, port: int, server_nonce: bytes, client_nonce: bytes, client_id: str) -> bytes:
    return MAGIC + b"|" + side + b"|" + str(int(port)).encode("ascii") + b"|" + server_nonce + client_nonce + client_id.encode("utf-8")


def client_mac(key: bytes, port: int, server_nonce: bytes, client_nonce: bytes, client_id: str) -> bytes:
    return hmac.new(key, _material(b"c", port, server_nonce, client_nonce, client_id), hashlib.sha256).digest()


def server_mac(key: bytes, port: int, server_nonce: bytes, client_nonce: bytes, client_id: str) -> bytes:
    return hmac.new(key, _material(b"s", port, server_nonce, client_nonce, client_id), hashlib.sha256).digest()


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """정확히 size 바이트를 받는다. 상대가 먼저 끊으면 AuthError."""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AuthError("상대가 핸드셰이크 도중 연결을 끊었습니다.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def client_handshake(sock: socket.socket, client_id: str, key: bytes, port: int,
                     timeout: float = 15.0) -> None:
    """클라이언트 쪽 핸드셰이크. 성공하면 그대로 돌아오고, 실패하면 AuthError.

    timeout 이 서버보다 긴 이유: cloudflared 를 거치면 첫 바이트가 오기까지 에지를 두 번 지난다.
    """
    previous = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        hello = recv_exact(sock, len(MAGIC) + NONCE_LEN)
        if hello[:len(MAGIC)] != MAGIC:
            raise AuthError("인증을 지원하지 않는 서버입니다(서버가 --auth 로 실행 중인지 확인하세요).")
        server_nonce = hello[len(MAGIC):]
        client_nonce = secrets.token_bytes(NONCE_LEN)
        ident = client_id.encode("utf-8")
        sock.sendall(bytes([len(ident)]) + ident + client_nonce
                     + client_mac(key, port, server_nonce, client_nonce, client_id))
        status = recv_exact(sock, 1)
        if status != OK:
            raise AuthError("서버가 키를 거부했습니다(회수되었거나 다른 서버의 키입니다).")
        proof = recv_exact(sock, MAC_LEN)
        if not hmac.compare_digest(proof, server_mac(key, port, server_nonce, client_nonce, client_id)):
            raise AuthError("서버 확인에 실패했습니다. 다른 서버에 연결된 것 같습니다.")
    except socket.timeout:
        raise AuthError("서버가 응답하지 않습니다.")
    finally:
        sock.settimeout(previous)


# ----------------------------------------------------------------------
# 등록부
# ----------------------------------------------------------------------


def clients_path() -> str:
    return os.path.join(util.home_dir(), CLIENTS_FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def encode_key(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def decode_key(text: str) -> bytes:
    try:
        key = base64.b64decode(str(text).strip(), validate=True)
    except (ValueError, TypeError):
        raise AuthError("키 형식이 올바르지 않습니다.")
    if len(key) != KEY_LEN:
        raise AuthError("키 길이가 올바르지 않습니다(%d바이트여야 합니다)." % KEY_LEN)
    return key


class Registry:
    """`clients.json` 을 읽고 쓴다. 관문은 파일이 바뀌면 다시 읽는다(다른 프로세스의 revoke 반영).

    형식: {"version": 1, "clients": {"<id>": {"key": "<base64>", "created": "...", "revoked": "..."?}}}
    회수한 항목은 지우지 않고 키만 없애 둔다. 언제 누구를 회수했는지 남기기 위해서다.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or clients_path()
        self.lock = threading.Lock()
        self.clients: Dict[str, Dict[str, str]] = {}
        self._mtime = None
        self.load()

    # -- 읽기 ---------------------------------------------------------------
    def load(self) -> None:
        data = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            self._mtime = None
        except (OSError, ValueError) as exc:
            # 망가진 등록부로 아무나 통과시키면 안 된다. 비어 있는 것으로 보고 모두 거부한다.
            util.warn("접속 키 등록부를 읽지 못했습니다(모든 연결을 거부합니다): %s" % exc)
            self._mtime = None
        clients = data.get("clients") if isinstance(data, dict) else None
        with self.lock:
            self.clients = {k: dict(v) for k, v in (clients or {}).items()
                            if isinstance(v, dict) and ID_RE.match(str(k))}

    def refresh(self) -> bool:
        """파일이 바뀌었으면 다시 읽는다. 다시 읽었으면 True."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return False
        self.load()
        return True

    def key_for(self, client_id: str) -> Optional[bytes]:
        with self.lock:
            entry = self.clients.get(client_id)
        if not entry or entry.get("revoked") or not entry.get("key"):
            return None
        try:
            return decode_key(entry["key"])
        except AuthError:
            return None

    def active_ids(self) -> Set[str]:
        with self.lock:
            return {cid for cid, e in self.clients.items() if e.get("key") and not e.get("revoked")}

    def rows(self) -> List[Tuple[str, str, str]]:
        """(id, 상태, 시각) 목록. 키는 내보내지 않는다."""
        with self.lock:
            items = sorted(self.clients.items())
        out = []
        for cid, entry in items:
            if entry.get("revoked"):
                out.append((cid, "회수됨", str(entry.get("revoked"))))
            else:
                out.append((cid, "사용 중", str(entry.get("created", ""))))
        return out

    # -- 쓰기 ---------------------------------------------------------------
    def _save(self) -> None:
        with self.lock:
            data = {"version": 1, "clients": self.clients}
        util.write_json_file(self.path, data)
        if not util.IS_WINDOWS:
            # 키가 들어 있는 파일이다. 윈도우는 사용자 프로필 폴더의 ACL 이 대신 막는다.
            os.chmod(self.path, 0o600)
        self._mtime = os.path.getmtime(self.path)

    def add(self, client_id: str) -> bytes:
        client_id = str(client_id).strip()
        if not ID_RE.match(client_id):
            raise AuthError("이름은 영문 소문자·숫자·`-`·`_` 로 1~32자여야 합니다(예: kim, lab-01).")
        self.load()
        with self.lock:
            entry = self.clients.get(client_id)
            if entry and entry.get("key") and not entry.get("revoked"):
                raise AuthError("'%s' 는 이미 등록돼 있습니다. 다시 만들려면 먼저 revoke 하세요." % client_id)
            key = secrets.token_bytes(KEY_LEN)
            self.clients[client_id] = {"key": encode_key(key), "created": _now_iso()}
        self._save()
        return key

    def revoke(self, client_id: str) -> bool:
        self.load()
        with self.lock:
            entry = self.clients.get(client_id)
            if not entry or entry.get("revoked"):
                return False
            entry.pop("key", None)
            entry["revoked"] = _now_iso()
        self._save()
        return True


def write_client_key(path: str, client_id: str, key: bytes) -> None:
    """접속 도우미가 읽는 키 파일. 이 파일이 곧 출입증이므로 남에게 넘기면 안 된다."""
    util.write_json_file(path, {"id": client_id, "key": encode_key(key)})
    if not util.IS_WINDOWS:
        os.chmod(path, 0o600)


def read_client_key(path: str) -> Tuple[str, bytes]:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise AuthError("키 파일을 읽지 못했습니다: %s" % exc)
    client_id = str(data.get("id", ""))
    if not ID_RE.match(client_id):
        raise AuthError("키 파일의 이름이 올바르지 않습니다.")
    return client_id, decode_key(data.get("key", ""))


# ----------------------------------------------------------------------
# 서버 쪽 관문
# ----------------------------------------------------------------------


def _close(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _pipe(src: socket.socket, dst: socket.socket, done: threading.Event) -> None:
    """한 방향으로 나른다. 끝나면 반대쪽에 '더 보낼 것 없음'(half-close)을 알린다."""
    try:
        while True:
            data = src.recv(BUFFER)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        done.set()


class AuthGate:
    """원격 포트 하나를 지키는 관문. 127.0.0.1 의 임의 포트에서 cloudflared 의 연결을 받는다."""

    def __init__(self, registry: Registry, target_host: str, target_port: int,
                 bind_host: str = "127.0.0.1"):
        self.registry = registry
        self.target_host = target_host
        self.target_port = int(target_port)
        self.bind_host = bind_host
        self.port = 0
        self.accepted = 0
        self.denied = 0
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._pending = threading.BoundedSemaphore(MAX_PENDING)
        self._lock = threading.Lock()
        self._active: Dict[str, Set[socket.socket]] = {}
        self._threads: List[threading.Thread] = []
        self._last_warn = 0.0

    # ------------------------------------------------------------------
    def start(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind_host, 0))
        server.listen(64)
        server.settimeout(0.5)
        self._server = server
        self.port = server.getsockname()[1]
        for target in (self._accept_loop, self._watch_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        util.debug("인증 관문 시작: 127.0.0.1:%d → %s:%d" % (self.port, self.target_host, self.target_port))
        return self.port

    def stop(self) -> None:
        self._stop.set()
        _close(self._server)
        with self._lock:
            socks = [s for group in self._active.values() for s in group]
            self._active.clear()
        for sock in socks:
            _close(sock)

    # ------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._pending.acquire(blocking=False):
                # 핸드셰이크가 몰리면 새 연결은 바로 닫는다. 이미 인증된 연결에는 영향이 없다.
                _close(conn)
                self._warn_throttled("핸드셰이크 대기열이 가득 차 연결을 닫았습니다.")
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _watch_loop(self) -> None:
        """등록부가 바뀌면(revoke) 그 사람의 열린 연결을 끊는다."""
        while not self._stop.wait(WATCH_INTERVAL):
            try:
                if not self.registry.refresh():
                    continue
            except Exception as exc:  # noqa: BLE001 - 감시가 죽으면 회수가 안 먹히므로 계속 돈다
                util.debug("등록부 확인 오류: %s" % exc)
                continue
            allowed = self.registry.active_ids()
            with self._lock:
                gone = [cid for cid in self._active if cid not in allowed]
                victims = [(cid, s) for cid in gone for s in self._active.pop(cid)]
            for cid, sock in victims:
                _close(sock)
            if gone:
                util.info("회수된 접속 키의 연결을 끊었습니다: %s (포트 %d)" % (", ".join(sorted(gone)), self.target_port))

    def _warn_throttled(self, message: str) -> None:
        now = time.time()
        if now - self._last_warn > 30:
            self._last_warn = now
            util.warn("[포트 %d] %s" % (self.target_port, message))

    # ------------------------------------------------------------------
    def _authenticate(self, conn: socket.socket) -> Optional[str]:
        """핸드셰이크. 성공하면 클라이언트 id, 실패하면 None."""
        conn.settimeout(HANDSHAKE_TIMEOUT)
        server_nonce = secrets.token_bytes(NONCE_LEN)
        conn.sendall(MAGIC + server_nonce)
        size = recv_exact(conn, 1)[0]
        if not 1 <= size <= 32:
            return None
        raw_id = recv_exact(conn, size)
        client_nonce = recv_exact(conn, NONCE_LEN)
        proof = recv_exact(conn, MAC_LEN)
        try:
            client_id = raw_id.decode("utf-8")
        except UnicodeDecodeError:
            return None
        key = self.registry.key_for(client_id) if ID_RE.match(client_id) else None
        # 없는 id 도 같은 계산을 거치게 해서, 응답 시간으로 등록 여부를 가늠하지 못하게 한다.
        expected = client_mac(key or b"\x00" * KEY_LEN, self.target_port, server_nonce, client_nonce, client_id)
        if key is None or not hmac.compare_digest(proof, expected):
            return None
        conn.sendall(OK + server_mac(key, self.target_port, server_nonce, client_nonce, client_id))
        return client_id

    def _handle(self, conn: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        client_id: Optional[str] = None
        try:
            try:
                client_id = self._authenticate(conn)
            except (AuthError, OSError):
                client_id = None
            finally:
                self._pending.release()
            if client_id is None:
                self.denied += 1
                self._warn_throttled("인증에 실패한 연결이 있습니다(키 없음·회수됨·잘못된 키).")
                try:
                    conn.sendall(DENIED)
                except OSError:
                    pass
                time.sleep(FAIL_DELAY)
                return
            self.accepted += 1
            conn.settimeout(None)
            try:
                upstream = socket.create_connection((self.target_host, self.target_port), timeout=5)
                upstream.settimeout(None)
            except OSError as exc:
                util.debug("[포트 %d] 대상에 연결하지 못했습니다: %s" % (self.target_port, exc))
                return
            with self._lock:
                if client_id not in self.registry.active_ids():
                    return  # 핸드셰이크와 회수가 겹친 경우
                self._active.setdefault(client_id, set()).update((conn, upstream))
            done_a = threading.Event()
            done_b = threading.Event()
            threading.Thread(target=_pipe, args=(conn, upstream, done_a), daemon=True).start()
            _pipe(upstream, conn, done_b)
            done_a.wait()
        finally:
            if client_id is not None:
                with self._lock:
                    group = self._active.get(client_id)
                    if group is not None:
                        group.discard(conn)
                        group.discard(upstream)  # type: ignore[arg-type]
                        if not group:
                            self._active.pop(client_id, None)
            _close(upstream)
            _close(conn)
