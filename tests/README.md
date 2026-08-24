# 테스트

외부 패키지가 필요 없습니다. pytest 도 쓰지 않습니다.

```bash
python tests/run_all.py                 # 전체 (약 35초, 실제 터널을 엽니다)
python tests/run_all.py --no-network    # 네트워크 없이 (core + notify)
python tests/run_all.py --no-gui        # GUI 제외
python tests/run_all.py core notify     # 원하는 것만
```

| 파일 | 검증 내용 | 필요 조건 |
|---|---|---|
| `test_core.py` | 포트 파싱, 설정 병합·마스킹, 방화벽 계획/적용/원복/정리, 시그널 | 없음 |
| `test_notify.py` | 디스코드·슬랙·범용 웹훅 페이로드 (로컬 수신 서버로 실제 전송) | 없음 |
| `test_tunnel.py` | 실제 cloudflared 터널 개통 후 공개 URL 접속까지 확인 | 네트워크 |
| `test_gui.py` | GUI 입력 검증, 오류 5종, 실제 터널, 종료 정리 | 화면 환경 |

## 원칙

- 테스트는 `FASTUNNEL_HOME` 을 임시 폴더로 바꿔 돕니다. 사용자의 `~/.fastunnel` 을 건드리지 않습니다.
- GUI 테스트는 창을 `withdraw()` 하거나 투명하게 만들어 **사용자 화면에 띄우지 않습니다.**
  모달 대화상자는 자동 응답으로 교체합니다.
- 디스코드로 진짜 메시지를 보내지 않습니다. 웹훅 URL 자리에 로컬 수신 서버를 씁니다.
- 방화벽 규칙은 실제로 넣지 않습니다(권한 필요). 명령 실행 함수를 가짜로 바꿔
  "무슨 명령이 만들어지는가"와 "원복이 짝을 이루는가"를 확인합니다.
