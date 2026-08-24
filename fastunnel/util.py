"""공용 유틸리티: 로깅 / 포트 파싱 / HTTP / 다운로드 / 권한 확인.

표준 라이브러리만 사용한다 (외부 의존성 없음).
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

APP_NAME = "fastunnel"
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

USER_AGENT = "fastunnel/1.0 (+https://github.com/)"

# --------------------------------------------------------------------------
# 경로
# --------------------------------------------------------------------------


def home_dir() -> str:
    """설정/바이너리/상태 파일이 저장되는 홈 디렉터리."""
    override = os.environ.get("FASTUNNEL_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".fastunnel")


def bin_dir() -> str:
    return ensure_dir(os.path.join(home_dir(), "bin"))


def state_dir() -> str:
    return ensure_dir(os.path.join(home_dir(), "state"))


def log_dir() -> str:
    return ensure_dir(os.path.join(home_dir(), "logs"))


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# 로깅
# --------------------------------------------------------------------------

_VERBOSE = False
_QUIET = False


def set_verbosity(verbose: bool = False, quiet: bool = False) -> None:
    global _VERBOSE, _QUIET
    _VERBOSE = verbose
    _QUIET = quiet


def setup_console() -> None:
    """윈도우 콘솔에서 한글이 깨지지 않도록 UTF-8 출력을 강제한다."""
    if IS_WINDOWS:
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _enable_ansi() -> bool:
    if sys.stdout is None:
        return False
    if not IS_WINDOWS:
        try:
            return bool(sys.stdout.isatty())
        except Exception:  # noqa: BLE001
            return False
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


_ANSI = _enable_ansi() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _ANSI:
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


# 로그를 가로채는 함수들 (GUI가 여기에 등록해서 로그 창에 표시한다).
# 콘솔 없이 pythonw.exe 로 실행되면 sys.stdout 이 None 이므로 출력은 항상 방어적으로 한다.
_SINKS: List[Any] = []


def add_sink(callback: Any) -> None:
    """log(level, message) 형태의 콜백을 등록한다. 스레드 어디서든 호출될 수 있다."""
    if callback not in _SINKS:
        _SINKS.append(callback)


def remove_sink(callback: Any) -> None:
    try:
        _SINKS.remove(callback)
    except ValueError:
        pass


def _emit(level: str, msg: str) -> None:
    for sink in list(_SINKS):
        try:
            sink(level, msg)
        except Exception:  # noqa: BLE001  - 로그 때문에 본 작업이 죽으면 안 된다
            pass


def _write(text: str, stream: Any = None) -> None:
    """stdout/stderr 이 없거나 닫혀 있어도 예외를 내지 않는 출력."""
    target = stream if stream is not None else sys.stdout
    if target is None:
        return
    try:
        target.write(text + "\n")
        target.flush()
    except Exception:  # noqa: BLE001
        pass


def info(msg: str) -> None:
    _emit("info", msg)
    if not _QUIET:
        _write("%s %s %s" % (dim(_stamp()), cyan("[i]"), msg))


def ok(msg: str) -> None:
    _emit("ok", msg)
    if not _QUIET:
        _write("%s %s %s" % (dim(_stamp()), green("[+]"), msg))


def warn(msg: str) -> None:
    _emit("warn", msg)
    _write("%s %s %s" % (dim(_stamp()), yellow("[!]"), msg))


def error(msg: str) -> None:
    _emit("error", msg)
    _write("%s %s %s" % (dim(_stamp()), red("[x]"), msg), sys.stderr)


def debug(msg: str) -> None:
    _emit("debug", msg)
    if _VERBOSE and not _QUIET:
        _write("%s %s %s" % (dim(_stamp()), dim("[d]"), dim(msg)))


def progress(text: str) -> None:
    """같은 줄을 덮어쓰는 진행 표시 (GUI에는 상태줄로 전달)."""
    _emit("progress", text)
    if _QUIET or sys.stdout is None:
        return
    try:
        sys.stdout.write("\r    %s" % text)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def progress_clear(width: int = 60) -> None:
    _emit("progress", "")
    if _QUIET or sys.stdout is None:
        return
    try:
        sys.stdout.write("\r" + " " * width + "\r")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 표 출력 (한글 폭 보정)
# --------------------------------------------------------------------------


def _width(text: str) -> int:
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    cols = len(headers)
    widths = [_width(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], _width(str(row[i])))
    line = "  ".join(_pad(bold(headers[i]), widths[i]) for i in range(cols))
    sep = "  ".join(dim("-" * widths[i]) for i in range(cols))
    out = [line, sep]
    for row in rows:
        out.append("  ".join(_pad(str(row[i]), widths[i]) for i in range(cols)))
    return "\n".join(out)


# --------------------------------------------------------------------------
# 포트 파싱
# --------------------------------------------------------------------------


class PortSpecError(ValueError):
    pass


def parse_ports(spec: str) -> List[int]:
    """"8000-8005,9000 3000:3002" 형태를 정렬된 포트 리스트로 변환."""
    if not spec:
        return []
    ports: List[int] = []
    normalized = spec.replace(" ", ",").replace(";", ",")
    for chunk in normalized.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        token = chunk.replace(":", "-")
        if "-" in token:
            head, _, tail = token.partition("-")
            try:
                start, end = int(head), int(tail)
            except ValueError:
                raise PortSpecError("포트 범위를 해석할 수 없습니다: %r" % chunk)
            if start > end:
                start, end = end, start
            if end - start > 500:
                raise PortSpecError(
                    "포트 범위가 너무 넓습니다 (%d개). 500개 이하로 지정하세요." % (end - start + 1)
                )
            ports.extend(range(start, end + 1))
        else:
            try:
                ports.append(int(token))
            except ValueError:
                raise PortSpecError("포트를 해석할 수 없습니다: %r" % chunk)
    for port in ports:
        if not (1 <= port <= 65535):
            raise PortSpecError("포트 범위를 벗어났습니다: %d" % port)
    return sorted(set(ports))


def group_ranges(ports: Iterable[int]) -> List[Tuple[int, int]]:
    """연속된 포트를 (시작, 끝) 구간으로 묶는다. 방화벽 규칙 수를 줄이기 위함."""
    sorted_ports = sorted(set(ports))
    ranges: List[Tuple[int, int]] = []
    for port in sorted_ports:
        if ranges and port == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], port)
        else:
            ranges.append((port, port))
    return ranges


def format_ranges(ports: Iterable[int]) -> str:
    parts = []
    for start, end in group_ranges(ports):
        parts.append(str(start) if start == end else "%d-%d" % (start, end))
    return ",".join(parts)


def find_free_port(start: int = 4040, limit: int = 50) -> int:
    """start부터 사용 가능한 로컬 포트를 찾는다 (ngrok web API 용)."""
    import socket

    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("사용 가능한 로컬 포트를 찾지 못했습니다.")


def is_port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# 프로세스 / 권한
# --------------------------------------------------------------------------


def run_cmd(
    args: Sequence[str], timeout: int = 60, env: Optional[Dict[str, str]] = None
) -> Tuple[int, str, str]:
    """명령을 실행하고 (returncode, stdout, stderr) 반환. 예외를 던지지 않는다."""
    debug("exec: %s" % " ".join(args))
    try:
        proc = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", "명령을 찾을 수 없습니다: %s" % args[0]
    except subprocess.TimeoutExpired:
        return 124, "", "시간 초과: %s" % " ".join(args)
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)
    decode = lambda b: (b or b"").decode("utf-8", errors="replace").strip()  # noqa: E731
    return proc.returncode, decode(proc.stdout), decode(proc.stderr)


def is_admin() -> bool:
    """윈도우: 관리자 권한 / 리눅스·맥: root 여부."""
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


def has_sudo() -> bool:
    """비밀번호 없이 sudo를 쓸 수 있는지 확인."""
    if IS_WINDOWS or is_admin():
        return False
    if not shutil.which("sudo"):
        return False
    code, _, _ = run_cmd(["sudo", "-n", "true"], timeout=5)
    return code == 0


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def pid_alive(pid: int) -> bool:
    """해당 PID의 프로세스가 살아 있는지 확인한다 (프로세스를 건드리지 않음).

    윈도우에서 os.kill 은 신호가 아니라 강제 종료라서 절대 쓰면 안 된다.
    """
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def http_request(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Tuple[int, str]:
    """(status, body) 반환. 네트워크 오류는 (0, 메시지)."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), body
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Any] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Tuple[int, Any]:
    body = None
    req_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    status, text = http_request(url, method, body, req_headers, timeout)
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


