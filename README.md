# fastunnel

**cloudflared / ngrok** 으로 원하는 **포트 범위**를 한 번에 터널링하고,
**방화벽 설정**과 **디스코드 알림**까지 자동으로 처리하는 CLI 도구입니다.

```
python -m fastunnel 8000-8005
```

한 줄이면 끝납니다. 터널 실행 파일이 없으면 알아서 내려받고, 방화벽 인바운드/아웃바운드를
열고, 발급된 임시 도메인 주소를 디스코드로 보내고, `Ctrl+C` 로 끄면 방화벽 규칙까지 원래대로 되돌립니다.

- 파이썬 **표준 라이브러리만** 사용합니다. `pip install` 이 필요 없습니다.
- 윈도우 / 리눅스 / macOS 에서 같은 명령으로 동작합니다.
- 클라우드플레어 quick tunnel 은 **계정도 토큰도 필요 없습니다.**
- 명령어가 싫다면 `fastunnel-gui.cmd` 로 **GUI**를 쓰면 됩니다.

> **개발에 참여하거나 코드를 이어서 손보려면** — [`CLAUDE.md`](CLAUDE.md) (개발 규칙과 함정),
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (설계 배경),
> [`docs/HANDOFF.md`](docs/HANDOFF.md) (현재 상태·검증 결과·남은 일),
> [`tests/README.md`](tests/README.md) (테스트 실행법)

---

## 1. 준비

Python 3.8 이상만 있으면 됩니다.

```bash
python --version     # 3.8+
```

이 폴더를 받아서 바로 실행하세요.

```powershell
# Windows (PowerShell)
cd C:\fast-turneling
python -m fastunnel doctor
```

```bash
# Linux / macOS
cd ~/fast-turneling
python3 -m fastunnel doctor
```

`doctor` 는 OS, 권한, 터널 실행 파일, 방화벽 백엔드, 알림 설정, 네트워크 연결을 한 번에 점검합니다.

편의를 위한 실행 래퍼도 함께 들어 있습니다.

```powershell
.\fastunnel.ps1 8000-8005                 # 일반 실행
.\fastunnel.ps1 -Elevated 8000-8005       # 관리자 권한 새 창으로 실행
```

```bash
./fastunnel.sh 8000-8005
sudo -E ./fastunnel.sh 8000-8005          # 방화벽까지 자동 설정
```

---

## 2. GUI로 관리하기 (윈도우 권장)

명령어가 번거롭다면 그래픽 화면으로 관리할 수 있습니다. tkinter(파이썬 기본 포함)만 쓰므로
따로 설치할 게 없습니다.

```powershell
fastunnel-gui.cmd            # 더블클릭해도 됩니다 (콘솔 창 없이 실행)
python -m fastunnel gui      # 콘솔에서 실행
```

```bash
./fastunnel-gui.sh           # Linux / macOS
```

화면 구성

| 영역 | 하는 일 |
|---|---|
| **터널 설정** | 포트 범위 · 프로토콜 · 제공자 · 업데이트 방식 |
| **옵션** | 방화벽 자동 설정(방향·유지 여부), 알림 켜기, 알림/고급 설정 대화상자 |
| **버튼 줄** | 시작 / 중지 / 환경 진단 / 방화벽 정리 / 설정 저장 / 관리자 권한으로 다시 실행 |
| **열린 터널** | 포트별 상태와 공개 주소. 더블클릭이나 우클릭으로 주소·접속 명령 복사, 브라우저 열기 |
| **로그** | 진행 상황이 실시간으로 쌓입니다. 복사·폴더 열기·상세 로그 토글 |
| **상태줄** | 현재 권한, 방화벽 백엔드, 연결된 터널 수 |

포인트

- 오른쪽 아래 상태줄에 **현재 권한과 방화벽 백엔드**가 항상 보입니다.
- 방화벽을 켠 채 권한이 없으면 시작할 때 **관리자 권한으로 다시 실행 / 방화벽 없이 계속 / 취소**
  중에서 고르는 창이 뜹니다. 그냥 멈추지 않습니다.
- 창을 닫을 때 터널이 실행 중이면 확인을 거쳐 **터널 종료 → 방화벽 원복**까지 마친 뒤 닫힙니다.
- 종료 시 화면의 설정을 `~/.fastunnel/config.json` 에 저장하므로 다음에 열면 그대로 이어집니다.
- 실행 중에는 시작 버튼과 포트 입력이 잠깁니다. 중지 후 다시 바꿀 수 있습니다.

