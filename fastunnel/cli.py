"""fastunnel 명령줄 인터페이스."""
from __future__ import annotations

import argparse
import os
import queue
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, binaries, config as config_mod, control as control_mod
from . import firewall as fw_mod, util
from .notify import Notifier
from .runner import SessionError, TunnelSession

SUBCOMMANDS = ("run", "gui", "install", "update", "doctor", "firewall", "notify", "config", "logs")

def banner() -> str:
    title = "fastunnel v%s" % __version__
    subtitle = "포트 범위 터널링 + 방화벽 + 알림 자동화"
    width = max(util._width(title), util._width(subtitle)) + 4
    top = "╭" + "─" * width + "╮"
    bottom = "╰" + "─" * width + "╯"
    line1 = "│  " + title + " " * (width - util._width(title) - 2) + "│"
    line2 = "│  " + subtitle + " " * (width - util._width(subtitle) - 2) + "│"
    return "\n".join(("", top, line1, line2, bottom))


BANNER = banner()

_STOP = {"flag": False}
_ORIGINAL_ARGV: List[str] = []


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def relaunch_elevated(argv: Optional[Sequence[str]] = None) -> int:
    """방화벽 설정을 위해 관리자/root 권한으로 다시 실행한다."""
    source = list(argv) if argv is not None else _ORIGINAL_ARGV
    argv = [a for a in source if a != "--elevate"]

    if util.IS_WINDOWS:
        import ctypes

        # GUI 는 콘솔이 필요 없다. pythonw 로 바로 권한 상승해서 빈 창이 남지 않게 한다.
        if argv and argv[0] == "gui":
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            executable = pythonw if os.path.isfile(pythonw) else sys.executable
            params = "-m fastunnel " + " ".join('"%s"' % a for a in argv)
            util.info("관리자 권한으로 다시 실행합니다. (UAC 승인 필요)")
            try:
                result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                    None, "runas", executable, params, os.getcwd(), 1
                )
            except Exception as exc:  # noqa: BLE001
                util.error("권한 상승 실패: %s" % exc)
                return 1
            if int(result) <= 32:
                util.error("권한 상승이 취소되었거나 실패했습니다 (code=%s)" % result)
                return 1
            return 0

        inner_args = " ".join(_ps_quote(a) for a in argv)
        command = "Set-Location %s; & %s -m fastunnel %s" % (
            _ps_quote(os.getcwd()),
            _ps_quote(sys.executable),
            inner_args,
        )
        params = '-NoExit -NoProfile -ExecutionPolicy Bypass -Command "%s"' % command.replace(
            '"', '`"'
        )
        util.info("관리자 권한으로 새 창에서 다시 실행합니다. (UAC 승인 필요)")
        try:
            result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "runas", "powershell.exe", params, None, 1
            )
        except Exception as exc:  # noqa: BLE001
            util.error("권한 상승 실패: %s" % exc)
            return 1
        if int(result) <= 32:
            util.error("권한 상승이 취소되었거나 실패했습니다 (code=%s)" % result)
            return 1
        util.ok("새 창에서 실행 중입니다. 이 창은 닫아도 됩니다.")
        return 0

    sudo = util.which("sudo")
    if not sudo:
        util.error("sudo 를 찾을 수 없습니다. root 로 직접 실행하세요.")
        return 1
    util.info("sudo 로 다시 실행합니다 …")
    os.execvp(sudo, [sudo, "-E", sys.executable, "-m", "fastunnel"] + argv)
    return 0  # 여기에는 도달하지 않는다


def _install_signal_handlers() -> None:
    def handler(signum, frame):  # noqa: ARG001
        if _STOP["flag"]:
            # 두 번째 신호는 즉시 종료
            os._exit(130)
        _STOP["flag"] = True
        print()
        util.info("종료 신호를 받았습니다. 정리 중 …")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# ----------------------------------------------------------------------
# 출력
# ----------------------------------------------------------------------


def _write_banner() -> None:
    util._write(util.cyan(BANNER))


