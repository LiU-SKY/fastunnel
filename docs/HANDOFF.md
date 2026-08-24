# 인수인계 문서

새 세션에서 이어서 작업할 때 **여기부터 읽으세요.**
"무엇이 끝났고, 무엇이 실제로 검증됐고, 무엇이 안 됐는지"를 정리한 문서입니다.

- 작성 시점: 2026-08-24
- 버전: 1.0.1
- 상태: **동작하는 완성품**. 실기 검증 완료. 배포 가능.

---

## 1. 30초 요약

cloudflared / ngrok 으로 포트 범위를 터널링하고 방화벽·디스코드 알림까지 자동화하는 도구.
CLI 와 GUI 둘 다 있고, 파이썬 표준 라이브러리만 씁니다.

```bash
python -m fastunnel doctor        # 환경 점검
python -m fastunnel 8000-8005     # CLI 로 터널
python -m fastunnel gui           # GUI
python tests/run_all.py           # 전체 검증 (약 50초)
```

읽는 순서: `README.md`(사용법) → `CLAUDE.md`(개발 규칙·함정) → `docs/ARCHITECTURE.md`(설계 배경)

---

## 2. 무엇이 만들어졌나

### 기능

| 기능 | 상태 | 비고 |
|---|---|---|
| 포트 범위 터널링 (`8000-8005,9000`) | 완료 | 범위·개별·`:`·`,` 혼용 지원 |
| cloudflared quick tunnel | 완료 | 계정/토큰 불필요. 포트마다 프로세스 1개 |
| ngrok | 완료 | authtoken 필요. 프로세스 1개 + 로컬 API 폴링 |
| TCP / HTTP / HTTPS | 완료 | 기본은 TCP |
| 실행 파일 자동 다운로드·업데이트 | 완료 | `auto` / `skip` / `force` / `ask` |
| 윈도우 방화벽 인바운드+아웃바운드 | 완료 | netsh. 종료 시 자동 원복 |
| 리눅스 방화벽 | 완료(문법 검증) | ufw → firewalld → iptables 자동 선택 |
| 권한 자동 상승 | 완료 | `--elevate` (윈도우 UAC / 리눅스 sudo) |
| 디스코드 웹훅 알림 | 완료 | 임베드 카드, 시작·변경·종료 |
| 디스코드 봇 알림 | 완료 | 토큰 + 채널 ID |
| 슬랙 / 텔레그램 / 범용 웹훅 | 완료 | `webhooks` 배열 |
| 터널 사망 시 자동 재시작 | 완료 | 최대 5회, 백오프, 새 주소 재알림 |
| GUI | 완료 | tkinter. 표·로그·설정 대화상자 |
| 설정 파일 / 환경 변수 | 완료 | 4단 병합 |
| 진단(`doctor`) · 로그 뷰어 · 방화벽 정리 | 완료 | |
| 자동 종료(`--duration`) | 완료 | 예약 실행·CI 용 |
| 공개 DNS 등록 대기 | 완료 | `dns_wait`. 주소를 알리기 전에 DoH 로 확인 (1.0.1) |

### 파일

```
fastunnel/          소스 9개 모듈 + providers 4개   (약 4,600줄)
tests/              검증 5개 파일 (70개 체크)
docs/               ARCHITECTURE.md, HANDOFF.md
README.md           사용자 문서 (한국어)
CLAUDE.md           개발자/AI 세션용 문서
fastunnel.ps1 / .sh            CLI 실행 래퍼
fastunnel-gui.cmd / -gui.sh    GUI 실행 래퍼 (.cmd 는 cp949 인코딩)
fastunnel.example.json         설정 예시
```

---

## 3. 실제로 검증한 것 (전부 실기 확인)

`python tests/run_all.py` → **69개 체크 통과 · 1 건너뜀 (51.3초)**

| 검증 | 결과 |
|---|---|
| HTTP 터널 → 발급된 공개 URL 로 접속 | **HTTP 200** (로컬 서버 응답 확인) |
| 주소 공개 시점에 이름 조회 성공 (NXDOMAIN 캐시 없음) | 통과. 실제 CLI 실행으로도 확인 |
| TCP 터널 → `cloudflared access tcp` 로 되돌려 접속 | **HTTP 200** (데이터 경로까지 확인) |
| 포트 2개 동시 터널 | 2/2 주소 확보 |
| ngrok 다운로드·압축해제·버전 인식 | 11.7MB → `3.39.11` |
| netsh 규칙 문법 | 비관리자 실행 시 "requires elevation"만 → 문법 정상 |
| 방화벽 적용→상태저장→원복→cleanup | 시뮬레이션 통과 |
| ufw/firewalld/iptables 명령 문자열 | 문자열 검증 통과 (**실기 미검증**, 아래 4장) |
| 디스코드 임베드 / 범용 / 슬랙 페이로드 | 로컬 수신 서버로 실제 전송 확인 |
| GUI 에서 터널 개통 → 복사 → 정리 종료 | 통과 |
| GUI 오류 5종 | 통과 (실행파일 오류 / ngrok 토큰 / 스레드 예외 / 권한 부족 / 실행 중 종료) |
| GUI 레이아웃 | 기본·최소 크기에서 잘림 0, 창 밖 위젯 0 |
| `.cmd` 런처로 GUI 기동 | 창 확인 |

### 개발 중 발견해 고친 버그 (참고용)

1. **GUI 대화상자 입력값 유실** — 시작할 때 설정 파일을 다시 읽어 방금 입력한 웹훅/토큰을 덮어씀
   → `_current_config()` 가 메모리 값과 화면 값을 병합하도록 변경
