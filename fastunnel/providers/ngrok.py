"""ngrok 제공자.

포트 목록으로 ngrok 설정 파일을 생성한 뒤 `ngrok start --all` 프로세스 하나를 띄우고,
로컬 관리 API(http://127.0.0.1:4040/api/tunnels)에서 공개 주소를 읽어온다.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from .. import util
from .base import BaseProvider, ProviderError, Tunnel

NAME_PREFIX = "ft"


def default_config_paths() -> List[str]:
    """ngrok이 기본으로 읽는 설정 파일 경로 후보."""
    paths = []
    if util.IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            paths.append(os.path.join(base, "ngrok", "ngrok.yml"))
    elif util.IS_MAC:
        paths.append(
            os.path.expanduser("~/Library/Application Support/ngrok/ngrok.yml")
        )
        paths.append(os.path.expanduser("~/.config/ngrok/ngrok.yml"))
    else:
        paths.append(os.path.expanduser("~/.config/ngrok/ngrok.yml"))
    paths.append(os.path.expanduser("~/.ngrok2/ngrok.yml"))
    return [p for p in paths if os.path.isfile(p)]


class NgrokTunnel(Tunnel):
    def client_hint(self) -> str:
        # ngrok tcp 엔드포인트는 host:port 로 바로 접속할 수 있다.
        return ""


class NgrokProvider(BaseProvider):
    name = "ngrok"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proc = None
        self.thread: Optional[threading.Thread] = None
        self.web_port = 0
        self.config_path = ""
        self.tunnels = [
            NgrokTunnel(port, self.proto, self.name, self.local_host) for port in self.ports
        ]
        self._by_name = {"%s-%d" % (NAME_PREFIX, t.port): t for t in self.tunnels}
        self.last_error = ""
        self._log = open(self.log_path, "a", encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        token = str(self.options.get("ngrok_authtoken", "") or "").strip()
        if not token and not default_config_paths():
            raise ProviderError(
                "ngrok 인증 토큰이 없습니다.\n"
                "      https://dashboard.ngrok.com/get-started/your-authtoken 에서 토큰을 받은 뒤\n"
                "      환경변수 NGROK_AUTHTOKEN 을 설정하거나 fastunnel.json 의 ngrok_authtoken 에 넣으세요."
            )
        if len(self.ports) > 3:
            util.warn(
                "포트가 %d개입니다. ngrok 무료 플랜은 동시 엔드포인트 수가 제한됩니다 "
                "(초과분은 실패할 수 있음)." % len(self.ports)
            )
        dead = [p for p in self.ports if not util.is_port_listening(p, self.local_host)]
        if dead:
            util.warn(
                "아직 응답 없는 로컬 포트: %s %s"
                % (util.format_ranges(dead), util.dim("(서비스가 뜨면 자동으로 연결됩니다)"))
            )

    # ------------------------------------------------------------------
    def _write_config(self) -> str:
        self.web_port = util.find_free_port(4040)
        token = str(self.options.get("ngrok_authtoken", "") or "").strip()
        region = str(self.options.get("ngrok_region", "") or "").strip()
        proto = "tcp" if self.proto == "tcp" else "http"

        lines = ['version: "2"']
        if token:
            lines.append("authtoken: %s" % token)
        if region:
            lines.append("region: %s" % region)
        lines.append("web_addr: 127.0.0.1:%d" % self.web_port)
        lines.append("log_level: info")
        lines.append("tunnels:")
        for tunnel in self.tunnels:
            lines.append("  %s-%d:" % (NAME_PREFIX, tunnel.port))
            lines.append("    proto: %s" % proto)
            lines.append("    addr: %s:%d" % (self.local_host, tunnel.port))
            if proto == "http":
                lines.append("    schemes: [https]")
        path = os.path.join(util.state_dir(), "ngrok-%d.yml" % os.getpid())
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        self.config_path = path
        util.debug("ngrok 설정 파일: %s (web_addr 127.0.0.1:%d)" % (path, self.web_port))
        return path

    def _args(self) -> List[str]:
        args = [self.binary, "start", "--all"]
        # 기본 설정 파일(인증 토큰 보관)을 먼저 넣고, 우리 설정을 덧씌운다.
        for path in default_config_paths():
            args.extend(["--config", path])
        args.extend(["--config", self.config_path])
        args.extend(["--log", "stdout", "--log-format", "logfmt"])
        return args

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._write_config()
        try:
            self.proc = self._popen(self._args())
        except OSError as exc:
            raise ProviderError("ngrok 실행 실패: %s" % exc)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if not line:
                    continue
                try:
                    self._log.write(line + "\n")
                    self._log.flush()
                except Exception:  # noqa: BLE001
                    pass
                lowered = line.lower()
                if "err=" in lowered or "lvl=eror" in lowered or "error" in lowered:
                    match = re.search(r'err="?([^"]+)"?', line)
                    message = match.group(1) if match else line
                    if message != self.last_error:
                        self.last_error = message[:300]
                        util.debug("ngrok: %s" % self.last_error)
        except Exception as exc:  # noqa: BLE001
            util.debug("ngrok 출력 읽기 종료: %s" % exc)

    # ------------------------------------------------------------------
    def _fetch_api(self) -> Optional[List[Dict[str, Any]]]:
        if not self.web_port:
            return None
        url = "http://127.0.0.1:%d/api/tunnels" % self.web_port
        status, body = util.http_json(url, timeout=5)
        if status != 200 or not isinstance(body, dict):
            return None
        items = body.get("tunnels")
        return items if isinstance(items, list) else None

    def poll(self) -> bool:
        changed = False
        if self.proc is not None and self.proc.poll() is not None and not self.stopping:
            for tunnel in self.tunnels:
                if tunnel.status != "error":
                    tunnel.status = "error"
                    tunnel.public_url = ""
                    tunnel.error = self.last_error or "ngrok 프로세스가 종료되었습니다."
            if self.last_error:
                util.warn("ngrok 오류: %s" % self.last_error)
            return True

        items = self._fetch_api()
        if items is None:
            return changed

        for item in items:
            name = str(item.get("name", ""))
            # v2 호환: "ft-8000 (http)" 같은 이름이 함께 올 수 있다.
            base_name = name.split(" ")[0]
            tunnel = self._by_name.get(base_name)
            if tunnel is None:
                continue
            url = str(item.get("public_url", ""))
            if not url:
                continue
            # http/https가 모두 오면 https를 우선한다.
            if tunnel.public_url.startswith("https://") and url.startswith("http://"):
                continue
            if tunnel.set_url(url):
                changed = True
        return changed

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self.stopping = True
        self._terminate(self.proc)
        self.proc = None
        for tunnel in self.tunnels:
            tunnel.status = "stopped"
        try:
            self._log.close()
        except Exception:  # noqa: BLE001
            pass
        if self.config_path and os.path.isfile(self.config_path):
            try:
                os.remove(self.config_path)
            except OSError:
                pass
