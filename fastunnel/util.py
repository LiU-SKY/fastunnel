"""공용 유틸리티: 로깅 / 포트 파싱 / HTTP / 다운로드 / 권한 확인.

표준 라이브러리만 사용한다 (외부 의존성 없음).
"""
from __future__ import annotations

import ctypes
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.parse
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


# --------------------------------------------------------------------------
# 공개 DNS / 도달성 확인
#
# cloudflared 는 quick tunnel 주소를 "DNS 에 등록되기 전에" 먼저 출력한다(실측 4~10초 차이).
# 그 틈에 누군가 이름을 조회하면 NXDOMAIN 이 리졸버에 음수 캐시되어, 정작 등록이 끝난 뒤에도
# 몇 분 동안 "그런 주소 없음"이 계속 돌아온다(실측 90초 이상 지속). 사용자 입장에서는
# "주소는 떴는데 접속이 안 되는" 상태다.
#
# 그래서 등록 여부는 로컬 리졸버가 아니라 DoH(DNS over HTTPS)로 확인한다.
# 로컬 캐시를 건드리지 않아야 나중에 브라우저가 조회할 때 깨끗하게 성공한다.
# --------------------------------------------------------------------------

DOH_ENDPOINTS = (
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
)

# DoH 도 막힌 망에서 확인 없이 그냥 넘어가기 전에 기다려 주는 시간(초).
# 실측 등록 시간이 4~10초라 넉넉히 잡았다.
DNS_BLIND_GRACE = 12.0


def resolve_public(host: str, timeout: int = 8) -> Tuple[str, List[str]]:
    """DoH 로 A 레코드를 조회한다. 로컬 리졸버/캐시는 건드리지 않는다.

    반환: ("ok", IP목록) | ("missing", []) | ("unknown", [])  ← unknown 은 DoH 자체가 막힌 경우
    """
    verdict = "unknown"
    query = urllib.parse.quote(host, safe="")
    for base in DOH_ENDPOINTS:
        status, body = http_request(
            "%s?name=%s&type=A" % (base, query),
            headers={"accept": "application/dns-json"},
            timeout=timeout,
        )
        if status != 200:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        answers = [
            str(item.get("data") or "")
            for item in (data.get("Answer") or [])
            if isinstance(item, dict) and item.get("type") == 1
        ]
        answers = [ip for ip in answers if ip]
        if answers:
            return "ok", answers
        verdict = "missing"      # 응답은 왔는데 A 레코드가 없다 = 아직 등록 전
    return verdict, []


def local_resolves(host: str) -> bool:
    """로컬 리졸버로 이름이 풀리는지. (주의: 실패하면 NXDOMAIN 이 캐시될 수 있다)"""
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def wait_public_dns(host: str, timeout: float = 45.0, tick: float = 2.0) -> str:
    """주소가 공개 DNS 에 등록될 때까지 기다린다.

    반환: "ok" | "timeout" | "unknown"(DoH 가 막혀 확인 불가)
    """
    started = time.time()
    deadline = started + max(0.0, timeout)
    while True:
        verdict, _ips = resolve_public(host)
        if verdict == "ok":
            return "ok"
        if verdict == "unknown":
            # 확인할 방법이 없다. 로컬 조회는 캐시를 오염시키므로 하지 않고,
            # 등록에 걸리는 만큼만 기다렸다가 그냥 진행한다.
            rest = min(DNS_BLIND_GRACE, timeout) - (time.time() - started)
            if rest > 0:
                time.sleep(rest)
            return "unknown"
        if time.time() >= deadline:
            return "timeout"
        time.sleep(tick)


def _get_via_ip(ip: str, host: str, path: str, secure: bool, timeout: int) -> int:
    """IP 로 직접 붙되 SNI 와 Host 헤더는 원래 이름으로 보낸다."""
    if secure:
        context = ssl.create_default_context()
        raw = socket.create_connection((ip, 443), timeout=timeout)
        try:
            wrapped = context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        conn = http.client.HTTPSConnection(ip, 443, timeout=timeout)
        conn.sock = wrapped
    else:
        conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
    try:
        conn.request("GET", path or "/", headers={"Host": host, "User-Agent": USER_AGENT})
        resp = conn.getresponse()
        resp.read(512)
        return resp.status
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 닫기 실패는 결과에 영향이 없다
            pass


def probe_url(url: str, timeout: int = 20) -> Tuple[int, str]:
    """공개 주소에 실제로 접속해 본다. (HTTP 상태, 경로 설명) 반환. 실패는 (0, 사유).

    로컬 리졸버가 이미 NXDOMAIN 을 캐시해 버린 환경에서도 판정이 되도록,
    이름 조회에 실패하면 DoH 로 IP 를 얻어 SNI/Host 를 붙여 직접 접속한다.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return 0, "주소를 해석할 수 없습니다"

    if local_resolves(host):
        status, body = http_request(url, timeout=timeout)
        if status:
            return status, "이름 조회"
        return 0, body[:120]

    verdict, ips = resolve_public(host)
    if verdict != "ok":
        return 0, "공개 DNS 에 아직 등록되지 않았습니다" if verdict == "missing" else "이름을 조회할 수 없습니다"

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    secure = parts.scheme != "http"
    last = ""
    for ip in ips[:3]:
        try:
            return _get_via_ip(ip, host, path, secure, timeout), "%s 직결(로컬 DNS 우회)" % ip
        except Exception as exc:  # noqa: BLE001 - 다음 IP 로 넘어간다
            last = str(exc)[:120]
    return 0, last or "접속 실패"


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