def print_tunnels(source) -> None:
    """터널 목록을 표로 출력한다. TunnelSession / Provider 둘 다 받는다."""
    rows = []
    for tunnel in source.tunnels:
        address = tunnel.address() or util.dim("-")
        if tunnel.public_url:
            status = util.green("● " + tunnel.status_text())
            address = util.bold(address)
        elif tunnel.status == "error":
            status = util.red("● " + tunnel.status_text())
            address = util.dim(tunnel.error[:60] or "-")
        else:
            status = util.yellow("○ " + tunnel.status_text())
        rows.append([str(tunnel.port), tunnel.proto.upper(), status, address])
    print()
    print(util.render_table(["포트", "프로토콜", "상태", "공개 주소"], rows))

    hints = [(t.port, t.client_hint()) for t in source.tunnels if t.client_hint()]
    if hints:
        print()
        print(util.dim("  접속하는 쪽에서 실행할 명령:"))
        for port, hint in hints:
            print("   %s %s" % (util.dim("%d →" % port), hint))
    print()


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------


def build_run_config(args: argparse.Namespace):
    """CLI 인자를 설정 객체로 합친다."""
    cli_overrides: Dict[str, Any] = {
        "provider": args.provider,
        "ports": args.ports,
        "proto": args.proto,
        "host": args.host,
        "update": args.update,
        "bin_path": args.bin,
        "start_delay": args.start_delay,
        "ready_timeout": args.timeout,
        "dns_wait": getattr(args, "dns_wait", None),
        "firewall_direction": args.firewall_direction,
        "discord_webhook": args.webhook,
        "discord_bot_token": args.bot_token,
        "discord_channel_id": args.channel_id,
        "mention": args.mention,
        "discord_prefix": getattr(args, "discord_prefix", None),
        "discord_admins": getattr(args, "discord_admins", None),
        "ngrok_authtoken": args.authtoken,
        "ngrok_region": args.region,
    }
    if args.no_firewall:
        cli_overrides["firewall"] = False
    if args.firewall_persist:
        cli_overrides["firewall_persist"] = True
    if args.no_notify:
        cli_overrides["notify"] = False
    if getattr(args, "discord_control", False):
        cli_overrides["discord_control"] = True
    if getattr(args, "no_discord_guild_admins", False):
        cli_overrides["discord_allow_guild_admins"] = False
    if getattr(args, "no_discord_slash", False):
        cli_overrides["discord_slash"] = False
    if args.no_restart:
        cli_overrides["restart"] = False
    return config_mod.load(cli_overrides, args.config)


class _RunActions(control_mod.ControlActions):
    """디스코드 명령을 실행 중인 세션에 연결한다.

    조회 명령은 바로 답하고, 터널을 건드리는 명령은 큐에 넣어 **메인 루프가** 처리한다.
    폴링 스레드에서 세션을 여닫으면 감시 루프와 부딪힌다.
    """

    def __init__(self, holder: Dict[str, Any], requests: "queue.Queue") -> None:
        self.holder = holder
        self.requests = requests

    def _session(self):
        return self.holder.get("session")

    def status(self) -> str:
        session = self._session()
        if session is None:
            return "터널이 실행 중이 아닙니다."
        return control_mod.format_tunnels(session.tunnels, "**%s**" % session.summary())

    def addresses(self) -> str:
        session = self._session()
        if session is None:
            return "터널이 실행 중이 아닙니다."
        return control_mod.format_tunnels(session.tunnels, only_address=True)

    def restart(self) -> str:
        self.requests.put(("restart", "", ""))
        return "터널을 다시 엽니다. 주소가 바뀌니 잠시 뒤 새 주소를 알려 드릴게요."

    def open(self, ports: str, proto: str = "") -> str:
        try:
            parsed = util.parse_ports(ports)
        except util.PortSpecError as exc:
            return "포트를 해석할 수 없습니다: %s" % exc
        proto = (proto or "").lower()
        if proto and proto not in ("tcp", "http", "https"):
            return "프로토콜은 tcp / http / https 중 하나여야 합니다."
        spec = util.format_ranges(parsed)
        self.requests.put(("open", spec, proto))
        label = "%s %s" % (proto.upper(), spec) if proto else spec
        return "`%s` 로 다시 엽니다. 잠시만요." % label

    def shutdown(self) -> str:
        self.requests.put(("stop", "", ""))
        return "터널을 닫고 종료합니다."