2. **`pythonw` 실행 시 출력 크래시** — `sys.stdout` 이 `None` → 로그를 sink 구조 + 방어적 출력으로 변경
3. **`.cmd` 런처 파싱 실패** — UTF-8 저장된 한글을 cmd.exe 가 잘못 읽음 → cp949 저장, 주석 괄호 제거
4. **`config.from_dict()` KeyError** — 부분 dict 를 넘기면 터짐 → 기본값 위에 덮어쓰도록 변경
5. **iptables 인자 순서** — `-m comment` 가 `-j` 뒤에 있으면 삭제 시 매칭 실패 → 순서 교정
6. **주소는 떴는데 접속이 안 됨 (1.0.1)** — cloudflared 가 DNS 등록보다 4~10초 먼저 주소를 출력.
   그 틈의 조회가 NXDOMAIN 을 음수 캐시시켜 90초 이상 접속 불가. 실측으로 원인 확정
   (일찍 조회 안 하면 성공 / 즉시 조회하면 91초까지 실패, 같은 시각 공개 DNS 는 정상).
   → 주소를 알리기 전에 DoH 로 등록을 확인하도록 변경 (`runner.wait_public_dns`)

---

## 4. 검증하지 못한 것 (다음 세션에서 확인 필요)

솔직하게 적습니다. 이것들은 **코드는 있지만 실기 확인을 못 했습니다.**

| 항목 | 이유 | 확인 방법 |
|---|---|---|
| **리눅스 방화벽 실제 적용** | 개발 환경이 윈도우뿐 | 리눅스/WSL 에서 `sudo python3 -m fastunnel run -p 8000 -v` 후 `ufw status` |
| **관리자 권한 방화벽 실적용** | 세션에서 UAC 승인 불가 | 관리자 PowerShell 에서 실행 → `fastunnel firewall status` 로 규칙 확인 → 종료 후 사라지는지 |
| **`--elevate` UAC 흐름** | 승인 창 조작 불가 | 일반 사용자로 `--elevate` 실행 → 새 창이 뜨고 방화벽이 적용되는지 |
| **ngrok 실제 터널** | authtoken 없음 | 토큰 등록 후 `python -m fastunnel run -p 8000 --provider ngrok` |
| **디스코드 실제 전송** | 실제 웹훅 없음 | 웹훅 URL 만들고 `python -m fastunnel notify test` |
| **macOS 전반** | 환경 없음 | 다운로드 URL(`darwin-*.tgz`)과 `open` 명령 확인 필요 |
| **GUI 실제 조작감** | 화면 캡처 불가(다른 창이 찍힘) | 사람이 직접 열어서 확인 |
| **터널 재시작 로직** | 프로세스 강제 종료 시나리오 미실행 | 터널 실행 중 `cloudflared` 프로세스를 죽여 보고 재시작·재알림 확인 |
| **포트 10개 이상 동시** | Cloudflare 제한 회피 | `--start-delay` 조절하며 확인 |

---

## 5. 다음에 해볼 만한 것

우선순위 순입니다. **아무것도 하지 않아도 도구는 동작합니다.**

1. **리눅스 실기 검증** — 위 4장 1행. 가장 값어치 있는 확인입니다.
2. **터널 재시작 시나리오 테스트 추가** — `tests/test_tunnel.py` 에 프로세스를 죽이고
   재시작·새 주소 알림까지 확인하는 케이스 추가
3. **GUI 프로파일 기능** — 자주 쓰는 포트 묶음을 이름 붙여 저장/전환
4. **터널 상태 요약 알림** — 일정 주기로 "N개 정상" 하트비트 (지금은 시작/변경/종료만)
5. **cloudflared 정식 터널 지원** — 계정 연동 시 고정 도메인 사용 (지금은 quick tunnel 만)
6. **다국어** — 지금은 한국어 고정. 필요하면 문자열을 한곳에 모으는 작업부터
7. **패키징** — `pyproject.toml` + `pipx install` 지원 (단, 실행 자체는 계속 의존성 0 유지)

---

## 6. 새 세션을 시작하는 방법

```bash
git clone <저장소 URL>
cd fastunnel
python -m fastunnel doctor          # 환경이 준비됐는지
python tests/run_all.py --no-gui    # 코드가 멀쩡한지 (약 20초)
```

그 다음 `CLAUDE.md` 의 "반드시 알아야 할 함정" 절만 읽으면 바로 손댈 수 있습니다.
설계 이유가 궁금하면 `docs/ARCHITECTURE.md`.

### 작업할 때 지킬 것

- 표준 라이브러리만. Python 3.8+. 사용자 문자열은 한국어. (`CLAUDE.md` 1장)
- 기능은 `runner.py` 에 넣어 CLI·GUI 양쪽이 같이 쓰게 할 것
- 바꾼 게 있으면 `tests/` 에 검증 추가하고 `python tests/run_all.py` 통과 확인
- 한글 출력 확인은 **PowerShell** 에서 (Git Bash 파이프는 원래 깨져 보임)

---

## 7. 알아 둘 제약

- **cloudflared TCP 는 접속하는 쪽에도 cloudflared 가 필요합니다.** 이건 Cloudflare 의 구조이지
  이 도구의 한계가 아닙니다. 상대방 설치가 부담이면 `--proto http` 또는 ngrok.
- quick tunnel 주소는 **실행할 때마다 바뀝니다.** 고정하려면 도메인 + 계정이 필요합니다.
- ngrok 무료 플랜은 동시 엔드포인트 수 제한이 있어 포트를 많이 열기엔 cloudflared 가 낫습니다.
- 방화벽 자동 설정은 관리자/root 권한이 필요합니다. 없으면 **중단하지 않고** 수동 명령을 출력합니다.
- 터널을 열면 해당 포트가 인터넷에 공개됩니다. 인증 없는 서비스는 열지 마세요.
