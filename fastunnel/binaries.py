"""cloudflared / ngrok 실행 파일 자동 확보 및 업데이트.

- ~/.fastunnel/bin 에 내려받아 관리한다 (시스템 설치 불필요).
- 이미 PATH에 설치돼 있으면 그것을 사용한다.
- update 모드: auto(하루 1회 확인 후 갱신) / skip(건너뜀) / force(무조건 재설치) / ask(물어봄)
"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, Optional, Tuple

from . import util

GITHUB_LATEST_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
CLOUDFLARED_DL = "https://github.com/cloudflare/cloudflared/releases/latest/download/%s"
NGROK_DL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-%s-%s.%s"

# (os, arch) -> 릴리스 자산 이름
CLOUDFLARED_ASSETS: Dict[Tuple[str, str], str] = {
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
    ("windows", "386"): "cloudflared-windows-386.exe",
    ("linux", "amd64"): "cloudflared-linux-amd64",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("linux", "arm"): "cloudflared-linux-arm",
    ("linux", "386"): "cloudflared-linux-386",
    ("darwin", "amd64"): "cloudflared-darwin-amd64.tgz",
    ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
}

VERSION_CACHE = "version-cache.json"
CACHE_TTL = 24 * 3600


class BinaryError(RuntimeError):
    pass


def _exe(name: str) -> str:
    return name + ".exe" if util.IS_WINDOWS else name


def managed_path(provider: str) -> str:
    return os.path.join(util.bin_dir(), _exe(provider))


def detect_version(path: str, provider: str) -> str:
    """실행 파일에서 버전 문자열을 추출한다. 실패하면 빈 문자열."""
    args = [path, "--version"] if provider == "cloudflared" else [path, "version"]
    code, out, err = util.run_cmd(args, timeout=20)
    text = ((out or "") + " " + (err or "")).strip()
    if not text:
        return ""
    match = re.search(r"(\d{4}\.\d+\.\d+|\d+\.\d+\.\d+)", text)
    if match:
        return match.group(1)
    return text.splitlines()[0][:40]


def _cache_get(key: str) -> Optional[str]:
    path = os.path.join(util.state_dir(), VERSION_CACHE)
    cache = util.read_json_file(path, default={}) or {}
    entry = cache.get(key)
    if isinstance(entry, dict) and time.time() - entry.get("ts", 0) < CACHE_TTL:
        return entry.get("value")
    return None


def _cache_put(key: str, value: str) -> None:
    path = os.path.join(util.state_dir(), VERSION_CACHE)
    cache = util.read_json_file(path, default={}) or {}
    cache[key] = {"value": value, "ts": time.time()}
    util.write_json_file(path, cache)


def latest_cloudflared_version(use_cache: bool = True) -> str:
    """GitHub 릴리스 태그 조회. 실패해도 예외를 던지지 않는다(오프라인 대비)."""
    if use_cache:
        cached = _cache_get("cloudflared_latest")
        if cached:
            return cached
    status, body = util.http_json(GITHUB_LATEST_API, timeout=10)
    if status == 200 and isinstance(body, dict):
        tag = str(body.get("tag_name") or "").lstrip("v")
        if tag:
            _cache_put("cloudflared_latest", tag)
            return tag
    util.debug("최신 cloudflared 버전 조회 실패 (status=%s)" % status)
    return ""


def _ask(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input("    %s %s " % (question, suffix)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _install_cloudflared() -> str:
    key = (util.os_tag(), util.arch_tag())
    asset = CLOUDFLARED_ASSETS.get(key)
    if not asset:
        raise BinaryError("지원하지 않는 플랫폼입니다: %s/%s" % key)
    dest = managed_path("cloudflared")
    url = CLOUDFLARED_DL % asset
    util.info("cloudflared 내려받는 중 ... (%s)" % asset)
    if asset.endswith(".tgz"):
        archive = os.path.join(util.bin_dir(), asset)
        util.download(url, archive)
        found = util.extract_archive(archive, util.bin_dir(), "cloudflared")
        os.remove(archive)
        if not found:
            raise BinaryError("압축 파일에서 cloudflared를 찾지 못했습니다.")
        if os.path.abspath(found) != os.path.abspath(dest):
            os.replace(found, dest)
    else:
        util.download(url, dest)
    if not util.IS_WINDOWS:
        os.chmod(dest, 0o755)
    return dest


def _install_ngrok() -> str:
    os_tag, arch = util.os_tag(), util.arch_tag()
    ext = "tgz" if os_tag == "linux" else "zip"
    url = NGROK_DL % (os_tag, arch, ext)
    archive = os.path.join(util.bin_dir(), "ngrok-download.%s" % ext)
    util.info("ngrok 내려받는 중 ... (%s-%s)" % (os_tag, arch))
    util.download(url, archive)
    found = util.extract_archive(archive, util.bin_dir(), "ngrok")
    try:
        os.remove(archive)
    except OSError:
        pass
    if not found:
        raise BinaryError("압축 파일에서 ngrok을 찾지 못했습니다.")
    dest = managed_path("ngrok")
    if os.path.abspath(found) != os.path.abspath(dest):
        os.replace(found, dest)
    if not util.IS_WINDOWS:
        os.chmod(dest, 0o755)
    return dest


def install(provider: str) -> str:
    if provider == "cloudflared":
        return _install_cloudflared()
    if provider == "ngrok":
        return _install_ngrok()
    raise BinaryError("알 수 없는 제공자: %s" % provider)


def locate(provider: str, bin_path: str = "") -> Optional[str]:
    """이미 존재하는 실행 파일 경로를 찾는다: 명시 경로 -> 관리 디렉터리 -> PATH"""
    if bin_path:
        candidate = os.path.abspath(os.path.expanduser(bin_path))
        return candidate if os.path.isfile(candidate) else None
    managed = managed_path(provider)
    if os.path.isfile(managed):
        return managed
    return util.which(provider)


def ensure(provider: str, update: str = "auto", bin_path: str = "") -> Tuple[str, str]:
    """실행 파일을 확보하고 (경로, 버전)을 반환한다."""
    update = (update or "auto").lower()
    path = locate(provider, bin_path)

    if path is None:
        if bin_path:
            raise BinaryError("지정한 실행 파일이 없습니다: %s" % bin_path)
        util.info("%s 실행 파일이 없어 새로 설치합니다." % provider)
        path = install(provider)
        version = detect_version(path, provider)
        util.ok("%s 설치 완료 %s" % (provider, util.dim("(%s)" % (version or "버전 미확인"))))
        return path, version

    version = detect_version(path, provider)
    is_managed = os.path.abspath(path) == os.path.abspath(managed_path(provider))
    util.debug(
        "%s 발견: %s (%s, %s)"
        % (provider, path, version or "?", "관리됨" if is_managed else "시스템")
    )

    if update == "force":
        util.info("%s 강제 재설치 ..." % provider)
        path = install(provider)
        return path, detect_version(path, provider)

    if update == "skip":
        util.info(
            "%s %s %s" % (provider, util.dim(version or ""), util.dim("(업데이트 확인 건너뜀)"))
        )
        return path, version

    # auto / ask
    if not is_managed and not bin_path:
        # 시스템에 설치된 바이너리는 임의로 갱신하지 않는다.
        util.info("%s %s %s" % (provider, util.dim(version or ""), util.dim("(시스템 설치본 사용)")))
        return path, version

    if provider == "cloudflared":
        latest = latest_cloudflared_version()
        if latest and version and latest != version:
            if update == "ask" and not _ask(
                "cloudflared %s -> %s 업데이트할까요?" % (version, latest), True
            ):
                util.info("업데이트를 건너뜁니다.")
                return path, version
            util.info("cloudflared 업데이트: %s -> %s" % (version or "?", latest))
            path = install(provider)
            version = detect_version(path, provider)
            util.ok("업데이트 완료 (%s)" % version)
        else:
            util.info("cloudflared %s %s" % (util.dim(version or "?"), util.dim("(최신)")))
    else:
        # ngrok은 자체 업데이트 명령을 제공한다.
        if update == "ask" and not _ask("ngrok 업데이트를 확인할까요?", False):
            return path, version
        if _cache_get("ngrok_checked") is None:
            code, out, err = util.run_cmd([path, "update"], timeout=120)
            _cache_put("ngrok_checked", "1")
            message = (out or err or "").splitlines()
            if code == 0:
                util.info("ngrok 업데이트 확인 완료 %s" % util.dim(message[-1] if message else ""))
                version = detect_version(path, provider)
            else:
                util.debug("ngrok update 실패(무시): %s" % (err or out))
        else:
            util.info("ngrok %s %s" % (util.dim(version or "?"), util.dim("(최근 확인됨)")))

    return path, version