def _reopen(session, cfg, ports: str, proto: str, extra_args: str):
    """실행 중인 세션을 닫고 (필요하면 다른 포트/프로토콜로) 새로 연다."""
    session.stop(notify=False)
    if ports or proto:
        changed = dict(cfg.data)
        if ports:
            changed["ports"] = ports
        if proto:
            changed["proto"] = proto
        cfg = config_mod.from_dict(changed, list(cfg.sources) + ["디스코드 명령"])
    fresh = TunnelSession(cfg, extra_args=extra_args)
    fresh.prepare()
    fresh.start()
    fresh.wait_ready()
    return fresh, cfg


def cmd_run(args: argparse.Namespace) -> int:
    cfg = build_run_config(args)

    try:
        session = TunnelSession(cfg, extra_args=args.extra_args or "")
    except SessionError as exc:
        util.error(str(exc))
        if exc.hint:
            print("      %s" % util.dim(exc.hint))
        return 2

    if not util._QUIET:
        _write_banner()
    util.info("제공자 %s · 프로토콜 %s · 포트 %s (%d개)" % (
        util.bold(session.provider_name),
        session.proto.upper(),
        util.format_ranges(session.ports),
        len(session.ports),
    ))

    # 1) 실행 파일 + 제공자 준비 ----------------------------------------
    try:
        session.prepare()
    except SessionError as exc:
        util.error(str(exc))
        if exc.hint:
            print("      %s" % util.dim(exc.hint))
        return 1

    if args.dry_run:
        util.info("--dry-run: 실제로 실행하지 않고 계획만 출력합니다.")
        manager = session.firewall
        print(util.dim("  방화벽 백엔드: %s" % fw_mod.backend_label(manager.backend)))
        for _, add, _ in manager.plan():
            print("   %s %s" % (util.dim("+"), " ".join(add)))
        print(util.dim("  터널 실행 파일: %s (%s)" % (session.binary, session.version or "?")))
        print(util.dim("  알림 채널: %s" % (", ".join(session.notifier.channels()) or "없음")))
        return 0

    # 2) 권한 상승이 필요하면 다시 실행 ---------------------------------
    if getattr(args, "elevate", False) and session.needs_elevation():
        return relaunch_elevated()

    if session.notifier.enabled and not session.notifier.channels():
        util.warn("알림 채널이 설정되지 않았습니다. %s" % util.dim("(fastunnel config init 참고)"))

    # 3) 터널 시작 ------------------------------------------------------
    _install_signal_handlers()
    exit_code = 0
    control = None
    try:
        util.info("터널을 시작합니다 …")
        session.start()
        if not session.wait_ready():
            missing = [t.port for t in session.tunnels if not t.public_url]
            if missing:
                util.warn("일부 터널이 아직 준비되지 않았습니다: %s" % util.format_ranges(missing))
        if session.online_count():
            util.ok("터널 %d/%d개 연결됨" % (session.online_count(), len(session.tunnels)))
        print_tunnels(session)
        util.debug("상태 파일: %s" % session.save_state(args.out))

        session.notify_started()

        # 3-1) 디스코드 명령 수신 ---------------------------------------
        holder: Dict[str, Any] = {"session": session}
        requests: "queue.Queue" = queue.Queue()
        control = None
        if cfg.bool("discord_control"):
            control = control_mod.DiscordControl(cfg, _RunActions(holder, requests))
            reason = control.available()
            if reason:
                util.warn("디스코드 명령 수신을 켤 수 없습니다: %s" % reason)
                control = None
            elif not control.start():
                control = None

        util.info("실행 중입니다. 종료하려면 %s 를 누르세요." % util.bold("Ctrl+C"))

        # 4) 감시 루프 --------------------------------------------------
        last_notify = time.time()
        started = time.time()
        duration = float(getattr(args, "duration", 0) or 0)
        pending_change = False
        while not _STOP["flag"]:
            time.sleep(1.0)
            if duration and time.time() - started >= duration:
                util.info("지정한 실행 시간(%.0fs)이 지나 종료합니다." % duration)
                break

            # 디스코드에서 온 요청 처리 (세션을 여닫는 건 이 스레드에서만 한다)
            action = ""
            action_arg = ""
            action_proto = ""
            try:
                action, action_arg, action_proto = requests.get_nowait()
            except queue.Empty:
                pass
            if action == "stop":
                util.info("디스코드 명령으로 종료합니다.")
                break
            if action in ("restart", "open"):
                bits = [b for b in (action_proto.upper(), action_arg) if b]
                label = "%s 로 다시 엽니다" % " ".join(bits) if bits else "터널을 다시 엽니다"
                util.info("디스코드 명령: %s" % label)
                try:
                    session, cfg = _reopen(
                        session, cfg, action_arg, action_proto, args.extra_args or "")
                except SessionError as exc:
                    util.error(str(exc))
                    if control:
                        control.reply("다시 열지 못했습니다: %s" % exc)
                    break
                holder["session"] = session
                print_tunnels(session)
                session.save_state(args.out)
                session.notify_started()
                if control:
                    control.reply(control_mod.format_tunnels(
                        session.tunnels, "**다시 열었습니다** · %s" % session.summary()))
                last_notify = time.time()
                pending_change = False
                continue

            if session.poll():
                pending_change = True
                session.save_state(args.out)
            if pending_change and time.time() - last_notify > 5:
                print_tunnels(session)
                session.notify_changed()
                if control:
                    control.reply(control_mod.format_tunnels(
                        session.tunnels, "**주소가 바뀌었습니다**"))
                last_notify = time.time()
                pending_change = False
    except KeyboardInterrupt:
        print()
    except SessionError as exc:
        util.error(str(exc))
        if exc.hint:
            print("      %s" % util.dim(exc.hint))
        exit_code = 1
    except Exception as exc:  # noqa: BLE001
        util.error("예기치 못한 오류: %s" % exc)
        if args.verbose:
            import traceback

            traceback.print_exc()
        exit_code = 1
    finally:
        if control is not None:
            control.stop()
        util.info("터널을 정리합니다 …")
        session.stop()
        util.ok("정리 완료")
    return exit_code


