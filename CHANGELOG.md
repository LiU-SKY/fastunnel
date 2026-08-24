# 변경 이력

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따릅니다.

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
