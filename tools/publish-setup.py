# -*- coding: utf-8 -*-
"""주소 게시(gist) 설정 도구.

접속 주소를 알려 줄 **고정 URL** 을 만들기 위한 설정이다. 자세한 배경은
`fastunnel/publish.py` 의 설명을 보라.

토큰을 명령줄 인자로 받지 않는다. 명령줄은 `ps` 나 셸 기록에 남기 때문이다.
직접 입력받아 `~/.fastunnel/config.json`(권한 600)에만 저장하고, 화면에는 마스킹해서 보여 준다.

    python3 tools/publish-setup.py --gist-id <GIST_ID> --owner <깃허브사용자명>

준비물
  1. gist 를 하나 만든다 (secret 도 됨). 주소의 마지막 토막이 GIST_ID 다.
     https://gist.github.com/  →  파일 이름 mc.json, 내용은 {} 로 두면 된다
  2. gist 권한이 있는 토큰
     https://github.com/settings/tokens  →  classic 은 `gist` 체크,
     fine-grained 는 Account permissions → Gists: Read and write
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastunnel import util  # noqa: E402
from fastunnel.publish import GistPublisher  # noqa: E402


def mask(secret: str) -> str:
    if len(secret) <= 12:
        return "*" * len(secret)
    return "%s…%s (%d자)" % (secret[:6], secret[-4:], len(secret))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="접속 주소를 게시할 gist 를 설정합니다.")
    parser.add_argument("--gist-id", required=True, help="gist 주소의 마지막 토막")
    parser.add_argument("--owner", default="", help="깃허브 사용자명 (클라이언트용 URL 을 만들 때 필요)")
    parser.add_argument("--file", default="mc.json", help="gist 안의 파일 이름 (기본 mc.json)")
    parser.add_argument("--token", default="", help="토큰. 비워 두면 직접 입력받습니다(권장)")
    parser.add_argument("--out", default=os.path.join(util.home_dir(), "config.json"),
                        help="저장할 설정 파일 경로")
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        try:
            token = getpass.getpass("gist 권한이 있는 GitHub 토큰을 붙여넣으세요(화면에 안 보임): ")
        except (EOFError, KeyboardInterrupt):
            print()
            util.error("입력이 취소되었습니다.")
            return 1
    token = token.strip()
    if not token:
        util.error("토큰이 비어 있습니다.")
        return 1
    util.ok("토큰 확인: %s" % mask(token))

    data = {}
    if os.path.isfile(args.out):
        loaded = util.read_json_file(args.out, default=None)
        if isinstance(loaded, dict):
            data = loaded
    data.update({
        "publish_gist_id": args.gist_id,
        "publish_gist_token": token,
        "publish_gist_file": args.file,
    })

    # 저장하기 전에 실제로 써 본다. 나중에 조용히 실패하는 것보다 지금 알아야 한다.
    from fastunnel import config as config_mod

    publisher = GistPublisher(config_mod.from_dict(data))
    probe = {"updated": "setup", "online": False, "note": "설정 확인용", "tunnels": []}
    if not publisher.publish(probe):
        util.error("gist 갱신에 실패했습니다. 위 메시지를 확인하세요.")
        return 1
    util.ok("gist 갱신 성공")

    util.ensure_dir(os.path.dirname(args.out))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(args.out, 0o600)
    except OSError:
        pass
    util.ok("설정 저장: %s" % args.out)

    print()
    if args.owner:
        url = publisher.raw_url(args.owner)
        util.info("클라이언트에 넣을 고정 URL:")
        print("   %s" % url)
        print()
        util.info("client/server.txt 첫 줄에 위 URL 을 넣어 배포하세요.")
    else:
        util.warn("--owner 를 주지 않아 클라이언트용 URL 을 만들지 못했습니다.")
        util.info("  형식: https://gist.githubusercontent.com/<사용자명>/%s/raw/%s"
                  % (args.gist_id, args.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
