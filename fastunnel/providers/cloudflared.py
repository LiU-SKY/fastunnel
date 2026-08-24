"""cloudflared quick tunnel 제공자.

포트마다 `cloudflared tunnel --url <proto>://<host>:<port>` 프로세스를 하나씩 띄우고
출력에서 https://xxxx.trycloudflare.com 주소를 뽑아낸다. 계정/토큰이 필요 없다.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from .. import util
from .base import BaseProvider, ProviderError, Tunnel

URL_RE = re.compile(r"https://[a-zA-Z0-9][-a-zA-Z0-9.]*\.trycloudflare\.com")
ERROR_HINTS = (
    ("429", "Cloudflare 요청 제한에 걸렸습니다. 포트 수를 줄이거나 잠시 후 다시 시도하세요."),
    ("failed to request quick Tunnel", "quick tunnel 생성 실패 - 네트워크/방화벽에서 443 아웃바운드를 확인하세요."),
    ("connection refused", "로컬 포트에 아직 서비스가 없습니다. 터널은 유지되며 서비스가 뜨면 연결됩니다."),
)


class CloudflaredTunnel(Tunnel):
    def client_hint(self) -> str:
        if self.proto in ("http", "https") or not self.public_url:
            return ""
        host = self.address()
        return "cloudflared access tcp --hostname %s --url localhost:%d" % (host, self.port)


class CloudflaredProvider(BaseProvider):
    name = "cloudflared"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.procs: Dict[int, Any] = {}
        self.threads: Dict[int, threading.Thread] = {}
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.tunnels = [
            CloudflaredTunnel(port, self.proto, self.name, self.local_host)
            for port in self.ports
        ]
        self._by_port = {t.port: t for t in self.tunnels}
        self._log = open(self.log_path, "a", encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        if len(self.ports) > 10:
            util.warn(
                "포트가 %d개입니다. Cloudflare quick tunnel은 동시 생성이 많으면 제한될 수 있습니다."
                % len(self.ports)
            )
        dead = [p for p in self.ports if not util.is_port_listening(p, self.local_host)]
        if dead:
            util.warn(
                "아직 응답 없는 로컬 포트: %s %s"
                % (util.format_ranges(dead), util.dim("(서비스가 뜨면 자동으로 연결됩니다)"))
            )

    def _args_for(self, port: int) -> List[str]:
        scheme = "tcp" if self.proto == "tcp" else self.proto
        target = "%s://%s:%d" % (scheme, self.local_host, port)
        args = [
            self.binary,
            "tunnel",
            "--no-autoupdate",
            "--loglevel",
            "info",
            "--url",
            target,
        ]
        extra = self.options.get("cloudflared_args")
        if extra:
            args.extend(str(extra).split())
        return args

    # ------------------------------------------------------------------
    def start(self) -> None:
        delay = float(self.options.get("start_delay", 1.0) or 0)
        for index, port in enumerate(self.ports):
            self._spawn(port)
            if delay and index < len(self.ports) - 1:
                time.sleep(delay)

    def _spawn(self, port: int) -> None:
        tunnel = self._by_port[port]
        try:
            proc = self._popen(self._args_for(port))
        except OSError as exc:
            tunnel.status = "error"
            tunnel.error = str(exc)
            util.error("cloudflared 실행 실패 (포트 %d): %s" % (port, exc))
            return
        self.procs[port] = proc
        thread = threading.Thread(target=self._reader, args=(port, proc), daemon=True)
        thread.start()
        self.threads[port] = thread
        util.debug("cloudflared 시작 (포트 %d, pid %s)" % (port, proc.pid))

    def _reader(self, port: int, proc) -> None:
        tunnel = self._by_port[port]
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if not line:
                    continue
                with self.lock:
                    try:
                        self._log.write("[%d] %s\n" % (port, line))
                        self._log.flush()
                    except Exception:  # noqa: BLE001
                        pass
                match = URL_RE.search(line)
                if match and tunnel.set_url(match.group(0)):
                    util.debug("포트 %d 주소 확보: %s" % (port, match.group(0)))
                    self.changed.set()
                    continue
                lowered = line.lower()
                if "error" in lowered or "failed" in lowered:
                    for needle, hint in ERROR_HINTS:
                        if needle.lower() in lowered:
                            if tunnel.error != hint:
                                tunnel.error = hint
                                util.debug("포트 %d: %s" % (port, hint))
                            break
        except Exception as exc:  # noqa: BLE001
            util.debug("cloudflared 출력 읽기 종료 (포트 %d): %s" % (port, exc))

    # ------------------------------------------------------------------
    def poll(self) -> bool:
        changed = self.changed.is_set()
        self.changed.clear()
        if self.stopping:
            return changed
        max_restarts = int(self.options.get("max_restarts", 5) or 0)
        restart_enabled = bool(self.options.get("restart", True))
        for port, proc in list(self.procs.items()):
            if proc.poll() is None:
                continue
            tunnel = self._by_port[port]
            if not restart_enabled or tunnel.restarts >= max_restarts:
                if tunnel.status != "error":
                    tunnel.status = "error"
                    tunnel.error = tunnel.error or "프로세스가 종료되었습니다 (code=%s)" % proc.returncode
                    util.warn("포트 %d 터널이 종료되었습니다. %s" % (port, util.dim(tunnel.error)))
                    changed = True
                continue
            tunnel.restarts += 1
            tunnel.status = "restarting"
            tunnel.public_url = ""
            util.warn(
                "포트 %d 터널 재시작 (%d/%d)" % (port, tunnel.restarts, max_restarts)
            )
            time.sleep(min(2 * tunnel.restarts, 10))
            self._spawn(port)
            changed = True
        return changed

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self.stopping = True
        for port, proc in list(self.procs.items()):
            self._terminate(proc)
            tunnel = self._by_port.get(port)
            if tunnel:
                tunnel.status = "stopped"
        self.procs.clear()
        try:
            self._log.close()
        except Exception:  # noqa: BLE001
            pass
