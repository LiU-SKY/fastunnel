# -*- coding: utf-8 -*-
"""디스코드 봇 토큰을 fastunnel 설정으로 옮겨 주는 도구.

이미 돌아가는 봇의 `.env` 에서 토큰을 읽어 `~/.fastunnel/config.json` 에 넣는다.
**토큰을 화면에 찍지 않는다.** 확인용으로 앞뒤 몇 글자만 마스킹해서 보여 준다.

    # 1) 토큰이 살아 있는지 확인하고, 봇이 볼 수 있는 채널 목록 보기
    python3 tools/discord-setup.py --env ~/dictrio-bot/.env

    # 2) 채널을 정했으면 설정 저장
    python3 tools/discord-setup.py --env ~/dictrio-bot/.env --channel 1234567890 --admin 9876543210

표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastunnel import util  # noqa: E402

API = "https://discord.com/api/v10"

# .env 에서 봇 토큰이 들어 있을 만한 이름들 (앞에 있을수록 우선)
TOKEN_KEYS = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_TOKEN",
    "BOT_TOKEN",
    "DISCORD_BOT",
    "TOKEN",
)
CHANNEL_KEYS = ("DISCORD_CHANNEL_ID", "CHANNEL_ID", "DISCORD_CHANNEL")


def read_env(path: str) -> dict:
    """아주 단순한 .env 파서. KEY=VALUE 만 본다."""
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    return values


def looks_like_token(value: str) -> bool:
    """봇 토큰은 점 두 개로 나뉜 긴 문자열이다."""
    return value.count(".") == 2 and len(value) > 50


def pick(values: dict, names, token_like: bool = False) -> tuple:
    """이름 후보를 순서대로 찾는다.

    token_like 는 **토큰을 찾을 때만** 켠다. 예전에 채널 ID 를 찾을 때도 이 휴리스틱이
    돌아서 토큰을 채널 ID 로 집어 화면에 찍은 적이 있다. 그래서 기본은 꺼 둔다.
    """
    for name in names:
        if values.get(name):
            return name, values[name]
    if token_like:
        for key, value in values.items():
            if "TOKEN" in key.upper() and looks_like_token(value):
                return key, value
    return "", ""


def mask(secret: str) -> str:
    if len(secret) <= 12:
        return "*" * len(secret)
    return "%s…%s (%d자)" % (secret[:6], secret[-4:], len(secret))


def api_get(path: str, token: str):
    return util.http_json(API + path, headers={"Authorization": "Bot %s" % token}, timeout=15)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="디스코드 봇 토큰을 fastunnel 설정에 넣습니다.")
    parser.add_argument("--env", default=os.path.expanduser("~/dictrio-bot/.env"),
                        help="봇의 .env 경로 (기본: ~/dictrio-bot/.env)")
    parser.add_argument("--token-key", help=".env 안에서 토큰이 들어 있는 키 이름 (자동 탐지)")
    parser.add_argument("--channel", help="명령/알림을 주고받을 채널 ID")
    parser.add_argument("--admin", action="append", default=[],
                        help="터널을 여닫을 수 있는 사용자 ID (여러 번 지정 가능)")
    parser.add_argument("--prefix", default="!ft", help="명령 접두사 (기본: !ft)")
    parser.add_argument("--out", default=os.path.join(util.home_dir(), "config.json"),
                        help="저장할 설정 파일 경로")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.env):
        util.error("파일이 없습니다: %s" % args.env)
        return 1

    values = read_env(args.env)
    if args.token_key:
        key, token = args.token_key, values.get(args.token_key, "")
    else:
        key, token = pick(values, TOKEN_KEYS, token_like=True)
    if not token:
        util.error("%s 에서 봇 토큰을 찾지 못했습니다." % args.env)
        util.info("찾은 키: %s" % ", ".join(sorted(values)) or "(없음)")
        util.info("--token-key 로 직접 지정하세요.")
        return 1
    util.ok("토큰 발견: %s = %s" % (key, mask(token)))

    # 살아 있는 토큰인지 확인
    status, body = api_get("/users/@me", token)
    if status == 401:
        util.error("토큰이 거부되었습니다(401). 봇 토큰이 맞는지 확인하세요.")
        return 1
    if not (200 <= status < 300) or not isinstance(body, dict):
        util.error("디스코드에 연결하지 못했습니다 (status=%s)" % status)
        return 1
    util.ok("봇 확인: %s (id=%s)" % (body.get("username"), body.get("id")))

    channel = str(args.channel or values.get(pick(values, CHANNEL_KEYS)[0], "") or "").strip()
    # 채널 ID 는 숫자(스노플레이크)다. 아니면 잘못 집은 것이니 절대 쓰지도, 찍지도 않는다.
    if channel and not channel.isdigit():
        util.warn("채널 ID 로 볼 수 없는 값이라 무시합니다. --channel 로 직접 지정하세요.")
        channel = ""

    # 채널을 안 정했으면 후보를 보여 주고 끝낸다
    if not channel:
        util.info("봇이 보이는 채널을 찾는 중 …")
        status, guilds = api_get("/users/@me/guilds", token)
        if not (200 <= status < 300) or not isinstance(guilds, list) or not guilds:
            util.warn("서버 목록을 가져오지 못했습니다. 채널 ID 를 --channel 로 직접 주세요.")
            util.info("  (디스코드에서 개발자 모드를 켜고 채널 우클릭 → 채널 ID 복사)")
            return 1
        for guild in guilds:
            util.info("서버: %s" % guild.get("name"))
            status, channels = api_get("/guilds/%s/channels" % guild.get("id"), token)
            if not isinstance(channels, list):
                continue
            for ch in channels:
                if ch.get("type") == 0:      # 0 = 일반 텍스트 채널
                    print("   %-22s %s" % (ch.get("id"), "#" + str(ch.get("name"))))
        print()
        util.info("채널을 정했으면 --channel <ID> 를 붙여 다시 실행하세요.")
        return 0

    # 채널에 실제로 쓸 수 있는지 확인
    status, ch = api_get("/channels/%s" % channel, token)
    if status == 403:
        util.warn("봇이 그 채널을 볼 수 없습니다. 채널 권한을 확인하세요.")
    elif status == 404:
        util.error("채널을 찾을 수 없습니다: %s" % channel)
        return 1
    elif isinstance(ch, dict) and ch.get("name"):
        util.ok("채널 확인: #%s" % ch.get("name"))

    # 기존 설정을 살려 두고 필요한 키만 덮어쓴다
    data = {}
    if os.path.isfile(args.out):
        loaded = util.read_json_file(args.out, default=None)
        if isinstance(loaded, dict):
            data = loaded
    data.update({
        "notify": True,
        "discord_bot_token": token,
        "discord_channel_id": str(channel),
        "discord_control": True,
        "discord_prefix": args.prefix,
        "discord_admins": [str(a) for a in args.admin] or data.get("discord_admins", []),
    })

    util.ensure_dir(os.path.dirname(args.out))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(args.out, 0o600)          # 토큰이 들어 있으니 본인만 읽게
    except OSError:
        pass
    util.ok("설정 저장: %s" % args.out)

    if not data["discord_admins"]:
        util.warn("관리자가 비어 있습니다. 지금은 조회 명령만 동작합니다.")
        util.info("  채널에서 '%s 재시작' 을 쳐 보면 답장에 본인 ID 가 나옵니다." % args.prefix)
        util.info("  그 ID 로 다시 실행하세요: --channel %s --admin <내ID>" % channel)
    else:
        util.ok("관리자: %s" % ", ".join(data["discord_admins"]))

    print()
    util.info("이제 이렇게 실행하면 됩니다:")
    print("   python3 -m fastunnel run -p 8000-8005 --proto http --no-firewall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