def download(url: str, dest: str, show_progress: bool = True) -> str:
    """URL을 dest 경로로 내려받는다. 임시 파일에 받은 뒤 원자적으로 교체."""
    ensure_dir(os.path.dirname(dest))
    tmp = dest + ".download"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    debug("download: %s" % url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        last_print = 0.0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                read += len(chunk)
                now = time.time()
                if show_progress and now - last_print > 0.2:
                    last_print = now
                    if total:
                        progress(
                            "내려받는 중 %3d%% (%.1f/%.1f MB)"
                            % (read * 100 // total, read / 1048576, total / 1048576)
                        )
                    else:
                        progress("내려받는 중 %.1f MB" % (read / 1048576))
    if show_progress:
        progress_clear()
    if os.path.exists(dest):
        os.remove(dest)
    os.replace(tmp, dest)
    return dest


def extract_archive(archive: str, dest_dir: str, member_hint: str) -> Optional[str]:
    """zip/tgz에서 member_hint 로 시작하는 실행 파일을 추출해 경로를 반환."""
    ensure_dir(dest_dir)
    extracted: Optional[str] = None
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                base = os.path.basename(name)
                if base.lower().startswith(member_hint.lower()) and not name.endswith("/"):
                    target = os.path.join(dest_dir, base)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted = target
                    break
    else:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                base = os.path.basename(member.name)
                if member.isfile() and base.lower().startswith(member_hint.lower()):
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    target = os.path.join(dest_dir, base)
                    with src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted = target
                    break
    if extracted and not IS_WINDOWS:
        os.chmod(extracted, 0o755)
    return extracted


# --------------------------------------------------------------------------
# 상태 파일
# --------------------------------------------------------------------------


def read_json_file(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_file(path: str, data: Any) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def arch_tag() -> str:
    """다운로드 URL에 쓰이는 아키텍처 태그."""
    import platform

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "amd64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("i386", "i686", "x86"):
        return "386"
    if machine.startswith("armv7") or machine.startswith("armv6"):
        return "arm"
    return "amd64"


def os_tag() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_MAC:
        return "darwin"
    return "linux"