### GUI가 예외 상황을 처리하는 방식

| 상황 | 동작 |
|---|---|
| 포트를 잘못 입력 (`abc`, `70000`, 범위 과다) | 시작하지 않고 무엇이 잘못됐는지 안내, 입력칸으로 포커스 이동 |
| 관리자/root 권한 없음 | 3지선다 창 → 권한 상승 재실행 / 방화벽 없이 계속 / 취소 |
| 실행 파일 다운로드 실패 (네트워크·프록시·디스크) | 오류 창 + 해결 힌트, 시작 버튼 원상복구 |
| 지정한 실행 파일 경로가 없음 | 경로를 알려 주고 대안(자동 다운로드) 안내 |
| ngrok 토큰 없음 | 토큰 발급 주소와 설정 방법 안내 |
| 터널 프로세스가 죽음 | 자동 재시작(최대 5회), 표에 재시작 횟수 표시, 새 주소로 다시 알림 |
| 주소 확보 시간 초과 | 열린 것만 표에 표시하고 나머지는 대기 상태로 남김 |
| 알림 전송 실패 | 로그에 원인(401/403/404 등)만 남기고 터널은 계속 유지 |
| 작업 스레드에서 예기치 못한 예외 | 오류 창 + `~/.fastunnel/logs/gui-error.log` 기록, UI는 계속 사용 가능 |
| 화면 처리 중 예외 | 같은 방식으로 기록 후 창 유지 (프로그램이 죽지 않음) |
| 이미 다른 창이 실행 중 | PID를 알려 주고 포트 충돌 주의 안내 |
| 비정상 종료로 방화벽 규칙이 남음 | GUI 시작 시 감지해서 정리할지 물어봄 |
| tkinter 없음 | OS별 설치 명령을 알려 주고 CLI 사용을 안내 |

---

## 3. 기본 사용법

```bash
# 8000~8005 + 9000 포트를 TCP로 터널링 (기본값)
python -m fastunnel 8000-8005,9000

# 웹 서버를 HTTPS 주소로 공개
python -m fastunnel run -p 8080 --proto http

# ngrok 사용
python -m fastunnel run -p 25565 --provider ngrok

# 업데이트 확인을 건너뛰고 바로 실행 (가장 빠름)
python -m fastunnel run -p 8000-8010 --update skip

# 방화벽을 건드리지 않고 실행
python -m fastunnel run -p 8000 --no-firewall

# 관리자/sudo 권한으로 다시 띄워 방화벽까지 자동 설정
python -m fastunnel run -p 8000-8005 --elevate

# 30분만 실행하고 자동 종료 (예약 실행/CI에 유용)
python -m fastunnel run -p 8000 --duration 1800
```

포트 지정 형식은 자유롭습니다: `8000` · `8000-8005` · `8000:8005` · `8000-8005,9000,3000-3002`

실행 결과는 이렇게 보입니다.

```
포트  프로토콜  상태      공개 주소
----  --------  --------  ----------------------------------------------
8811  TCP       ● 연결됨  syracuse-tax-bulletin-arthur.trycloudflare.com
8812  TCP       ● 연결됨  definition-res-roles-protect.trycloudflare.com

  접속하는 쪽에서 실행할 명령:
   8811 → cloudflared access tcp --hostname syracuse-tax-bulletin-arthur.trycloudflare.com --url localhost:8811
```

터널 정보는 `~/.fastunnel/state/tunnels.json` 에도 저장됩니다 (`--out` 으로 경로 지정 가능).

---

## 4. 프로토콜 선택: TCP vs HTTP

| | `--proto tcp` (기본) | `--proto http` |
|---|---|---|
| 용도 | 게임 서버, SSH, RDP, DB 등 원시 TCP | 웹 서버, REST API |
| cloudflared | 접속 측에서 `cloudflared access tcp` 실행 필요 | 브라우저로 URL 바로 열기 |
| ngrok | `호스트:포트` 로 바로 접속 | HTTPS URL 바로 열기 |

### 제공자별 TCP 비교

|  | cloudflared | ngrok |
|---|---|---|
| 접속하는 쪽에 도구 필요? | **필요** (`cloudflared access tcp`) | **불필요** (`host:port` 로 바로 접속) |
| 계정 | 불필요 | authtoken 필요 |
| 무료로 TCP 사용 | 가능 | **카드 등록 필요** (`ERR_NGROK_8013`, 청구는 없다고 안내) |

