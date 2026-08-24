"""방화벽 자동 설정.

- 윈도우: netsh advfirewall 로 인바운드/아웃바운드 규칙 추가·삭제
- 리눅스: ufw -> firewalld -> iptables 순으로 사용 가능한 백엔드를 자동 선택
- 연속된 포트는 하나의 범위 규칙으로 묶어 규칙 수를 줄인다
- 추가한 규칙만 상태 파일에 기록해 두었다가 종료 시 그대로 되돌린다
  (비정상 종료 시에는 `fastunnel firewall cleanup` 으로 정리)
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import util

RULE_PREFIX = "fastunnel"


class Rule:
    """추가된 방화벽 규칙 하나. 되돌리기 위한 명령을 함께 들고 있다."""

    def __init__(self, describe: str, undo: Sequence[str], backend: str):
        self.describe = describe
        self.undo = list(undo)
        self.backend = backend

    def to_dict(self) -> Dict[str, object]:
        return {"describe": self.describe, "undo": self.undo, "backend": self.backend}

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "Rule":
        return Rule(
            str(data.get("describe", "")),
            [str(x) for x in (data.get("undo") or [])],
            str(data.get("backend", "")),
        )


def _state_path(tag: str) -> str:
    return os.path.join(util.state_dir(), "firewall-%s.json" % tag)


def detect_backend() -> str:
    """사용할 방화벽 백엔드 이름을 반환한다."""
    if util.IS_WINDOWS:
        return "netsh"
    if util.IS_MAC:
        return "none"
    if util.which("ufw"):
        code, out, _ = util.run_cmd(_prefix() + ["ufw", "status"], timeout=10)
        # 로케일에 따라 문구가 달라지므로 'inactive' 만 확인한다.
        if code == 0 and "inactive" in out.lower():
            return "ufw-inactive"
        return "ufw"
    if util.which("firewall-cmd"):
        code, out, _ = util.run_cmd(["firewall-cmd", "--state"], timeout=10)
        if code == 0 and "running" in out.lower():
            return "firewalld"
        return "firewalld-inactive"
    if util.which("nft") or util.which("iptables"):
        return "iptables"
    return "none"


def _prefix() -> List[str]:
    """권한이 필요한 명령 앞에 붙일 접두사 (root면 없음, 아니면 sudo -n)."""
    if util.IS_WINDOWS or util.is_admin():
        return []
    if util.which("sudo"):
        return ["sudo", "-n"]
    return []


def backend_label(backend: str) -> str:
    return {
        "netsh": "Windows 방화벽 (netsh advfirewall)",
        "ufw": "ufw",
        "ufw-inactive": "ufw (비활성 상태)",
        "firewalld": "firewalld",
        "firewalld-inactive": "firewalld (중지됨)",
        "iptables": "iptables",
        "none": "사용 가능한 방화벽 없음",
    }.get(backend, backend)


class FirewallManager:
    def __init__(
        self,
        ports: Sequence[int],
        proto: str = "tcp",
        direction: str = "both",
        enabled: bool = True,
        persist: bool = False,
        tag: Optional[str] = None,
    ):
        self.ports = list(ports)
        self.proto = "tcp" if proto in ("tcp", "http", "https") else proto
        self.direction = direction if direction in ("in", "out", "both") else "both"
        self.enabled = enabled
        self.persist = persist
        self.tag = tag or str(os.getpid())
        self.backend = detect_backend() if enabled else "none"
        self.applied: List[Rule] = []
        self.manual: List[str] = []

    # ------------------------------------------------------------------
    def directions(self) -> List[str]:
        return ["in", "out"] if self.direction == "both" else [self.direction]

    def _rule_name(self, start: int, end: int, direction: str) -> str:
        span = str(start) if start == end else "%d-%d" % (start, end)
        return "%s %s %s %s" % (RULE_PREFIX, self.proto.upper(), span, direction)

    # ------------------------------------------------------------------
    def plan(self) -> List[Tuple[str, List[str], List[str]]]:
        """(설명, 적용 명령, 되돌리기 명령) 목록을 만든다."""
        prefix = _prefix()
        items: List[Tuple[str, List[str], List[str]]] = []
        for start, end in util.group_ranges(self.ports):
            for direction in self.directions():
                name = self._rule_name(start, end, direction)
                if self.backend == "netsh":
                    span = str(start) if start == end else "%d-%d" % (start, end)
                    add = [
                        "netsh", "advfirewall", "firewall", "add", "rule",
                        "name=%s" % name,
                        "dir=%s" % direction,
                        "action=allow",
                        "protocol=%s" % self.proto.upper(),
                        "localport=%s" % span,
                        "profile=any",
                        "enable=yes",
                    ]
                    remove = [
                        "netsh", "advfirewall", "firewall", "delete", "rule",
                        "name=%s" % name,
                        "dir=%s" % direction,
                        "protocol=%s" % self.proto.upper(),
                        "localport=%s" % span,
                    ]
                elif self.backend == "ufw":
                    span = str(start) if start == end else "%d:%d" % (start, end)
                    target = "%s/%s" % (span, self.proto)
                    if direction == "in":
                        add = prefix + ["ufw", "allow", target]
                        remove = prefix + ["ufw", "delete", "allow", target]
                    else:
                        add = prefix + ["ufw", "allow", "out", target]
                        remove = prefix + ["ufw", "delete", "allow", "out", target]
                elif self.backend == "firewalld":
                    if direction == "out":
                        # firewalld는 기본적으로 아웃바운드를 차단하지 않는다.
                        continue
                    span = str(start) if start == end else "%d-%d" % (start, end)
                    add = prefix + ["firewall-cmd", "--add-port=%s/%s" % (span, self.proto)]
                    remove = prefix + ["firewall-cmd", "--remove-port=%s/%s" % (span, self.proto)]
                elif self.backend == "iptables":
                    span = str(start) if start == end else "%d:%d" % (start, end)
                    if direction == "in":
                        add = prefix + [
                            "iptables", "-I", "INPUT", "-p", self.proto,
                            "--dport", span,
                            "-m", "comment", "--comment", name,
                            "-j", "ACCEPT",
                        ]
                        remove = prefix + [
                            "iptables", "-D", "INPUT", "-p", self.proto,
                            "--dport", span,
                            "-m", "comment", "--comment", name,
                            "-j", "ACCEPT",
                        ]
                    else:
                        add = prefix + [
                            "iptables", "-I", "OUTPUT", "-p", self.proto,
                            "--sport", span,
                            "-m", "comment", "--comment", name,
                            "-j", "ACCEPT",
                        ]
                        remove = prefix + [
                            "iptables", "-D", "OUTPUT", "-p", self.proto,
                            "--sport", span,
                            "-m", "comment", "--comment", name,
                            "-j", "ACCEPT",
                        ]
                else:
                    continue
                items.append((name, add, remove))
        return items

    # ------------------------------------------------------------------
    def open(self) -> bool:
        """규칙을 적용한다. 하나라도 적용됐으면 True."""
        if not self.enabled:
            util.info("방화벽 설정 건너뜀 %s" % util.dim("(--no-firewall)"))
            return False
        if self.backend in ("none", "ufw-inactive", "firewalld-inactive"):
            util.info(
                "방화벽 규칙 불필요 %s" % util.dim("(%s)" % backend_label(self.backend))
            )
            return False

        items = self.plan()
        if not items:
            return False

        if not self._can_modify():
            util.warn(
                "권한이 없어 방화벽을 자동 설정할 수 없습니다. %s"
                % util.dim("(%s 권한 필요)" % ("관리자" if util.IS_WINDOWS else "root/sudo"))
            )
            self._print_manual(items)
            return False

        failed = 0
        for name, add, remove in items:
            code, out, err = util.run_cmd(add, timeout=30)
            if code == 0:
                self.applied.append(Rule(name, remove, self.backend))
                util.debug("방화벽 규칙 추가: %s" % name)
            else:
                failed += 1
                util.debug("방화벽 규칙 실패(%s): %s" % (name, err or out))
                self.manual.append(" ".join(add))
        if self.applied:
            self._save_state()
            util.ok(
                "방화벽 %d개 규칙 적용 %s"
                % (len(self.applied), util.dim("[%s] %s %s" % (
                    backend_label(self.backend),
                    util.format_ranges(self.ports),
                    "/".join(self.directions()),
                )))
            )
        if failed:
            util.warn("방화벽 규칙 %d개는 적용하지 못했습니다. 아래 명령을 직접 실행하세요:" % failed)
            for cmd in self.manual:
                print("      %s" % util.dim(cmd))
        return bool(self.applied)

    def _can_modify(self) -> bool:
        if util.IS_WINDOWS:
            return util.is_admin()
        return util.is_admin() or util.has_sudo()

    def _print_manual(self, items) -> None:
        print(util.dim("    ── 수동 실행용 명령 ──"))
        for _, add, _ in items[:12]:
            print("      %s" % util.dim(" ".join(add)))
        if len(items) > 12:
            print("      %s" % util.dim("... 외 %d개" % (len(items) - 12)))
        if util.IS_WINDOWS:
            print(
                util.dim(
                    "    팁: PowerShell을 '관리자 권한으로 실행' 후 다시 시작하면 자동 적용됩니다."
                )
            )
        else:
            print(util.dim("    팁: sudo 로 다시 실행하면 자동 적용됩니다."))

    # ------------------------------------------------------------------
    def close(self) -> None:
        """추가했던 규칙만 되돌린다."""
        if self.persist:
            if self.applied:
                util.info(
                    "방화벽 규칙을 유지합니다 %s"
                    % util.dim("(정리: fastunnel firewall cleanup)")
                )
            return
        if not self.applied:
            self._clear_state()
            return
        removed = 0
        for rule in self.applied:
            code, out, err = util.run_cmd(rule.undo, timeout=30)
            if code == 0:
                removed += 1
            else:
                util.debug("방화벽 규칙 삭제 실패(%s): %s" % (rule.describe, err or out))
        util.ok("방화벽 규칙 %d/%d개 원복" % (removed, len(self.applied)))
        self.applied = []
        self._clear_state()

    # ------------------------------------------------------------------
    def _save_state(self) -> None:
        util.write_json_file(
            _state_path(self.tag),
            {
                "pid": os.getpid(),
                "created": time.time(),
                "backend": self.backend,
                "ports": util.format_ranges(self.ports),
                "rules": [r.to_dict() for r in self.applied],
            },
        )

    def _clear_state(self) -> None:
        try:
            os.remove(_state_path(self.tag))
        except OSError:
            pass


# ----------------------------------------------------------------------
# 남은 규칙 정리 (비정상 종료 대비)
# ----------------------------------------------------------------------


def list_leftovers() -> List[Tuple[str, Dict[str, object]]]:
    out = []
    directory = util.state_dir()
    for name in sorted(os.listdir(directory)):
        if name.startswith("firewall-") and name.endswith(".json"):
            data = util.read_json_file(os.path.join(directory, name), default=None)
            if isinstance(data, dict):
                out.append((os.path.join(directory, name), data))
    return out


def cleanup(dry_run: bool = False) -> int:
    """이전 실행에서 남은 방화벽 규칙을 모두 제거한다."""
    leftovers = list_leftovers()
    if not leftovers:
        util.ok("정리할 방화벽 규칙이 없습니다.")
        return 0
    total = 0
    for path, data in leftovers:
        rules = [Rule.from_dict(r) for r in (data.get("rules") or [])]
        util.info(
            "PID %s 가 남긴 규칙 %d개 %s"
            % (data.get("pid", "?"), len(rules), util.dim(str(data.get("ports", ""))))
        )
        for rule in rules:
            if dry_run:
                print("      %s" % util.dim(" ".join(rule.undo)))
                continue
            code, out, err = util.run_cmd(rule.undo, timeout=30)
            if code == 0:
                total += 1
            else:
                util.debug("삭제 실패(%s): %s" % (rule.describe, err or out))
        if not dry_run:
            try:
                os.remove(path)
            except OSError:
                pass
    if not dry_run:
        util.ok("규칙 %d개 제거 완료" % total)
    return total


def status() -> str:
    """현재 등록된 fastunnel 규칙을 조회한다."""
    backend = detect_backend()
    lines = ["백엔드: %s" % backend_label(backend)]
    if backend == "netsh":
        code, out, _ = util.run_cmd(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"], timeout=60
        )
        hits = [ln for ln in out.splitlines() if RULE_PREFIX in ln]
        lines.append("등록된 fastunnel 규칙: %d개" % len(hits))
        lines.extend("  " + h.strip() for h in hits[:40])
    elif backend == "ufw":
        code, out, _ = util.run_cmd(_prefix() + ["ufw", "status", "numbered"], timeout=15)
        lines.append(out or "(조회 실패 - 권한 확인)")
    elif backend == "firewalld":
        code, out, _ = util.run_cmd(["firewall-cmd", "--list-ports"], timeout=15)
        lines.append("열린 포트: %s" % (out or "(없음)"))
    elif backend == "iptables":
        code, out, _ = util.run_cmd(
            _prefix() + ["iptables", "-S"], timeout=15
        )
        hits = [ln for ln in out.splitlines() if RULE_PREFIX in ln]
        lines.append("등록된 fastunnel 규칙: %d개" % len(hits))
        lines.extend("  " + h for h in hits[:40])
    leftovers = list_leftovers()
    if leftovers:
        lines.append(
            "정리되지 않은 상태 파일 %d개 (fastunnel firewall cleanup 실행 권장)" % len(leftovers)
        )
    return "\n".join(lines)
