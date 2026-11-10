"""터널 제공자 공통 인터페이스."""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import util


class ProviderError(RuntimeError):
    pass


STATUS_TEXT = {
    "starting": "시작 중",
    "online": "연결됨",
    "restarting": "재시작 중",
    "error": "오류",
    "stopped": "종료됨",
}


class Tunnel:
    """포트 하나에 대응하는 터널 상태."""

    def __init__(self, port: int, proto: str, provider: str, local_host: str = "localhost"):
        self.port = port
        self.proto = proto
        self.provider = provider
        self.local_host = local_host
        self.public_url = ""
        self.status = "starting"
        self.error = ""
        self.restarts = 0
        self.started_at = time.time()
        self.ready_at: Optional[float] = None
        # 인증 관문(auth.py)을 거치는 터널인지. 접속 안내와 게시 내용이 달라진다.
        self.auth = False

    # 표시용 --------------------------------------------------------
    def label(self) -> str:
        return "%s %d" % (self.proto.upper(), self.port)

    def address(self) -> str:
        """사용자가 실제로 접속할 주소."""
        if not self.public_url:
            return ""
        if self.proto in ("http", "https"):
            return self.public_url
        # tcp: 스킴을 떼고 host[:port] 형태로 보여준다.
        addr = self.public_url
        for scheme in ("tcp://", "https://", "http://"):
            if addr.startswith(scheme):
                addr = addr[len(scheme):]
        return addr.rstrip("/")

    def hostname(self) -> str:
        """공개 주소의 호스트 이름만 (DNS 등록 확인용). 없으면 빈 문자열."""
        if not self.public_url:
            return ""
        addr = self.public_url
        for scheme in ("tcp://", "https://", "http://"):
            if addr.startswith(scheme):
                addr = addr[len(scheme):]
                break
        addr = addr.split("/", 1)[0]
        # host:port 형태면 포트를 뗀다 (IPv6 주소는 quick tunnel 에서 나오지 않는다)
        if addr.count(":") == 1:
            addr = addr.split(":", 1)[0]
        return addr.strip()

    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, self.status)

    def client_hint(self) -> str:
        """접속 측에서 추가로 실행해야 하는 명령 (없으면 빈 문자열)."""
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "port": self.port,
            "proto": self.proto,
            "provider": self.provider,
            "url": self.public_url,
            "address": self.address(),
            "status": self.status,
            "client_hint": self.client_hint(),
            "restarts": self.restarts,
            "error": self.error,
            "auth": self.auth,
        }

    def set_url(self, url: str) -> bool:
        """주소가 새로 잡히거나 바뀌었으면 True."""
        url = (url or "").strip()
        if not url or url == self.public_url:
            return False
        self.public_url = url
        self.status = "online"
        self.ready_at = time.time()
        return True


class BaseProvider:
    name = "base"

    def __init__(
        self,
        binary: str,
        ports: Sequence[int],
        proto: str = "tcp",
        local_host: str = "localhost",
        options: Optional[Dict[str, Any]] = None,
    ):
        self.binary = binary
        self.ports = list(ports)
        self.proto = proto
        self.local_host = local_host
        self.options = dict(options or {})
        self.tunnels: List[Tunnel] = []
        self.stopping = False
        self.on_change: Optional[Callable[[List[Tunnel]], None]] = None
        self.log_path = os.path.join(
            util.log_dir(), "%s-%d.log" % (self.name, os.getpid())
        )

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        """실행 전 점검 (필요 시 하위 클래스에서 경고)."""

    def start(self) -> None:
        raise NotImplementedError

    def poll(self) -> bool:
        """상태를 갱신한다. 변경이 있으면 True."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    def online(self) -> List[Tunnel]:
        return [t for t in self.tunnels if t.public_url]

    def pending(self) -> List[Tunnel]:
        return [t for t in self.tunnels if not t.public_url and t.status != "error"]

    def wait_ready(self, timeout: int = 90, tick: float = 0.5) -> bool:
        """모든 터널이 주소를 받을 때까지 대기. 전부 준비되면 True."""
        deadline = time.time() + timeout
        last_count = -1
        while time.time() < deadline:
            self.poll()
            ready = len(self.online())
            if ready != last_count:
                last_count = ready
                util.progress("터널 주소 확보 중 %d/%d" % (ready, len(self.tunnels)))
            if ready == len(self.tunnels) and self.tunnels:
                util.progress_clear()
                return True
            if not self.pending():
                break
            time.sleep(tick)
        util.progress_clear()
        return False

    # ------------------------------------------------------------------
    @staticmethod
    def _popen(args: Sequence[str], cwd: Optional[str] = None) -> subprocess.Popen:
        creationflags = 0
        if util.IS_WINDOWS:
            # 콘솔 창이 새로 뜨지 않도록, Ctrl+C가 자식으로 전파되지 않도록 분리
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        util.debug("spawn: %s" % " ".join(args))
        return subprocess.Popen(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            creationflags=creationflags,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )

    @staticmethod
    def _terminate(proc: Optional[subprocess.Popen], timeout: float = 5.0) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