즉 **상대방에게 아무것도 설치시키지 않고 TCP 를 열려면 ngrok + 카드 등록**이 필요하고,
아무 등록 없이 쓰려면 cloudflared 를 쓰되 접속 측에서 `cloudflared access tcp` 를 실행해야 합니다.
웹 서비스라면 `--proto http` 가 가장 편합니다 — 양쪽 다 브라우저만으로 됩니다.

### cloudflared + TCP 로 접속하는 방법

터널을 연 쪽에서 안내되는 명령을 **접속하는 쪽 PC**에서 실행하면 로컬 포트로 연결됩니다.

```bash
cloudflared access tcp --hostname xxxx.trycloudflare.com --url localhost:8811
# 이제 localhost:8811 로 접속하면 원격 서버의 8811 포트에 연결됩니다
```

접속하는 쪽에도 cloudflared 가 필요합니다. 상대방에게 설치가 부담스럽다면
`--proto http` 를 쓰거나 `--provider ngrok` 을 쓰세요 (ngrok TCP는 `호스트:포트` 로 바로 접속).

---

## 5. 터널 실행 파일 자동 관리

별도 설치 과정이 없습니다. 처음 실행할 때 `~/.fastunnel/bin/` 에 자동으로 내려받습니다.
이미 시스템에 설치돼 있으면(PATH 에 있으면) 그것을 그대로 씁니다.

| `--update` | 동작 |
|---|---|
| `auto` (기본) | 하루 한 번 최신 버전 확인 후 필요하면 갱신 |
| `skip` | 확인하지 않고 바로 실행 (가장 빠름) |
| `force` | 무조건 다시 내려받기 |
| `ask` | 새 버전이 있으면 물어보기 |

```bash
python -m fastunnel install            # cloudflared + ngrok 모두 준비
python -m fastunnel install ngrok      # 하나만
python -m fastunnel update             # 강제 재설치
```

> 시스템에 이미 설치된 실행 파일(PATH)은 임의로 갱신하지 않습니다.
> 직접 관리하려면 `--bin <경로>` 로 실행 파일을 지정하세요.

### ngrok 을 쓸 때

ngrok 은 인증 토큰이 필요합니다. https://dashboard.ngrok.com/get-started/your-authtoken

```bash
# 방법 1: 환경 변수
export NGROK_AUTHTOKEN=2abc...        # Windows: $env:NGROK_AUTHTOKEN="2abc..."

# 방법 2: 설정 파일 fastunnel.json 의 ngrok_authtoken

# 방법 3: ngrok 에 이미 등록해 뒀다면 그대로 사용됨
ngrok config add-authtoken 2abc...
```

무료 플랜은 동시 엔드포인트 수가 제한되므로 포트를 여러 개 열 때는 cloudflared 쪽이 유리합니다.

---

## 6. 방화벽 자동 설정

기본으로 **켜져 있고**, 종료할 때 **추가한 규칙만** 되돌립니다.

| OS | 사용하는 도구 | 필요한 권한 |
|---|---|---|
| Windows | `netsh advfirewall` (인바운드 + 아웃바운드) | 관리자 |
| Linux (ufw) | `ufw allow` / `ufw allow out` | root 또는 무암호 sudo |
| Linux (firewalld) | `firewall-cmd --add-port` (런타임) | root / sudo |
| Linux (기타) | `iptables -I INPUT/OUTPUT` | root / sudo |
| macOS | 별도 설정 없음 (기본 정책상 불필요) | — |

연속된 포트는 하나의 범위 규칙으로 묶습니다. `8000-8010` 은 규칙 2개(in/out)면 충분합니다.

권한이 없으면 **중단하지 않고** 경고와 함께 직접 실행할 명령을 출력합니다.
자동으로 권한을 올리려면 `--elevate` 를 쓰세요 (윈도우는 UAC 창, 리눅스는 sudo 재실행).

```bash
python -m fastunnel run -p 8000-8005 --elevate

python -m fastunnel run -p 8000 --no-firewall             # 방화벽 건드리지 않기
python -m fastunnel run -p 8000 --firewall-direction in   # 인바운드만
python -m fastunnel run -p 8000 --firewall-persist        # 종료 후에도 규칙 유지
```

### 규칙 관리