# ----------------------------------------------------------------------
# 기타 명령
# ----------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    targets = [args.provider] if args.provider else ["cloudflared", "ngrok"]
    update = "force" if getattr(args, "force", False) else "auto"
    failed = 0
    for name in targets:
        try:
            path, version = binaries.ensure(name, update)
            util.ok("%s → %s %s" % (name, path, util.dim(version or "")))
        except Exception as exc:  # noqa: BLE001
            util.error("%s 설치 실패: %s" % (name, exc))
            failed += 1
    return 1 if failed else 0


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = config_mod.load(None, args.config)
    print(util.cyan(BANNER))
    print(util.bold("  환경"))
    print("   OS          : %s (%s)" % (util.os_tag(), util.arch_tag()))
    print("   Python      : %s" % sys.version.split()[0])
    print("   홈 디렉터리 : %s" % util.home_dir())
    print(
        "   권한        : %s"
        % (util.green("관리자/root") if util.is_admin() else util.yellow("일반 사용자"))
    )
    if not util.is_admin() and not util.IS_WINDOWS:
        print("   sudo(무암호): %s" % ("가능" if util.has_sudo() else "불가"))

    print()
    print(util.bold("  터널 실행 파일"))
    for name in ("cloudflared", "ngrok"):
        path = binaries.locate(name)
        if path:
            version = binaries.detect_version(path, name)
            print("   %-12s: %s %s" % (name, util.green("있음"), util.dim("%s (%s)" % (version or "?", path))))
        else:
            print("   %-12s: %s %s" % (name, util.yellow("없음"), util.dim("(첫 실행 시 자동 설치)")))

    print()
    print(util.bold("  방화벽"))
    backend = fw_mod.detect_backend()
    print("   백엔드      : %s" % fw_mod.backend_label(backend))
    leftovers = fw_mod.list_leftovers()
    if leftovers:
        print(
            "   %s %s"
            % (
                util.yellow("남은 규칙  :"),
                "%d개 (fastunnel firewall cleanup 으로 정리)" % len(leftovers),
            )
        )

    print()
    print(util.bold("  알림"))
    notifier = Notifier(cfg)
    channels = notifier.channels()
    print("   채널        : %s" % (", ".join(channels) if channels else util.yellow("설정 없음")))
    print("   알림 사용   : %s" % ("예" if cfg.bool("notify") else "아니오"))

    print()
    print(util.bold("  설정"))
    print("   출처        : %s" % " < ".join(cfg.sources))
    for key, value in cfg.masked().items():
        if key in ("ports", "provider", "proto", "update", "firewall", "firewall_direction"):
            print("   %-12s: %s" % (key, value))

    print()
    print(util.bold("  네트워크"))
    for label, url in (
        ("GitHub API", "https://api.github.com"),
        ("Cloudflare", "https://api.trycloudflare.com"),
        ("Discord", "https://discord.com/api/v10/gateway"),
    ):
        start = time.time()
        status, _ = util.http_request(url, timeout=6)
        took = (time.time() - start) * 1000
        mark = util.green("OK") if status else util.red("실패")
        print("   %-12s: %s %s" % (label, mark, util.dim("%d, %.0fms" % (status, took))))

    # 터널 주소는 발급 직후 몇 초간 DNS 에 없다. 그때 조회하면 NXDOMAIN 이 캐시되어
    # 한동안 접속이 막힌다. 그걸 피하려면 DoH 로 등록을 확인할 수 있어야 한다.
    verdict, _ips = util.resolve_public("dns.google")
    dns_wait = cfg.get("dns_wait", 45)
    label = util._pad("DNS 등록확인", 12)   # 한글은 폭이 2라 %-12s 로는 안 맞는다
    if verdict == "ok":
        print("   %s: %s %s" % (
            label, util.green("가능"),
            util.dim("DoH 사용 · 최대 %s초 대기 후 주소 공개" % dns_wait)))
    else:
        print("   %s: %s %s" % (
            label, util.yellow("불가"),
            util.dim("DoH 차단됨 · %.0f초 고정 대기로 대체" % min(util.DNS_BLIND_GRACE, float(dns_wait or 0)))))
    print()
    return 0


