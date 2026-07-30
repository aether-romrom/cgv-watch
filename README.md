# cgv-watch

CGV 상영 스케줄을 주기적으로 확인해서 변화를 ntfy 푸시로 알린다.

- **새 회차 등장** — 아직 안 열려 있던 날짜의 예매가 오픈된 경우
- **잔여좌석 증가** — 매진/임박 회차에 취소표가 풀린 경우

CGV는 Cloudflare 봇 차단이 걸려 있어 일반 HTTP 요청으로는 403이 떨어진다.
그래서 Playwright로 실제 Chromium을 띄우고 페이지 컨텍스트 안에서
내부 API(`/api/v1/booking/searchSchByMov`)를 호출한다.

## 설정

감시 대상은 `config.json` 에서 바꾼다.

| 필드 | 뜻 |
|---|---|
| `movNo` | CGV 영화 번호 |
| `siteNo` | 극장 번호 (`0013` = 용산아이파크몰) |
| `screen_match` | 상영관 이름/포맷에 이 문자열이 들어간 회차만 감시 (`IMAX`) |
| `days_ahead` | 오늘부터 며칠 앞까지 볼지 |
| `seats_freed_when_prev_at_or_below` | 잔여가 이 수 이하였던 회차만 취소표 알림 |

알림 주소(`NTFY_TOPIC`)는 이 저장소에 두지 않는다.
로컬은 launchd 환경변수, Actions는 저장소 Secrets로 주입한다.

## 실행

```bash
NTFY_TOPIC=... python watch.py
```
