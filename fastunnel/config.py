"""설정 로딩: 기본값 < 설정 파일 < 환경 변수 < CLI 인자."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import util

CONFIG_FILENAME = "fastunnel.json"

DEFAULTS: Dict[str, Any] = {
    # 터널
    "provider": "cloudflared",       # cloudflared | ngrok
    "ports": "",                     # "8000-8005,9000"
    "proto": "tcp",                  # tcp | http | https
    "host": "localhost",             # 터널이 연결할 로컬 호스트
    "start_delay": 1.0,              # 프로세스 순차 기동 간격(초) - 레이트리밋 회피
    "ready_timeout": 90,             # 모든 터널 주소 확보 대기(초)
    "dns_wait": 45,                  # 주소가 공개 DNS 에 등록될 때까지 대기(초). 0이면 끔
    "restart": True,                 # 터널 프로세스가 죽으면 재시작
    "max_restarts": 5,
    # 바이너리
    "update": "auto",                # auto | skip | force | ask
    "bin_path": "",                  # 직접 지정한 실행 파일 경로
    "extra_args": "",                # 터널 실행 파일에 그대로 전달할 추가 인자
    # 방화벽
    "firewall": True,
    "firewall_direction": "both",    # in | out | both
    "firewall_persist": False,       # True면 종료 시 규칙을 지우지 않음
    # 알림
    "notify": True,
    "discord_webhook": "",
    "discord_bot_token": "",
    "discord_channel_id": "",
    "mention": "",                   # 예: "<@&1234567890>" 또는 "@here"
    "webhooks": [],                  # [{"type":"slack","url":...}, ...]
    "notify_on_change": True,
    # 디스코드 봇으로 명령 받기 (discord_bot_token + discord_channel_id 필요)
    "discord_control": False,
    "discord_prefix": "!ft",         # 명령 접두사
    "discord_admins": [],            # 터널을 여닫을 수 있는 사용자 ID 목록
    "discord_poll": 3,               # 채널 확인 간격(초)
    # ngrok 전용
    "ngrok_authtoken": "",
    "ngrok_region": "",
}

# 환경 변수 → 설정 키 매핑
ENV_MAP = {
    "FASTUNNEL_PROVIDER": "provider",
    "FASTUNNEL_PORTS": "ports",
    "FASTUNNEL_PROTO": "proto",
    "FASTUNNEL_HOST": "host",
    "FASTUNNEL_UPDATE": "update",
    "FASTUNNEL_BIN": "bin_path",
    "FASTUNNEL_DNS_WAIT": "dns_wait",
    "FASTUNNEL_DISCORD_WEBHOOK": "discord_webhook",
    "DISCORD_WEBHOOK_URL": "discord_webhook",
    "FASTUNNEL_DISCORD_BOT_TOKEN": "discord_bot_token",
    "DISCORD_BOT_TOKEN": "discord_bot_token",
    "FASTUNNEL_DISCORD_CHANNEL_ID": "discord_channel_id",
    "DISCORD_CHANNEL_ID": "discord_channel_id",
    "FASTUNNEL_DISCORD_CONTROL": "discord_control",
    "FASTUNNEL_DISCORD_PREFIX": "discord_prefix",
    "FASTUNNEL_DISCORD_ADMINS": "discord_admins",
    "FASTUNNEL_MENTION": "mention",
    "NGROK_AUTHTOKEN": "ngrok_authtoken",
    "FASTUNNEL_NGROK_REGION": "ngrok_region",
}

BOOL_KEYS = {
    "restart",
    "firewall",
    "firewall_persist",
    "notify",
    "notify_on_change",
    "discord_control",
}

SECRET_KEYS = {"discord_webhook", "discord_bot_token", "ngrok_authtoken"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def config_search_paths(explicit: Optional[str] = None) -> List[str]:
    """탐색 순서: 명시 경로 → 현재 디렉터리 → ~/.fastunnel/config.json"""
    paths = []
    if explicit:
        paths.append(os.path.abspath(os.path.expanduser(explicit)))
    else:
        paths.append(os.path.join(os.getcwd(), CONFIG_FILENAME))
        paths.append(os.path.join(util.home_dir(), "config.json"))
    return paths


class Config:
    def __init__(self, data: Dict[str, Any], sources: List[str]):
        self.data = data
        self.sources = sources

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def get(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key, DEFAULTS.get(key, default))
        return default if value is None else value

    def bool(self, key: str) -> bool:
        return _as_bool(self.get(key, False))

    def masked(self) -> Dict[str, Any]:
        out = {}
        for key, value in sorted(self.data.items()):
            if key in SECRET_KEYS and value:
                text = str(value)
                out[key] = text[:8] + "…" + text[-4:] if len(text) > 16 else "***"
            else:
                out[key] = value
        return out


def load(cli_args: Optional[Dict[str, Any]] = None, config_path: Optional[str] = None) -> Config:
    data: Dict[str, Any] = dict(DEFAULTS)
    sources: List[str] = ["기본값"]

    for path in config_search_paths(config_path):
        if os.path.isfile(path):
            loaded = util.read_json_file(path, default=None)
            if isinstance(loaded, dict):
                # 알 수 없는 키도 그대로 보존한다(향후 확장용).
                data.update(loaded)
                sources.append(path)
            else:
                util.warn("설정 파일을 읽을 수 없습니다(JSON 형식 확인): %s" % path)
            break

    env_used = []
    for env_key, cfg_key in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            data[cfg_key] = value
            env_used.append(env_key)
    if env_used:
        sources.append("환경변수(%s)" % ",".join(sorted(set(env_used))))

    if cli_args:
        overrides = {k: v for k, v in cli_args.items() if v is not None}
        if overrides:
            data.update(overrides)
            sources.append("CLI 인자")

    return Config(normalize(data), sources)


def normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """타입을 정리한다. 일부 키만 있거나 사람이 손으로 고친 값이 와도 안전하게 만든다."""
    for key in BOOL_KEYS:
        data[key] = _as_bool(data.get(key, DEFAULTS[key]))

    for key in ("start_delay",):
        try:
            data[key] = float(data.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            data[key] = DEFAULTS[key]
    for key in ("ready_timeout", "max_restarts", "dns_wait", "discord_poll"):
        try:
            data[key] = int(data.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            data[key] = DEFAULTS[key]

    if not isinstance(data.get("webhooks"), list):
        data["webhooks"] = []

    # 환경 변수로 넘기면 문자열이라 "111,222" 형태도 받아 준다.
    admins = data.get("discord_admins", [])
    if isinstance(admins, str):
        admins = [part for part in admins.replace(";", ",").split(",")]
    if not isinstance(admins, (list, tuple)):
        admins = []
    data["discord_admins"] = [str(x).strip() for x in admins if str(x).strip()]
    return data


def from_dict(data: Dict[str, Any], sources: Optional[List[str]] = None) -> Config:
    """값 묶음으로 Config 를 만든다 (GUI 가 화면 값과 합칠 때, 테스트에서 사용).

    일부 키만 넘겨도 되도록 기본값 위에 덮어쓴다.
    """
    merged = dict(DEFAULTS)
    merged.update(data)
    return Config(normalize(merged), list(sources or ["직접 지정"]))


EXAMPLE_CONFIG = {
    "_help": {
        "discord_webhook": "디스코드 채널 > 설정 > 연동 > 웹후크에서 만든 URL을 붙여넣으세요.",
        "discord_bot_token": "봇으로 보내려면 토큰과 채널 ID를 함께 채우세요.",
        "webhooks": '예: [{"type": "slack", "url": "https://hooks.slack.com/..."}]',
        "proto": "tcp | http | https",
        "update": "auto | skip | force | ask",
        "dns_wait": "주소가 공개 DNS에 등록될 때까지 기다리는 초. 0이면 기다리지 않습니다.",
        "discord_control": "디스코드 봇으로 명령을 받으려면 true. 봇 토큰과 채널 ID가 필요합니다.",
        "discord_admins": "터널을 여닫을 수 있는 디스코드 사용자 ID 목록. 예: [\"123456789\"]",
    },
    "provider": "cloudflared",
    "ports": "8000-8005,9000",
    "proto": "tcp",
    "update": "auto",
    "dns_wait": 45,
    "firewall": True,
    "firewall_direction": "both",
    "notify": True,
    "discord_webhook": "",
    "discord_bot_token": "",
    "discord_channel_id": "",
    "mention": "",
    "webhooks": [],
    "discord_control": False,
    "discord_prefix": "!ft",
    "discord_admins": [],
    "ngrok_authtoken": "",
}


def write_example(path: str, force: bool = False) -> str:
    if os.path.exists(path) and not force:
        raise FileExistsError(path)
    util.write_json_file(path, EXAMPLE_CONFIG)
    return path