def cmd_firewall(args: argparse.Namespace) -> int:
    action = args.action
    if action == "status":
        print(fw_mod.status())
        return 0
    if action == "cleanup":
        fw_mod.cleanup(dry_run=args.dry_run)
        return 0

    cfg = config_mod.load({"ports": args.ports, "proto": args.proto}, args.config)
    try:
        ports = util.parse_ports(str(cfg.get("ports", "")))
    except util.PortSpecError as exc:
        util.error(str(exc))
        return 2
    if not ports:
        util.error("포트를 지정하세요. 예: fastunnel firewall open -p 8000-8005")
        return 2

    manager = fw_mod.FirewallManager(
        ports,
        str(cfg.get("proto", "tcp")),
        args.direction,
        True,
        persist=(action == "open"),
    )
    if action == "open":
        manager.open()
        if manager.applied:
            util.info(
                "규칙이 유지됩니다. 제거하려면 %s"
                % util.bold("fastunnel firewall cleanup")
            )
        return 0
    if action == "close":
        # 계획을 역으로 실행한다 (상태 파일이 없는 경우 대비).
        removed = 0
        for name, _, undo in manager.plan():
            code, _, _ = util.run_cmd(undo, timeout=30)
            if code == 0:
                removed += 1
        util.ok("규칙 %d개 제거" % removed)
        fw_mod.cleanup()
        return 0
    return 2


def cmd_notify(args: argparse.Namespace) -> int:
    overrides = {
        "discord_webhook": args.webhook,
        "discord_bot_token": args.bot_token,
        "discord_channel_id": args.channel_id,
        "mention": args.mention,
    }
    cfg = config_mod.load(overrides, args.config)
    notifier = Notifier(cfg, "test", "tcp")
    return 0 if notifier.send_test() else 1


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "path":
        for path in config_mod.config_search_paths(args.config):
            mark = "있음" if os.path.isfile(path) else "없음"
            print("%s %s" % (path, util.dim("(%s)" % mark)))
        return 0
    if args.action == "init":
        target = args.config or os.path.join(os.getcwd(), config_mod.CONFIG_FILENAME)
        try:
            config_mod.write_example(target, force=args.force)
        except FileExistsError:
            util.error("이미 존재합니다: %s %s" % (target, util.dim("(--force 로 덮어쓰기)")))
            return 1
        util.ok("설정 파일 생성: %s" % target)
        print(util.dim("  discord_webhook 값을 채운 뒤 fastunnel notify test 로 확인하세요."))
        return 0
    cfg = config_mod.load(None, args.config)
    print(util.dim("출처: %s" % " < ".join(cfg.sources)))
    for key, value in cfg.masked().items():
        print("  %-20s %s" % (key, value))
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """GUI 실행. tkinter 가 없으면 원인과 해결 방법을 알려준다."""
    try:
        from .gui import launch
    except SystemExit as exc:  # gui.py 가 tkinter 부재를 알려 준다
        util.error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        util.error("GUI 모듈을 불러오지 못했습니다: %s" % exc)
        return 1
    return launch(args.config)


