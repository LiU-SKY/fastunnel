# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따릅니다.

## [1.2.1] - 2026-08-24

### 수정

- **슬래시 명령을 등록할 때 같은 봇의 다른 명령을 지우던 문제.** `PUT …/commands` 는
  그 애플리케이션의 명령 목록을 통째로 갈아엎는다. 같은 봇 토큰을 쓰는 다른 프로그램이
  등록해 둔 명령(`/setup`, `/sync` 등)이 전부 사라졌다.
  이제 `POST …/commands` 로 **우리 명령 하나만 추가/갱신**한다. 남의 명령은 건드리지 않는다.

---

## [1.2.0] - 2026-08-24

### 추가

- **슬래시 명령 `/fastunnel`** — `status` / `url` / `restart` / `open ports:…` / `stop` / `help`.
  접두사 명령과 달리 **MESSAGE CONTENT INTENT 가 필요 없다.**
- `ws.py` — RFC 6455 WebSocket 클라이언트를 필요한 만큼만 직접 구현(외부 패키지 없이).
  텍스트 프레임 · 조각 합치기 · ping/pong · 마스킹
- `gateway.py` — 디스코드 게이트웨이 클라이언트. 인터랙션만 받으므로 `intents=0`.
  끊기면 백오프 후 다시 IDENTIFY
- `slash.py` — 명령 등록(서버 범위라 즉시 반영)과 인터랙션 처리.
  권한은 인터랙션에 딸려 오는 `member.permissions` 로 판정해 추가 조회가 없다
- 설정 `discord_slash`(기본 true) / `--no-discord-slash`
- 시작할 때 MESSAGE CONTENT INTENT 가 꺼진 것으로 보이면 경고하고 슬래시 명령을 안내한다

### 배경

`!ft` 접두사 명령이 응답하지 않는 사례가 있었다. 확인해 보니 봇에게 오는 메시지의
`content` 가 전부 빈 문자열이었다(자기 자신이 보낸 메시지까지). MESSAGE CONTENT INTENT 가
꺼져 있으면 REST 로 읽어도 내용이 지워져서 오기 때문이고, 코드로는 우회할 수 없다.
슬래시 명령은 이 인텐트를 요구하지 않아서 근본적인 해결이 된다.

---

## [1.1.0] - 2026-08-24

### 추가

- **디스코드 봇으로 명령 받기** (`control.py`). 채널에서 `!ft 상태` / `주소` / `재시작` /
  `열기 8000-8005` / `종료` 로 터널을 조종한다. 한국어·영어 명령 모두 인식.
  게이트웨이(WebSocket) 대신 채널 폴링을 쓴다 — 표준 라이브러리만 쓰기 위해서다.
- 설정 `discord_control` / `discord_prefix` / `discord_admins` / `discord_poll`,
  인자 `--discord-control` / `--discord-prefix` / `--discord-admin`,
  환경변수 `FASTUNNEL_DISCORD_CONTROL` / `_PREFIX` / `_ADMINS`
- 터널을 여닫는 명령은 **디스코드 서버 관리자**(Administrator 권한 또는 서버 소유자)이거나
  `discord_admins` 에 등록된 사용자만 쓸 수 있다. 서버 관리자는 따로 등록할 필요가 없다.
  둘 다 아니면 조회 명령만 동작한다(채널 멤버 아무나 포트를 열 수 있으면 안 되므로).
  자동 허용을 끄려면 `discord_allow_guild_admins: false` / `--no-discord-guild-admins`
- 명령으로 주소가 바뀌면 채널에 새 주소를 알린다
- `tools/discord-setup.py` — 이미 돌아가는 봇의 `.env` 에서 토큰을 찾아 검증하고
  채널 목록을 보여 준 뒤 `~/.fastunnel/config.json`(권한 600)을 만들어 준다.
  토큰은 마스킹해서만 출력한다

### 수정

- 종료 중에 공개 DNS 대기가 정리를 붙잡지 않도록 중단 신호를 받는다

---

## [1.0.1] - 2026-08-24

### 수정

- **주소는 떴는데 접속이 안 되던 문제.** cloudflared 는 quick tunnel 주소를 DNS 등록보다
  4~10초 먼저 출력한다. 그 틈에 이름을 조회하면 NXDOMAIN 이 리졸버에 음수 캐시되어
  등록이 끝난 뒤에도 90초 이상 접속이 막혔다(학교·회사 공용 DNS 에서 특히 잘 발생).
  이제 **주소를 알리기 전에 공개 DNS 등록을 먼저 확인**한다. 확인은 로컬 캐시를 오염시키지
  않도록 DoH(dns.google → cloudflare-dns.com)로 한다. 터널 재시작으로 주소가 바뀔 때도 동일하게 적용된다.

### 추가

- 설정 `dns_wait`(기본 45초) / 인자 `--dns-wait` / 환경변수 `FASTUNNEL_DNS_WAIT`. `0` 이면 기다리지 않는다
- `doctor` 에 `주소 등록확인` 항목 — DoH 사용 가능 여부를 알려 준다
- `util.resolve_public` / `util.wait_public_dns` / `util.probe_url` / `Tunnel.hostname()`
- `probe_url` 은 리졸버가 이미 오염된 환경에서도 DoH + SNI 직결로 도달성을 판정한다(검증용)

---

## [1.0.0] - 2026-08-24

첫 공개 버전. 표준 라이브러리만으로 동작하는 터널링 자동화 도구.

### 추가

- **터널링**: cloudflared quick tunnel(계정 불필요) / ngrok 지원, 포트 범위 동시 터널링
  (`8000-8005,9000` 형식), TCP·HTTP·HTTPS 프로토콜
- **실행 파일 자동 관리**: 없으면 자동 다운로드, `auto`/`skip`/`force`/`ask` 업데이트 정책,
  시스템 PATH 설치본은 그대로 사용
- **방화벽 자동 설정**: 윈도우 netsh 인바운드/아웃바운드, 리눅스 ufw·firewalld·iptables 자동 선택,
  연속 포트 범위 규칙 묶기, 종료 시 추가한 규칙만 원복, `firewall cleanup` 으로 잔여 규칙 정리
- **권한 상승**: `--elevate` 로 윈도우 UAC / 리눅스 sudo 재실행
- **알림**: 디스코드 웹훅·봇 토큰, 슬랙, 텔레그램, 범용 웹훅. 시작·주소 변경·종료 시 전송
- **GUI**: tkinter 기반 관리 화면. 터널 표(주소·접속 명령 복사, 브라우저 열기), 실시간 로그,
  알림/고급 설정 대화상자, 권한 부족 시 3지선다 안내, 실행 중 종료 시 정리 후 닫기
- **안정성**: 터널 프로세스 사망 시 자동 재시작(최대 5회), 상태 파일 기록, 중복 실행 감지
- **부가 명령**: `doctor`(환경 진단), `logs`, `config init/show/path`, `notify test`,
  `--dry-run`, `--duration`(자동 종료)
- **테스트**: 외부 도구 없이 `python tests/run_all.py` 로 도는 검증 56개