```bash
python -m fastunnel firewall status              # 현재 등록된 fastunnel 규칙 보기
python -m fastunnel firewall open -p 8000-8005   # 규칙만 열어두기
python -m fastunnel firewall close -p 8000-8005  # 규칙 닫기
python -m fastunnel firewall cleanup             # 비정상 종료로 남은 규칙 전부 정리
python -m fastunnel firewall cleanup --dry-run   # 지울 명령만 확인
```

프로그램이 강제 종료돼 규칙이 남아도 `~/.fastunnel/state/firewall-*.json` 에 기록이 남으므로
`cleanup` 한 번이면 깨끗하게 정리됩니다.

---

## 7. 디스코드 알림

터널이 열리면 임시 도메인 주소를 임베드 카드로 보냅니다. 주소가 바뀌거나 종료될 때도 알립니다.

### 방법 A. 채널 웹훅 (가장 간단, 권장)

디스코드 채널 → **채널 편집 → 연동 → 웹후크 → 새 웹후크** → URL 복사

```bash
python -m fastunnel run -p 8000 --webhook "https://discord.com/api/webhooks/123.../abc..."

# 또는 환경 변수
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python -m fastunnel 8000-8005
```

### 방법 B. 봇 토큰

[디스코드 개발자 포털](https://discord.com/developers/applications)에서 봇을 만들고 서버에 초대한 뒤,
채널 ID(개발자 모드 활성화 → 채널 우클릭 → ID 복사)와 함께 지정합니다.
봇에게 해당 채널의 **메시지 보내기** 권한이 있어야 합니다.

```bash
python -m fastunnel run -p 8000 \
  --bot-token "MTIz...." \
  --channel-id "1234567890"
```

### 알림 테스트 / 기타 옵션

```bash
python -m fastunnel notify test                       # 설정 확인용 테스트 메시지
python -m fastunnel run -p 8000 --mention "@here"     # 멘션 붙이기
python -m fastunnel run -p 8000 --no-notify           # 알림 끄기
```

### 디스코드 봇으로 명령 받기

봇 토큰이 있으면 알림만 받는 게 아니라 **채널에서 명령을 내려** 터널을 조종할 수 있습니다.
서버에 원격 접속하지 않고 디스코드만으로 주소 확인·재시작이 됩니다.

```bash
python -m fastunnel run -p 8000-8005   --discord-control --discord-admin 여러분의_디스코드_사용자_ID
```

또는 설정 파일에:

```json
{
  "discord_bot_token": "봇 토큰",
  "discord_channel_id": "명령을 받을 채널 ID",
  "discord_control": true,
  "discord_prefix": "!ft",
  "discord_admins": ["여러분의 디스코드 사용자 ID"]
}
```

**슬래시 명령을 쓰세요.** `/ft` 를 치면 하위 명령이 자동완성으로 뜹니다.

| 명령 | 하는 일 | 권한 |
|---|---|---|
| `/ft status` | 열려 있는 터널 목록 | 누구나 |
| `/ft url` | 공개 주소만 보기 | 누구나 |
| `/ft restart` | 터널을 다시 열기 (주소가 바뀜) | 서버 관리자 |
| `/ft open proto:tcp port:25565` | 지정한 프로토콜/포트로 열기 | 서버 관리자 |
| `/ft stop` | 터널을 닫고 종료 | 서버 관리자 |

**명령과 그 답장은 친 사람에게만 보입니다.** 채널에는 터널이 열렸다는 알림만 올라갑니다.
명령이 오갈 때마다 채널이 지저분해지지 않게 하려는 것입니다.

접두사 명령(`!ft …`)도 그대로 남아 있지만 **MESSAGE CONTENT INTENT 가 켜져 있어야만** 동작합니다.
꺼져 있으면 봇에게 메시지 내용이 빈 문자열로 오기 때문에 읽을 방법이 없습니다.
슬래시 명령은 그 인텐트가 필요 없습니다.

| 접두사 명령 | 하는 일 | 권한 |
|---|---|---|
| `!ft 상태` | 열려 있는 터널 목록 | 누구나 |
| `!ft 주소` | 공개 주소만 다시 보기 | 누구나 |
| `!ft 재시작` | 터널을 다시 열기 (주소가 바뀜) | 서버 관리자 |
| `!ft 열기 8000-8005` | 지정한 포트로 다시 열기 | 서버 관리자 |
| `!ft 종료` | 터널을 닫고 프로그램 종료 | 서버 관리자 |
| `!ft 도움말` | 명령 목록 | 누구나 |

영어 명령(`status` `url` `restart` `open` `stop` `help`)도 그대로 받습니다.

**누가 관리자인가**: 다음 둘 중 하나면 터널을 여닫을 수 있습니다.

1. **디스코드 서버에서 관리자 권한(Administrator)을 가졌거나 서버 소유자** — 기본으로 허용됩니다.
   따로 ID 를 등록할 필요가 없습니다.
2. `discord_admins` 에 사용자 ID 가 등록된 사람

서버 관리자 자동 허용을 끄려면 `discord_allow_guild_admins: false` (또는 `--no-discord-guild-admins`).
그러면 `discord_admins` 목록만 봅니다.

> 채널에 있는 아무나 이 PC 의 포트를 열 수 있으면 안 되므로, 둘 다 아닌 사람은 조회 명령만 됩니다.
> 자기 ID 를 모르면 아무 명령이나 쳐 보세요. 거부 메시지에 본인 ID 가 같이 나옵니다.

**이미 돌아가는 봇이 있다면** 토큰을 직접 옮겨 적지 말고 도구를 쓰세요.
`.env` 에서 토큰을 찾아 검증하고 설정 파일까지 만들어 줍니다. 토큰은 마스킹해서만 보여 줍니다.

```bash
# 1) 토큰 확인 + 봇이 보이는 채널 목록
python3 tools/discord-setup.py --env ~/내봇/.env

# 2) 채널을 정했으면 저장 (설정은 ~/.fastunnel/config.json, 권한 600)
python3 tools/discord-setup.py --env ~/내봇/.env --channel 채널ID --admin 내사용자ID
```

**봇 준비물**: 봇을 초대할 때 **`applications.commands` 범위**가 들어가야 슬래시 명령을 등록할 수 있습니다.
(초대 링크 만들 때 `bot` 과 `applications.commands` 를 같이 체크) 채널 권한은 메시지 보내기면 됩니다.
접두사 명령까지 쓰려면 [Developer Portal](https://discord.com/developers/applications) 에서
Bot → MESSAGE CONTENT INTENT 를 켜세요.

**동작 방식**: 슬래시 명령은 게이트웨이(WebSocket)로 인터랙션을 받습니다. 표준 라이브러리에
WebSocket 클라이언트가 없어서 `fastunnel/ws.py` 에 필요한 만큼만 직접 구현했습니다.
인텐트는 하나도 요구하지 않습니다(`intents=0`). 접두사 명령은 채널 폴링(기본 3초, `discord_poll`)입니다.
슬래시 명령을 끄려면 `--no-discord-slash`.

---

### 슬랙 · 텔레그램 · 범용 웹훅

`fastunnel.json` 의 `webhooks` 배열에 추가하면 함께 전송됩니다.

```json
{
  "webhooks": [
    { "type": "slack",    "url": "https://hooks.slack.com/services/..." },
    { "type": "telegram", "token": "12345:AA...", "chat_id": "-100123456" },
    { "type": "raw",      "url": "https://my-server/hook", "headers": { "X-Token": "..." } }
  ]
}
```

`raw` 는 아래 형태의 JSON 을 POST 합니다.

```json
{
  "event": "터널 3개 열림",
  "host": "MY-PC",
  "provider": "cloudflared",
  "timestamp": "2026-08-23T13:51:49+00:00",
  "tunnels": [{ "port": 8000, "proto": "tcp", "url": "...", "address": "...", "status": "online" }]
}
```

---

## 8. 설정 파일

매번 옵션을 치기 번거로우면 설정 파일을 만드세요.

```bash
python -m fastunnel config init      # 현재 폴더에 fastunnel.json 생성
python -m fastunnel config show      # 최종 적용값 확인 (비밀값은 마스킹)
python -m fastunnel config path      # 탐색 경로 확인
```

우선순위는 **기본값 < 설정 파일 < 환경 변수 < 명령줄 인자** 입니다.
설정 파일은 `./fastunnel.json` → `~/.fastunnel/config.json` 순으로 찾습니다.

```json
{
  "provider": "cloudflared",
  "ports": "8000-8005,9000",
  "proto": "tcp",
  "update": "auto",

  "firewall": true,
  "firewall_direction": "both",

  "notify": true,
  "discord_webhook": "https://discord.com/api/webhooks/...",
  "mention": "",

  "discord_control": false,
  "discord_prefix": "!ft",
  "discord_admins": [],
  "discord_allow_guild_admins": true,
  "discord_slash": true,

  "start_delay": 1.0,
  "restart": true,
  "max_restarts": 5,
  "dns_wait": 45
}
```

`dns_wait` 는 **주소가 공개 DNS 에 등록될 때까지 기다리는 시간(초)** 입니다. 자세한 이유는
"11. 문제 해결 > 주소는 떴는데 접속이 안 돼요" 를 보세요. `0` 이면 기다리지 않습니다(권장하지 않음).

주요 환경 변수: `FASTUNNEL_PORTS`, `FASTUNNEL_PROVIDER`, `FASTUNNEL_PROTO`, `FASTUNNEL_UPDATE`,
`FASTUNNEL_DNS_WAIT`,
`DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `NGROK_AUTHTOKEN`,
`FASTUNNEL_HOME`(작업 디렉터리 변경)

---

## 9. 명령 전체 목록

| 명령 | 설명 |
|---|---|
| `run` (기본) | 터널 실행. 서브커맨드를 생략하면 `run` 으로 처리됩니다 |
| `gui` | 그래픽 화면으로 관리 (윈도우 권장) |
| `install [provider]` | 터널 실행 파일 설치 |
| `update [provider]` | 강제 재설치 |
| `doctor` | 환경 점검 |
| `firewall {open,close,cleanup,status}` | 방화벽 규칙 관리 |
| `notify test` | 알림 테스트 |
| `config {init,show,path}` | 설정 파일 관리 |
| `logs -n 100` | 최근 터널 로그 보기 |

`run` 주요 옵션:

```
-p, --ports          포트 범위 (예: 8000-8005,9000)
    --provider       cloudflared | ngrok
    --proto          tcp | http | https        (기본 tcp)
    --host           로컬 대상 호스트          (기본 localhost)
    --update         auto | skip | force | ask
    --bin            터널 실행 파일 경로 직접 지정
    --no-firewall / --firewall-direction / --firewall-persist / --elevate
    --no-notify / --webhook / --bot-token / --channel-id / --mention
    --discord-control                          디스코드 봇으로 명령 받기
    --discord-prefix / --discord-admin         명령 접두사 / 관리자 ID(여러 번 가능)
    --no-discord-guild-admins                  디스코드 서버 관리자 자동 허용 끄기
    --no-discord-slash                         슬래시 명령 끄기 (접두사 명령만)
    --authtoken / --region                     (ngrok)
    --start-delay    터널 순차 기동 간격(초)   (기본 1.0)
    --timeout        주소 확보 대기 시간(초)   (기본 90)
    --dns-wait       공개 DNS 등록 대기(초)     (기본 45, 0이면 안 기다림)
    --no-restart     터널이 죽어도 재시작하지 않음
    --duration       지정한 초 후 자동 종료
    --out            터널 정보 JSON 저장 경로
    --extra-args     터널 실행 파일에 추가 인자 전달
    --dry-run        실행하지 않고 계획만 출력
-v, --verbose  /  -q, --quiet  /  -c, --config
```

---

## 10. 동작 방식

```
포트 파싱 → 실행 파일 확보(필요 시 다운로드/업데이트) → 사전 점검
   → 방화벽 규칙 적용 → 터널 프로세스 기동 → 공개 주소 수집
   → 표 출력 + tunnels.json 저장 + 디스코드 알림
   → 감시 루프(주소 변경/프로세스 사망 시 재시작·재알림)
   → Ctrl+C → 터널 종료 → 방화벽 원복 → 종료 알림
```

- **cloudflared**: 포트마다 프로세스를 하나씩 띄우고 출력에서 `*.trycloudflare.com` 주소를 추출합니다.
  동시 생성 제한을 피하려고 기본 1초 간격으로 순차 기동합니다 (`--start-delay 0` 으로 끌 수 있음).
- **ngrok**: 포트 목록으로 설정 파일을 만들어 프로세스 하나만 띄우고,
  로컬 관리 API(`127.0.0.1:4040/api/tunnels`)에서 주소를 읽어옵니다.
- 터널 프로세스가 죽으면 최대 5회까지 백오프를 두고 재시작하며, 새 주소를 다시 알립니다.

디렉터리 구조:

```
~/.fastunnel/
├── bin/              내려받은 cloudflared / ngrok
├── state/            tunnels.json, firewall-*.json, 버전 캐시
└── logs/             터널 실행 로그 (fastunnel logs 로 확인)
```

---

## 11. 문제 해결

**주소가 안 잡혀요**
`python -m fastunnel logs -n 100` 으로 터널 로그를 보세요. 443 아웃바운드가 막혀 있으면 실패합니다.

**주소는 떴는데 접속이 안 돼요 ("사이트에 연결할 수 없음" / NXDOMAIN)**
cloudflared 는 주소를 **DNS 에 등록되기 전에** 먼저 출력합니다(실측 4~10초 차이).
이 틈에 주소를 한 번이라도 조회하면 "그런 주소 없음"이 리졸버에 캐시되어,
등록이 끝난 뒤에도 **몇 분 동안 계속 실패**합니다(실측 90초 이상). 특히 학교·회사망처럼
공용 DNS 서버를 쓰는 환경에서 잘 나타납니다.

fastunnel 은 이걸 막으려고 **주소를 알리기 전에 공개 DNS 등록을 먼저 확인**합니다
(`dns_wait`, 기본 45초). 그래서 화면에 뜬 주소는 바로 열어도 됩니다.

그래도 실패한다면:
- `python -m fastunnel doctor` 의 `주소 등록확인` 항목을 보세요. `불가` 면 DoH 가 막힌 망이라
  등록 확인 없이 고정 시간만 기다립니다. `--dns-wait 60` 처럼 넉넉히 주세요.
- 이미 캐시가 오염됐다면 `ipconfig /flushdns`(윈도우) 후 **1~2분 뒤** 다시 시도하세요.
  상위 DNS 서버가 캐시한 경우엔 시간이 지나야 풀립니다.
- 급하면 터널을 다시 열어 **새 주소**를 받는 게 가장 빠릅니다.

**포트가 여러 개일 때 일부만 열려요**
cloudflared 는 quick tunnel 동시 생성이 많으면 제한될 수 있습니다.
`--start-delay 2` 로 간격을 늘리거나 포트 수를 줄이세요. ngrok 무료 플랜은 엔드포인트 수 제한이 있습니다.

**방화벽 규칙이 안 들어가요**
관리자/root 권한이 필요합니다. `--elevate` 를 쓰거나 관리자 PowerShell / `sudo -E` 로 실행하세요.
리눅스에서 무암호 sudo 가 아니면 자동 적용되지 않고 수동 명령이 출력됩니다.

**강제 종료해서 방화벽 규칙이 남았어요**
`python -m fastunnel firewall cleanup`

**GUI가 안 떠요**
`python -m fastunnel gui` 로 콘솔에서 실행해 보면 원인이 보입니다.
tkinter 가 없다는 메시지가 나오면 리눅스는 `sudo apt install python3-tk`,
윈도우는 python.org 설치본으로 다시 설치하세요(설치 시 'tcl/tk and IDLE' 체크).
그 외 오류는 `~/.fastunnel/logs/gui-error.log` 에 기록됩니다.

**한글이 깨져요**
UTF-8 출력을 자동으로 켜지만, 오래된 콘솔에서는 `chcp 65001` 을 먼저 실행하세요.

**디스코드 알림이 안 와요**
`python -m fastunnel notify test` 로 확인하세요. 웹훅 URL 오타(404), 봇 권한 부족(403)이면 그대로 안내합니다.

---

## 12. 보안상 주의

- 터널을 열면 해당 포트가 **인터넷에 공개**됩니다. 인증 없는 서비스는 열지 마세요.
- 발급된 임시 도메인 주소는 아는 사람이면 누구나 접속할 수 있습니다. 알림 채널 관리에 주의하세요.
- 필요한 포트만, 필요한 시간만 여세요 (`--duration` 활용).
- 웹훅 URL과 봇 토큰은 비밀값입니다. 설정 파일을 저장소에 커밋하지 마세요.

---

## 13. 개발

```bash
python tests/run_all.py                 # 전체 검증 (약 35초, 실제 터널을 엽니다)
python tests/run_all.py --no-network    # 네트워크 없이
python -m compileall -q fastunnel       # 문법 검사
```

테스트는 `FASTUNNEL_HOME` 을 임시 폴더로 바꿔 돌기 때문에 `~/.fastunnel` 을 건드리지 않습니다.
자세한 내용은 [`CLAUDE.md`](CLAUDE.md) 와 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 를 보세요.

## 라이선스

MIT — [`LICENSE`](LICENSE)