def cmd_logs(args: argparse.Namespace) -> int:  # noqa: ARG001
    directory = util.log_dir()
    files = sorted(
        (os.path.join(directory, f) for f in os.listdir(directory)),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    if not files:
        util.info("로그가 없습니다. (%s)" % directory)
        return 0
    latest = files[0]
    print(util.dim("== %s ==" % latest))
    with open(latest, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in lines[-args.lines :]:
        print(line.rstrip())
    return 0


# ----------------------------------------------------------------------
# 파서
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fastunnel",
        description="cloudflared / ngrok 으로 포트 범위를 빠르게 터널링하고 "
        "방화벽 설정과 디스코드 알림까지 자동으로 처리합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""예시:
  fastunnel gui                    그래픽 화면으로 관리
  fastunnel 8000-8005              8000~8005 포트를 TCP로 터널링
  fastunnel run -p 25565 --proto tcp --provider ngrok
  fastunnel run -p 8080 --proto http --update skip
  fastunnel notify test            알림 설정 확인
  fastunnel doctor                 환경 점검
  fastunnel firewall cleanup       남아 있는 방화벽 규칙 정리
""",
    )
    parser.add_argument("--version", action="version", version="fastunnel %s" % __version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 로그")
    parser.add_argument("-q", "--quiet", action="store_true", help="출력 최소화")
    parser.add_argument("-c", "--config", help="설정 파일 경로")

    sub = parser.add_subparsers(dest="command")

    # run ---------------------------------------------------------------
    run = sub.add_parser("run", help="터널 실행 (기본 명령)")
    run.add_argument("ports_pos", nargs="?", help="포트 범위 (예: 8000-8005,9000)")
    run.add_argument("-p", "--ports", help="포트 범위 (예: 8000-8005,9000)")
    run.add_argument(
        "--provider", choices=["cloudflared", "ngrok"], help="터널 제공자 (기본: cloudflared)"
    )
    run.add_argument("--proto", choices=["tcp", "http", "https"], help="프로토콜 (기본: tcp)")
    run.add_argument("--host", help="로컬 대상 호스트 (기본: localhost)")
    run.add_argument(
        "--update",
        choices=["auto", "skip", "force", "ask"],
        help="실행 파일 업데이트 방식 (기본: auto)",
    )
    run.add_argument("--bin", help="터널 실행 파일 경로 직접 지정")
    run.add_argument("--no-firewall", action="store_true", help="방화벽 설정 건너뛰기")
    run.add_argument(
        "--elevate",
        action="store_true",
        help="방화벽 설정을 위해 관리자(Windows) / sudo(Linux) 권한으로 다시 실행",
    )
    run.add_argument(
        "--firewall-direction",
        choices=["in", "out", "both"],
        help="방화벽 방향 (기본: both)",
    )
    run.add_argument(
        "--firewall-persist", action="store_true", help="종료 시 방화벽 규칙을 지우지 않음"
    )
    run.add_argument("--no-notify", action="store_true", help="알림 보내지 않기")
    run.add_argument("--webhook", help="디스코드 웹훅 URL")
    run.add_argument("--bot-token", help="디스코드 봇 토큰")
    run.add_argument("--channel-id", help="디스코드 채널 ID")
    run.add_argument("--mention", help="알림에 붙일 멘션 (예: @here)")
    run.add_argument(
        "--discord-control",
        action="store_true",
        help="디스코드 봇으로 명령 받기 (봇 토큰 + 채널 ID 필요)",
    )
    run.add_argument("--discord-prefix", help="디스코드 명령 접두사 (기본: !ft)")
    run.add_argument(
        "--no-discord-slash",
        action="store_true",
        help="슬래시 명령(/fastunnel)을 쓰지 않음 (접두사 명령만)",
    )
    run.add_argument(
        "--no-discord-guild-admins",
        action="store_true",
        help="디스코드 서버 관리자를 자동으로 허용하지 않음 (discord_admins 목록만 사용)",
    )
    run.add_argument(
        "--discord-admin",
        action="append",
        dest="discord_admins",
        help="터널을 여닫을 수 있는 디스코드 사용자 ID (여러 번 지정 가능)",
    )
    run.add_argument("--authtoken", help="ngrok authtoken")
    run.add_argument("--region", help="ngrok region (us, eu, ap, au, sa, jp, in)")
    run.add_argument("--start-delay", type=float, help="터널 순차 기동 간격(초)")
    run.add_argument("--timeout", type=int, help="주소 확보 대기 시간(초)")
    run.add_argument(
        "--dns-wait",
        type=int,
        help="주소가 공개 DNS에 등록될 때까지 대기(초, 기본 45). 0이면 기다리지 않음",
    )
    run.add_argument("--no-restart", action="store_true", help="터널이 죽어도 재시작하지 않음")
    run.add_argument(
        "--duration", type=float, help="지정한 초가 지나면 자동 종료 (0이면 무제한)"
    )
    run.add_argument("--out", help="터널 정보를 저장할 JSON 경로")
    run.add_argument("--extra-args", help="터널 실행 파일에 추가로 전달할 인자")
    run.add_argument("--dry-run", action="store_true", help="실행하지 않고 계획만 출력")
    run.set_defaults(func=cmd_run)

    # gui ---------------------------------------------------------------
    gui = sub.add_parser("gui", help="그래픽 화면으로 관리 (윈도우 권장)")
    gui.set_defaults(func=cmd_gui)

    # install -----------------------------------------------------------
    install = sub.add_parser("install", help="터널 실행 파일 설치/갱신")
    install.add_argument("provider", nargs="?", choices=["cloudflared", "ngrok"])
    install.add_argument("-f", "--force", action="store_true", help="강제 재설치")
    install.set_defaults(func=cmd_install)

    update = sub.add_parser("update", help="터널 실행 파일 업데이트 (install --force 와 동일)")
    update.add_argument("provider", nargs="?", choices=["cloudflared", "ngrok"])
    update.set_defaults(func=cmd_install, force=True)

    # doctor ------------------------------------------------------------
    doctor = sub.add_parser("doctor", help="환경 점검")
    doctor.set_defaults(func=cmd_doctor)

    # firewall ----------------------------------------------------------
    firewall = sub.add_parser("firewall", help="방화벽 규칙 관리")
    firewall.add_argument("action", choices=["open", "close", "cleanup", "status"])
    firewall.add_argument("-p", "--ports", help="포트 범위")
    firewall.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    firewall.add_argument(
        "--direction", choices=["in", "out", "both"], default="both", help="방향 (기본: both)"
    )
    firewall.add_argument("--dry-run", action="store_true", help="실행하지 않고 명령만 출력")
    firewall.set_defaults(func=cmd_firewall)

    # notify ------------------------------------------------------------
    notify = sub.add_parser("notify", help="알림 테스트")
    notify.add_argument("action", nargs="?", choices=["test"], default="test")
    notify.add_argument("--webhook", help="디스코드 웹훅 URL")
    notify.add_argument("--bot-token", help="디스코드 봇 토큰")
    notify.add_argument("--channel-id", help="디스코드 채널 ID")
    notify.add_argument("--mention", help="멘션")
    notify.set_defaults(func=cmd_notify)

    # config ------------------------------------------------------------
    conf = sub.add_parser("config", help="설정 파일 관리")
    conf.add_argument("action", nargs="?", choices=["init", "show", "path"], default="show")
    conf.add_argument("-f", "--force", action="store_true", help="덮어쓰기")
    conf.set_defaults(func=cmd_config)

    # logs --------------------------------------------------------------
    logs = sub.add_parser("logs", help="최근 터널 로그 보기")
    logs.add_argument("-n", "--lines", type=int, default=50, help="출력할 줄 수")
    logs.set_defaults(func=cmd_logs)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    util.setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    _ORIGINAL_ARGV[:] = argv

    # `fastunnel 8000-8005` 처럼 서브커맨드를 생략하면 run 으로 취급한다.
    if argv and not argv[0].startswith("-") and argv[0] not in SUBCOMMANDS:
        argv.insert(0, "run")

    parser = build_parser()
    args = parser.parse_args(argv)

    util.set_verbosity(getattr(args, "verbose", False), getattr(args, "quiet", False))

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    # run 의 위치 인자 포트를 --ports 로 합친다.
    if args.command == "run":
        if getattr(args, "ports_pos", None) and not args.ports:
            args.ports = args.ports_pos

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
