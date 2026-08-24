# CLAUDE.md

이 파일은 이 저장소에서 작업하는 Claude Code(또는 다른 개발자)가 **새 세션에서 맥락 없이 시작해도
바로 일할 수 있도록** 필요한 것만 모아 둔 문서입니다. 사용법은 `README.md`, 설계 배경은
`docs/ARCHITECTURE.md`, 현재 진행 상황은 `docs/HANDOFF.md` 를 보세요.

---

## 이 프로젝트가 하는 일

cloudflared / ngrok 으로 **포트 범위를 한 번에 터널링**하고, **방화벽 설정**과
**디스코드 알림**까지 자동으로 처리하는 도구. CLI 와 GUI 를 모두 제공합니다.

```bash
python -m fastunnel 8000-8005      # CLI
python -m fastunnel gui            # GUI (윈도우는 fastunnel-gui.cmd 더블클릭)
```

---

## 절대 지켜야 할 제약

이 세 가지는 프로젝트의 정체성입니다. 깨뜨리려면 먼저 사용자에게 물어보세요.

1. **표준 라이브러리만 사용한다.** `pip install` 이 필요 없어야 합니다.
   requests·PyYAML·pytest·PySide 등 어떤 외부 패키지도 추가하지 않습니다.
   (YAML 이 필요한 ngrok 설정은 문자열로 직접 만듭니다 → `providers/ngrok.py`)
2. **Python 3.8+ 에서 동작해야 한다.** 개발 환경은 3.10 이지만 3.8 문법 범위를 지킵니다.
   `tomllib`(3.11+), `datetime.UTC`(3.11+), 구조적 패턴 매칭 금지.
3. **사용자 대면 문자열은 전부 한국어.** 로그·오류·GUI·문서 모두. 코드 주석도 한국어입니다.

추가로:
- 크로스 플랫폼(Windows / Linux / macOS). 윈도우가 1순위입니다.
- 실패해도 프로그램이 죽지 않습니다. 특히 **방화벽·알림 실패가 터널을 막아서는 안 됩니다.**

---

## 개발 명령

```bash
python -m fastunnel doctor              # 환경 점검 (제일 먼저 해볼 것)
python -m fastunnel run -p 8000 --dry-run --update skip   # 실행 안 하고 계획만

python tests/run_all.py                 # 전체 테스트 (약 35초, 실제 터널을 엽니다)
python tests/run_all.py --no-network    # 네트워크 없이 (core + notify)
python tests/run_all.py --no-gui        # GUI 제외
python tests/run_all.py core            # 하나만

python -m compileall -q fastunnel       # 문법 검사
```

**테스트는 `FASTUNNEL_HOME` 을 임시 폴더로 바꿔 돌기 때문에 사용자의 `~/.fastunnel` 을 건드리지 않습니다.**
새 기능을 넣으면 `tests/` 에 검증을 같이 추가하세요.

---

## 코드 지도

```
fastunnel/
├── cli.py          명령줄 파서 + 각 서브커맨드. 진입점(main)
├── gui.py          tkinter GUI. FastunnelApp 한 클래스가 화면 전부를 담당
├── runner.py       TunnelSession - CLI/GUI 가 공유하는 실행 엔진 ★핵심
├── config.py       설정 로딩/병합 (기본값 < 파일 < 환경변수 < 인자)
├── binaries.py     cloudflared/ngrok 자동 다운로드·버전확인·업데이트
├── firewall.py     netsh / ufw / firewalld / iptables 규칙 추가·원복
├── notify.py       디스코드 웹훅·봇, 슬랙, 텔레그램, 범용 웹훅
├── control.py      디스코드 봇 명령 수신 (접두사 명령 = 채널 폴링). 권한 판정도 여기
├── slash.py        슬래시 명령 등록/처리 (/fastunnel)
├── gateway.py      디스코드 게이트웨이 클라이언트 (인터랙션 수신)
├── ws.py           최소 WebSocket 클라이언트 (RFC 6455). 게이트웨이 전용
├── publish.py      현재 주소를 고정 URL(gist)에 게시. 클라이언트가 이걸 읽는다
├── util.py         로깅·포트파싱·HTTP·다운로드·권한 등 공용 도구
└── providers/
    ├── base.py         BaseProvider / Tunnel 공통 인터페이스
    ├── cloudflared.py  포트마다 프로세스 1개, stderr 에서 URL 추출
    └── ngrok.py        설정 파일 생성 → 프로세스 1개 → 로컬 API 폴링
```

