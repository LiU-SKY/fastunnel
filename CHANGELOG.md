# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따릅니다.

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
