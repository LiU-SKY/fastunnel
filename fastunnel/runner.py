"""터널 실행 세션.

CLI(`fastunnel run`)와 GUI가 같은 코드를 쓰도록 준비→시작→감시→정리 흐름을 한곳에 모았다.
각 단계는 실패해도 프로그램이 죽지 않도록 SessionError 로 감싸서 올려 보낸다.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from . import binaries, firewall as fw_mod, util
from .notify import Notifier
from .publish import address_payload, make as make_publisher
from .providers import BaseProvider, ProviderError, Tunnel, get_provider


class SessionError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 오류 메시지."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class TunnelSession:
    """설정 하나로 터널 묶음을 열고 닫는 단위."""

    def __init__(self, cfg, extra_args: str = "", tag: Optional[str] = None):
        self.cfg = cfg
        self.extra_args = extra_args or str(cfg.get("extra_args", "") or "")
        self.tag = tag
        self.ports: List[int] = []
        self.proto = "tcp"
        self.provider_name = "cloudflared"
        self.provider: Optional[BaseProvider] = None
        self.firewall: Optional[fw_mod.FirewallManager] = None
        self.notifier: Optional[Notifier] = None
        self.publisher = None
        self.binary = ""
        self.version = ""
        self.started_at = 0.0
        self._prepared = False
        self._started = False
        self._stopped = False
        self.validate()

    # ------------------------------------------------------------------
    # 1) 입력 검증
    # ------------------------------------------------------------------
    def validate(self) -> None:
        try:
            self.ports = util.parse_ports(str(self.cfg.get("ports", "") or ""))
        except util.PortSpecError as exc:
            raise SessionError(str(exc), "예: 8000-8005,9000")
        if not self.ports:
            raise SessionError(
                "터널링할 포트를 지정하세요.", "예: 8000-8005,9000 또는 25565"
            )

        self.proto = str(self.cfg.get("proto", "tcp") or "tcp").lower()
        if self.proto not in ("tcp", "http", "https"):
            raise SessionError("프로토콜은 tcp / http / https 중 하나여야 합니다.")

        self.provider_name = str(self.cfg.get("provider", "cloudflared") or "").lower()
        if self.provider_name not in ("cloudflared", "ngrok"):
            raise SessionError("제공자는 cloudflared 또는 ngrok 이어야 합니다.")

    # ------------------------------------------------------------------
    # 2) 실행 파일 + 제공자 준비
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        try:
            self.binary, self.version = binaries.ensure(
                self.provider_name,
                str(self.cfg.get("update", "auto")),
                str(self.cfg.get("bin_path", "")),
            )
        except binaries.BinaryError as exc:
            raise SessionError(str(exc), "‘실행 파일 경로’에 직접 지정하거나 업데이트 방식을 건너뜀으로 바꿔보세요.")
        except OSError as exc:
            raise SessionError(
                "터널 실행 파일을 준비하지 못했습니다: %s" % exc,
                "쓰기 권한이나 디스크 여유 공간을 확인하세요. (%s)" % util.bin_dir(),
            )
        except Exception as exc:  # noqa: BLE001  - 네트워크/TLS 등 모든 실패
            raise SessionError(
                "터널 실행 파일을 내려받지 못했습니다: %s" % exc,
                "네트워크·프록시·방화벽에서 443 아웃바운드가 열려 있는지 확인하세요.",
            )

        options: Dict[str, Any] = {
            "start_delay": self.cfg.get("start_delay", 1.0),
            "restart": self.cfg.bool("restart"),
            "max_restarts": self.cfg.get("max_restarts", 5),
            "ngrok_authtoken": self.cfg.get("ngrok_authtoken", ""),
            "ngrok_region": self.cfg.get("ngrok_region", ""),
            "cloudflared_args": self.extra_args or "",
        }
        provider_cls = get_provider(self.provider_name)
        self.provider = provider_cls(
            self.binary,
            self.ports,
            self.proto,
            str(self.cfg.get("host", "localhost")),
            options,
        )
        try:
            self.provider.preflight()
        except ProviderError as exc:
            raise SessionError(str(exc))

        self.firewall = fw_mod.FirewallManager(
            self.ports,
            self.proto,
            str(self.cfg.get("firewall_direction", "both")),
            self.cfg.bool("firewall"),
            self.cfg.bool("firewall_persist"),
            tag=self.tag,
        )
        self.notifier = Notifier(self.cfg, self.provider_name, self.proto)
        self.publisher = make_publisher(self.cfg)
        if self.publisher is not None:
            util.info("주소 게시 사용 (gist %s)" % self.publisher.gist_id)
        self._prepared = True

    # ------------------------------------------------------------------
    def needs_elevation(self) -> bool:
        """방화벽을 설정해야 하는데 권한이 부족한 상태인지."""
        if not self.cfg.bool("firewall"):
            return False
        manager = self.firewall or fw_mod.FirewallManager(
            self.ports, self.proto, str(self.cfg.get("firewall_direction", "both")), True
        )
        if manager.backend in ("none", "ufw-inactive", "firewalld-inactive"):
            return False
        return not (util.is_admin() or (not util.IS_WINDOWS and util.has_sudo()))

    def firewall_backend(self) -> str:
        manager = self.firewall
        if manager is None:
            return fw_mod.detect_backend() if self.cfg.bool("firewall") else "none"
        return manager.backend

    # ------------------------------------------------------------------
    # 3) 시작
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self._prepared:
            self.prepare()
        assert self.provider is not None and self.firewall is not None

        try:
            self.firewall.open()
        except Exception as exc:  # noqa: BLE001  - 방화벽 실패로 터널까지 막지는 않는다
            util.warn("방화벽 설정 중 오류(무시하고 계속): %s" % exc)

        try:
            self.provider.start()
        except ProviderError as exc:
            self._rollback_firewall()
            raise SessionError(str(exc))
        except OSError as exc:
            self._rollback_firewall()
            raise SessionError(
                "터널 프로세스를 실행하지 못했습니다: %s" % exc,
                "백신·보안 프로그램이 실행 파일을 막고 있는지 확인하세요. (%s)" % self.binary,
            )
        self._started = True
        self.started_at = time.time()

    def _rollback_firewall(self) -> None:
        if self.firewall is not None:
            try:
                self.firewall.close()
            except Exception as exc:  # noqa: BLE001
                util.debug("방화벽 원복 실패: %s" % exc)

    # ------------------------------------------------------------------
    # 4) 상태 확인
    # ------------------------------------------------------------------
    def wait_ready(self, timeout: Optional[int] = None) -> bool:
        if self.provider is None:
            return False
        seconds = int(timeout if timeout is not None else self.cfg.get("ready_timeout", 90))
        try:
            ready = self.provider.wait_ready(seconds)
        except Exception as exc:  # noqa: BLE001
            util.debug("wait_ready 오류: %s" % exc)
            return False
        self.wait_public_dns()
        return ready

    def wait_public_dns(self) -> str:
        """주소가 공개 DNS 에 등록될 때까지 기다린다.

        cloudflared 는 DNS 등록보다 먼저 주소를 출력한다(실측 4~10초 차이).
        그 틈에 브라우저·알림 봇·사용자가 조회하면 NXDOMAIN 이 음수 캐시되어
        등록이 끝난 뒤에도 몇 분간 접속이 안 된다. 그래서 주소를 알리기 전에 여기서 막는다.

        반환: "ok" | "timeout" | "unknown" | "skip"(대기 꺼짐/확인할 주소 없음)
        """
        try:
            wait = float(self.cfg.get("dns_wait", 45) or 0)
        except (TypeError, ValueError):
            wait = 45.0
        if wait <= 0:
            return "skip"

        hosts: List[str] = []
        for tunnel in self.tunnels:
            host = tunnel.hostname()
            if host and "." in host and host not in hosts:
                hosts.append(host)
        if not hosts:
            return "skip"

        result = "ok"
        started = time.time()
        stopping = lambda: self._stopped        # noqa: E731 - 종료 중이면 대기를 즉시 접는다
        for index, host in enumerate(hosts, 1):
            util.progress("공개 DNS 등록 대기 중 %d/%d" % (index - 1, len(hosts)))
            left = wait - (time.time() - started)
            verdict = util.wait_public_dns(host, max(0.0, left), should_stop=stopping)
            if verdict != "ok":
                result = verdict
                break
        util.progress_clear()

        if result == "ok":
            util.debug("공개 DNS 등록 확인 (%.1f초)" % (time.time() - started))
        elif result == "timeout":
            util.warn(
                "주소가 아직 공개 DNS 에 등록되지 않았습니다. "
                "지금 바로 접속하면 잠시 '주소 없음'이 뜰 수 있으니 10~20초 뒤에 다시 시도하세요."
            )
        elif result == "unknown":
            util.debug("공개 DNS 확인 불가(DoH 차단). 대기만 하고 진행합니다.")
        elif result == "aborted":
            util.debug("종료 중이라 공개 DNS 대기를 중단했습니다.")
        return result

    def poll(self) -> bool:
        if self.provider is None or self._stopped:
            return False
        try:
            changed = bool(self.provider.poll())
        except Exception as exc:  # noqa: BLE001  - 감시 루프는 절대 죽지 않아야 한다
            util.debug("poll 오류: %s" % exc)
            return False
        if changed:
            # 재시작으로 주소가 새로 발급된 경우에도 DNS 등록 전에 알리면 안 된다.
            # 여기서 한 번 막아 두면 CLI 출력·GUI 표·알림이 전부 안전해진다.
            try:
                self.wait_public_dns()
            except Exception as exc:  # noqa: BLE001 - 확인 실패가 감시를 멈춰선 안 된다
                util.debug("공개 DNS 확인 실패: %s" % exc)
        return changed

    @property
    def tunnels(self) -> List[Tunnel]:
        return list(self.provider.tunnels) if self.provider else []

    def online_count(self) -> int:
        return len([t for t in self.tunnels if t.public_url])

    def summary(self) -> str:
        return "%s · %s · %s (%d개)" % (
            self.provider_name,
            self.proto.upper(),
            util.format_ranges(self.ports),
            len(self.ports),
        )

    # ------------------------------------------------------------------
    def save_state(self, path: Optional[str] = None) -> str:
        data = {
            "pid": os.getpid(),
            "provider": self.provider_name,
            "proto": self.proto,
            "updated": time.time(),
            "tunnels": [t.to_dict() for t in self.tunnels],
        }
        target = path or os.path.join(util.state_dir(), "tunnels.json")
        try:
            util.write_json_file(target, data)
        except OSError as exc:
            util.debug("상태 파일 저장 실패: %s" % exc)
        return target

    # ------------------------------------------------------------------
    # 5) 알림 (실패해도 본 작업에 영향 없음)
    # ------------------------------------------------------------------
    def _notify(self, method: str) -> None:
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, method)(self.tunnels)
        except Exception as exc:  # noqa: BLE001
            util.warn("알림 전송 실패(무시): %s" % exc)

    def publish_address(self, note: str = "") -> bool:
        """현재 주소를 고정 URL 에 올린다. 실패해도 터널은 그대로 간다."""
        if self.publisher is None:
            return False
        try:
            payload = address_payload(self.tunnels, self.provider_name, note)
            return self.publisher.publish(payload)
        except Exception as exc:  # noqa: BLE001 - 게시 실패가 터널을 막아선 안 된다
            util.debug("주소 게시 오류: %s" % exc)
            return False

    def notify_started(self) -> None:
        # 알림보다 게시를 먼저 한다. 알림을 보고 바로 들어오는 사람이 있기 때문이다.
        self.publish_address()
        self._notify("send_started")

    def notify_changed(self) -> None:
        self.publish_address()
        if self.cfg.bool("notify_on_change"):
            self._notify("send_changed")

    def notify_stopped(self) -> None:
        self.publish_address("종료됨")
        self._notify("send_stopped")

    # ------------------------------------------------------------------
    # 6) 정리 - 어떤 단계가 실패해도 나머지는 계속 진행한다
    # ------------------------------------------------------------------
    def stop(self, notify: bool = True) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.provider is not None:
            try:
                self.provider.stop()
            except Exception as exc:  # noqa: BLE001
                util.debug("터널 종료 중 오류: %s" % exc)
        self._rollback_firewall()
        if notify and self._started:
            self.notify_stopped()