### 실행 흐름 (CLI·GUI 공통)

```
설정 병합 → TunnelSession(cfg)          # 포트/프로토콜/제공자 검증
   → session.prepare()                  # 실행 파일 확보 + 제공자 생성 + preflight
   → session.start()                    # 방화벽 규칙 적용 + 터널 프로세스 기동
   → session.wait_ready()               # 공개 주소 확보 대기
   → session.notify_started()
   → 반복: session.poll()               # 주소 변경·프로세스 사망 감지, 재시작
   → session.stop()                     # 터널 종료 + 방화벽 원복 + 종료 알림
```

**새 기능은 대부분 `runner.py` 에 넣으면 CLI 와 GUI 양쪽에 자동으로 반영됩니다.**
한쪽에만 로직을 넣지 마세요 (예전에 그래서 버그가 났습니다).

---

## 반드시 알아야 할 함정

실제로 부딪혀서 고친 것들입니다. 모르면 똑같이 당합니다.

### 플랫폼·인코딩

| 함정 | 대응 |
|---|---|
| 윈도우 콘솔에서 한글이 깨짐 | `util.setup_console()` 이 UTF-8 을 강제. `main()` 첫 줄에서 호출 중 |
| **Git Bash 파이프로 실행하면 한글이 깨져 보임** | 코드 문제가 아님. 확인은 **PowerShell** 에서 `[Console]::OutputEncoding` 을 UTF-8 로 두고 하세요 |
| `pythonw.exe`(GUI) 로 돌면 `sys.stdout` 이 **None** → `print()` 가 예외 | 출력은 `util._write()` / `util.progress()` 만 사용. 새 코드에서 `print()` 직접 호출 금지 |
| `.cmd` 파일을 UTF-8 로 저장하면 cmd.exe 가 파싱 실패 | `fastunnel-gui.cmd` 는 **cp949** 로 저장. 주석에 괄호 `()` 넣지 말 것 |
| `os.kill(pid, 0)` 은 윈도우에서 **강제 종료**가 됨 | 생존 확인은 `util.pid_alive()` (OpenProcess 사용) |
| Bash 힙독으로 큰 파일을 쓰면 잘림 | 소스 파일 생성은 Write 도구 사용 |
| **Bash 힙독 안의 `
` 이 실제 개행으로 바뀜** | 파이썬/PS 소스를 힙독으로 패치하지 말 것. Write/Edit 도구를 쓰세요 |
| **`.ps1` 에 BOM 이 없으면 윈도우 파워셸 5.1 이 cp949 로 읽어 한글이 깨지고 파싱 실패** | `client/mc-connect.ps1` 은 **UTF-8 with BOM + CRLF** 로 저장. 편집 후 BOM 유지 확인 |

### 방화벽 (문법은 실기 검증 완료)

| 백엔드 | 범위 표기 | 비고 |
|---|---|---|
| netsh | `localport=8000-8010` | 인바운드/아웃바운드 각각 규칙 1개. 관리자 필요 |
| ufw | `8000:8005/tcp` | 범위는 프로토콜 필수. `ufw allow out` 으로 아웃바운드 |
| firewalld | `8000-8005/tcp` | `--permanent` 안 붙임(재시작하면 자동 원복). 아웃바운드는 안 함 |
| iptables | `--dport 8000:8005` | **`-m comment` 는 `-j ACCEPT` 앞에** 와야 삭제가 매칭됨 |

- 규칙은 `~/.fastunnel/state/firewall-<pid>.json` 에 기록 → 비정상 종료 후 `firewall cleanup` 으로 정리.
- 권한이 없으면 **중단하지 말고** 수동 명령을 출력할 것. 이미 그렇게 되어 있습니다.

