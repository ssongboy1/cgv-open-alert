# CGV 예매 오픈 알리미

지정한 **영화관 + 상영관 + 영화**의 회차가 새로 열리면 텔레그램으로 알려준다.

- 설치할 파이썬 패키지가 **없다** (표준 라이브러리만)
- 설정은 **텔레그램 봇**에서 한다. PC 없이도 된다
- GitHub Actions에서 돌아서 **PC를 꺼도 동작**한다
- 조회 전용이다. 예매·결제·좌석 선점은 하지 않는다

---

## 지금 이 프로젝트는

| | |
| --- | --- |
| 리포지토리 | https://github.com/ssongboy1/cgv-open-alert (public) |
| 텔레그램 봇 | [@ssongmovie_bot](https://t.me/ssongmovie_bot) — 영화예매오픈알림 |
| 깨우는 주체 | cron-job.org — 5분마다 `repository_dispatch` |
| 실제 확인 주기 | **1분** (새벽 1~7시는 10분) |
| 알림까지 최악 | 약 1분 30초 |

---

## 쓰는 법 — 텔레그램 명령

봇 대화창에서 `/` 를 누르면 목록이 나온다.

| 명령 | 하는 일 |
| --- | --- |
| `/add` | 감시 대상 추가 — 지역 → 지점 → 상영관 → 영화 순으로 버튼 |
| `/list` | 감시 목록 보기, 항목별 삭제 버튼 |
| `/check` | **지금** 몇 회차가 잡히는지 조회 |
| `/status` | 마지막 점검 시각, 감시 대상, 기준선 건수 |
| `/help` | 도움말 |

명령은 대개 **1초 안에** 응답한다.

### `/add` 를 쓸 때

- 지점을 고르면 **그 지점에 아이맥스가 있는지** 바로 알려준다
- 영화 제목은 일부만 적어도 되고, 띄어쓰기·붙임표는 무시한다
  (`스파이더맨 브랜드 뉴 데이` → `스파이더맨-브랜드 뉴 데이` 매칭됨)
- **오타를 잡아준다.** CGV 목록에 없으면 비슷한 제목을 버튼으로 제안한다
- 개봉예정작도 등록된다. `🕒 개봉예정작입니다` 라고 알려준다
- 영화를 안 정하면 **새 영화가 걸릴 때만** 알리도록 자동 설정된다
  (안 그러면 날짜 열릴 때마다 수십 건이 쏟아진다)

### 알림이 오면

```
🎬 CGV 예매 오픈!

📍 CGV 대구
🎞 오디세이

  8/26(수) 07:30  IMAX LASER 2D  6/241석
  8/26(수) 11:00  IMAX LASER 2D  6/241석

🔗 https://cgv.co.kr/cnm/movieBook/cinema?siteNo=0345&siteNm=대구

2026-08-26 09:00:12 KST

[📱 CGV 앱으로 열기]
```

버튼을 누르면 CGV 앱이 열린다. 앱이 없으면 웹 예매로 넘어간다.

---

## 뭔가 이상할 때

**알림이 안 온다**
→ `/check` 로 회차가 잡히는지 본다. **0건이면 영화 제목이 틀린 것**이다.
`/list` 에서 지우고 `/add` 로 다시 넣는다.

**같은 알림이 반복된다**
→ `state.json` 이 커밋되지 않고 있다. GitHub Actions 탭에서 "변경사항 커밋" 스텝을 본다.

**403 경고가 왔다**
→ Cloudflare 정책이 바뀐 것이다. `gh workflow run smoke-test.yml` 로 확인한다.

**전부 다시 검사하고 싶다**
```bash
python selftest.py --live
```

---

## 파일

| 파일 | 역할 |
| --- | --- |
| `watcher.py` | 감시 본체. 여기가 핵심 |
| `cgv_api.py` | CGV 조회. **UA 상수 건드리면 전부 403** |
| `telegram_bot.py` | 봇 명령 처리 (`/add`, `/list` …) |
| `notifier.py` | 텔레그램 전송 |
| `merge_state.py` | 실행이 겹칠 때 state 병합 |
| `selftest.py` | 자체 점검 60건 |
| `setup_gui.py` | PC용 설정 창 (봇으로 대체됨, 초기 설정용) |
| `config.json` | 감시 대상 — 봇이 고치고 커밋된다 |
| `state.json` | 이미 알린 회차 — Actions가 커밋한다 |
| `docs/open.html` | 앱 열어주는 중계 페이지 (GitHub Pages) |
| `.env` | 로컬용 토큰 — **커밋 안 됨** |

---

## 명령어

```bash
python watcher.py              # 1회 실행 (Actions가 이걸 쓴다)
python watcher.py --dry-run    # 알림 없이 결과만
python watcher.py --loop 60    # 60초 주기 상시 실행 (로컬)
python selftest.py             # 점검 56건 (CGV 호출 없음, 몇 초)
python selftest.py --live      # 실연결까지 60건
python setup_gui.py            # PC 설정 창
```

---

## 더 깊은 내용

동작 원리, CGV API 스펙, 그동안 잡은 버그, 손대면 안 되는 것들은
**[개발노트.md](개발노트.md)** 에 있다. 다음에 작업할 때 그걸 먼저 읽으면 된다.