### 터널 제공자

- **cloudflared 는 DNS 등록보다 주소를 먼저 출력합니다(실측 4~10초 차이).**
  그 틈에 이름을 조회하면 NXDOMAIN 이 리졸버에 음수 캐시되어 등록 후에도 90초 이상 접속이 막힙니다.
  그래서 `runner.wait_public_dns()` 가 **주소를 알리기 전에** 등록을 확인합니다.
  확인은 반드시 **DoH**(`util.resolve_public`)로 하세요. 로컬 조회로 확인하면 그 조회가 캐시를 오염시킵니다.
  재시작으로 주소가 바뀔 때도 같은 문제가 나므로 `runner.poll()` 에도 같은 대기가 들어 있습니다.
- cloudflared quick tunnel 은 **계정/토큰 불필요**. `--url tcp://...` 도 quick tunnel 로 열립니다.
- **TCP 터널은 접속하는 쪽에서 `cloudflared access tcp --hostname ... --url localhost:N` 을 실행해야** 합니다.
  이 안내 문자열은 `Tunnel.client_hint()` 가 만듭니다.
- cloudflared 는 **로컬 서비스가 없어도 터널을 엽니다.** preflight 는 경고만 하고 진행합니다.
- quick tunnel 은 동시 생성이 많으면 제한될 수 있어 기본 1초 간격으로 순차 기동합니다(`start_delay`).
- ngrok 은 authtoken 필수. 기본 설정 파일(`%LOCALAPPDATA%\ngrok\ngrok.yml` 등)이 있으면 그것도 함께 넘깁니다.
- ngrok 설정은 `version: "2"` 형식으로 씁니다(v3 에이전트가 여전히 지원).

### GUI

- **tkinter 는 스레드 안전하지 않습니다.** 위젯은 메인 스레드에서만 만지고,
  작업 스레드 → GUI 전달은 `self.events` 큐 하나로만 합니다.
- `util.add_sink()` 로 등록한 로그 콜백은 **아무 스레드에서나 불립니다.** 큐에 넣기만 하세요.
- 설정을 다시 읽으면 대화상자에서 방금 입력한 값이 날아갑니다.
  `_current_config()` 는 파일을 재로딩하지 않고 `self.cfg.data` + 화면 값을 병합합니다. 이 구조를 유지하세요.
- 테스트에서 GUI 를 띄울 때는 `withdraw()` 하거나 `attributes("-alpha", 0.0)` 로 감춥니다.
  **사용자 화면을 캡처하지 마세요.** (다른 창이 찍힐 수 있습니다. 레이아웃 확인은 위젯 좌표 측정으로.)
- 모달 대화상자는 테스트에서 `messagebox.*` 를 교체해 자동 응답시킵니다.

### 설정

- `config.from_dict()` 는 **부분 dict 도 허용**해야 합니다(기본값 위에 덮어씀).
  예전에 여기서 `KeyError` 가 났습니다.
- 비밀값(`discord_webhook`, `discord_bot_token`, `ngrok_authtoken`)은 `SECRET_KEYS` 에 등록해
  `masked()` 로만 출력하세요. **로그에 그대로 찍지 마세요.**

---

## 확장하는 법

### 새 터널 제공자 추가

1. `providers/새이름.py` 에 `BaseProvider` 상속 클래스 작성
   (`preflight` / `start` / `poll` / `stop` 구현, `self.tunnels` 채우기)
2. 필요하면 `Tunnel` 을 상속해 `client_hint()` 재정의
3. `providers/__init__.py` 의 `PROVIDERS` 에 등록
4. `binaries.py` 에 다운로드 URL 추가
5. `cli.py` 의 `--provider` choices, GUI 콤보박스 값에 추가

### 디스코드 명령 다루기

- **접두사 명령(`!ft`)은 MESSAGE CONTENT INTENT 가 켜져 있어야만 동작합니다.** 꺼져 있으면
  REST 로 읽어도 `content` 가 빈 문자열로 옵니다(봇 자신의 메시지까지). 코드로 우회할 수 없습니다.
  그래서 기본 경로는 **슬래시 명령**입니다. 인텐트가 필요 없습니다.
- 인터랙션은 게이트웨이(WebSocket)로만 받을 수 있습니다. 인터랙션 엔드포인트 URL 방식은
  고정 공개 주소 + Ed25519 서명 검증이 필요한데 우리 주소는 실행할 때마다 바뀝니다.
  그래서 `ws.py` 에 WebSocket 클라이언트를 직접 만들었습니다(외부 패키지 없음).
  압축·RESUME 은 구현하지 않았습니다. 끊기면 다시 IDENTIFY 합니다.
- 슬래시 명령 권한은 인터랙션에 딸려 오는 `member.permissions` 비트로 봅니다. REST 조회가 필요 없습니다.
- **명령 등록은 `POST …/commands` 로만 하세요. `PUT` 은 절대 쓰지 마세요.**
  PUT 은 그 애플리케이션의 명령 목록을 통째로 교체해서, 같은 봇 토큰을 쓰는 다른 프로그램이
  등록해 둔 명령을 전부 지웁니다. 실제로 그렇게 해서 같은 봇의 `/setup` `/sync` 를 날렸습니다.
- 같은 봇을 다른 프로그램과 나눠 쓰면 **게이트웨이 세션이 둘**이 됩니다. 디스코드는 허용하고
  두 세션 모두 인터랙션을 받습니다. 그래서 `on_interaction` 은 **명령 이름이 `fastunnel` 이 아니면
  즉시 무시**해야 합니다. 안 그러면 남의 명령에 끼어들어 답합니다.
- **터널을 여닫는 명령은 디스코드 서버 관리자(Administrator/소유자) 또는 `discord_admins` 등록자만**
  쓸 수 있습니다. 둘 다 아니면 조회만 됩니다. 채널 멤버 아무나 포트를 열 수 있으면 안 되므로
  이 기준을 더 느슨하게 만들지 마세요.
- 서버 관리자 판정은 REST 3종(`/channels/{id}` → guild_id, `/guilds/{id}` → owner_id,
  `/guilds/{id}/roles` → 권한 비트)으로 합니다. 게이트웨이 이벤트가 아니라 REST 로 받은 메시지에는
  `member` 가 없어서 역할을 따로 조회해야 합니다. 레이트리밋 때문에 결과는 캐시합니다
  (사용자 60초 / 역할 300초).
- **폴링 스레드에서 세션을 여닫지 마세요.** 감시 루프와 부딪힙니다.
  `_RunActions` 가 큐에 넣고 `cmd_run` 의 메인 루프가 처리합니다. 이 구조를 유지하세요.
- 봇 자신이 보낸 메시지는 무시해야 합니다(`/users/@me` 로 자기 ID 확인). 안 그러면 무한 되먹임입니다.
- MESSAGE CONTENT INTENT 가 꺼져 있으면 `content` 가 빈 문자열로 옵니다. 이때는 조용히 무시합니다.

### 새 알림 채널 추가

`notify.py` 의 `_post_extra()` 에 `type` 분기를 추가하고 `channels()` 이름표만 넣으면 됩니다.
설정은 `webhooks` 배열에 `{"type": "...", ...}` 로 들어옵니다.

### 새 설정 항목 추가

`config.py` 의 `DEFAULTS` 에 추가 → 필요하면 `ENV_MAP` / `BOOL_KEYS` / `SECRET_KEYS` 에도 등록
→ `cli.py` 인자, `gui.py` 대화상자, `README.md` 표에 반영.

---

## 코드 스타일

- 주석과 docstring 은 한국어. **무엇을 하는지가 아니라 왜 그렇게 했는지**를 씁니다.
- 문자열 포맷은 `%` 스타일로 통일(기존 코드와 맞추기 위함).
- 예외를 넓게 잡을 때는 `# noqa: BLE001` 과 함께 **왜 삼키는지** 한 줄 남깁니다.
- 사용자에게 보여줄 오류는 `SessionError(메시지, 힌트)` 로 던집니다. 힌트는 "그래서 뭘 하면 되는지".
- 들여쓰기 4칸, 줄 길이 100자 안팎.
