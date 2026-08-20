# CGV 아이맥스 예매 오픈 알리미

지정한 CGV 지점 + 영화의 **IMAX 회차가 새로 열리면 텔레그램으로 알려준다.**

- 설치할 파이썬 패키지가 **없다** (표준 라이브러리만 사용)
- 설정은 GUI에서 한다. 지점 목록은 CGV에서 실시간으로 받아온다
- 조회 전용이다. 예매·결제·좌석 선점은 하지 않는다

---

## 빠른 시작

### 1. 텔레그램 봇 만들기

1. 텔레그램에서 **@BotFather** 를 찾아 `/newbot` 을 보낸다
2. 이름을 정하면 `123456:ABC-DEF...` 형태의 **토큰**을 준다
3. 방금 만든 봇과의 대화창에서 **아무 메시지나 한 번 보낸다** (이래야 챗 ID를 찾을 수 있다)

### 2. 설정

`설정.bat` 을 더블클릭한다.

- 토큰을 붙여넣고 **[챗 ID 자동 찾기]** → **[테스트 전송]** 으로 확인
- **[추가]** 로 지역 → 지점 → 영화를 고른다
  - 지점을 고르면 그 지점에 아이맥스가 있는지 바로 알려준다
  - 아직 개봉 안 한 영화는 **[직접 입력]** 에 제목을 적는다 (부분일치, 공백 무시)
- **[설정 저장]** → **[지금 1회 점검]** 으로 동작 확인

### 3. 실행

**클라우드 (권장, PC를 꺼둬도 동작)** — 아래 "GitHub + cron-job.org" 참고

**로컬** — 예매 오픈이 임박한 날 더 빠르게 감시하고 싶을 때

```bash
python watcher.py --loop 30     # 30초 주기
```

---

## 동작 방식

### 감지

CGV 신규 사이트의 공개 BFF(`https://cgv.co.kr/api/v1/booking/*`)를 조회한다.
토큰도 쿠키도 필요 없고 `coCd=A420` 만 있으면 된다.

회차 하나를 아래 키로 식별하고, `state.json` 의 `seen` 에 없으면 **새로 열린 회차**로 본다.

```
site_no | movNo | scnYmd | scnsrtTm | scnsNo
```

첫 실행에서는 이미 열려 있는 회차를 전부 기준선으로만 저장하고 알리지 않는다.
그래서 알림은 **진짜 새로 생긴 회차**에만 온다.

### 요청 절약 (2단계 게이트)

매 실행마다 전체 시간표를 훑으면 CGV에 부담이 크다. 그래서 지점당 **2요청**만 먼저 보낸다.

| 게이트 | 무엇을 보나 |
| --- | --- |
| `searchSscnsSchdExistList` | 그 지점 아이맥스에서 상영 중인 **영화 편수** |
| `searchSiteScnscYmdListBySite` | **예매 가능한 날짜 목록** |

둘 다 지난번과 같으면 상세 조회를 건너뛴다. 값이 바뀌었을 때만 날짜별 시간표를 읽는다.
같은 날짜 안에서 회차만 늘어나는 경우는 게이트로 안 잡히므로,
`full_scan_every_runs` 회마다(기본 30 = 1분 주기 기준 30분) 강제로 전체를 훑는다.

### Cloudflare

`cgv.co.kr` 앞단 Cloudflare는 **User-Agent 문자열만 보고** 차단한다.

| UA | 결과 |
| --- | --- |
| `curl/8.4.0` | 403 |
| `python-requests/2.32.0` | 403 |
| `Mozilla/5.0 ... Chrome/131` | 200 |

`cgv_api.py` 의 `UA` 상수가 그래서 있다. **지우면 전부 403이 된다.**
403이 발생하면 텔레그램으로 경고를 보내고 종료한다.

---

## GitHub + cron-job.org

GitHub Actions 자체 cron은 5~15분씩 지연된다. 그래서 **cron-job.org**(무료, 1분 주기)가
`repository_dispatch` 로 워크플로를 직접 깨우게 한다. GitHub schedule 은 1시간 안전망으로만 둔다.

### 1) 리포지토리

리포지토리는 **public** 으로 만든다. Actions 분이 무제한 무료가 되기 때문이다.
private 이면 무료 2,000분/월 한도에 걸려 5분 주기도 일주일이면 소진된다.
공개되는 것은 코드와 `config.json`(지점·영화명)뿐이고, 토큰은 Secrets 에 들어간다.

```bash
gh auth login
git init && git add -A && git commit -m "CGV IMAX 알리미"
gh repo create imax-alert --public --source=. --remote=origin --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

### 2) 403 여부 먼저 확인

```bash
gh workflow run smoke-test.yml && gh run watch
```

- 성공 → 다음 단계
- 실패(403) → `watch.yml` 을 끄고 로컬 실행(`--loop`)으로 전환한다. 코드는 그대로다

### 3) cron-job.org 등록

`public_repo` 스코프만 가진 **classic PAT** 를 발급한 뒤, cron-job.org 에 job 하나를 만든다.

| 항목 | 값 |
| --- | --- |
| URL | `https://api.github.com/repos/<계정>/imax-alert/dispatches` |
| Method | `POST` |
| Schedule | every 1 minute |
| Header | `Authorization: Bearer <PAT>` |
| Header | `Accept: application/vnd.github+json` |
| Header | `User-Agent: imax-alert` |
| Body | `{"event_type":"poll"}` |

성공 시 GitHub는 **204 No Content** 를 준다. cron-job.org 에서 성공 조건을 204로 두고
실패 알림 메일을 켜둔다.

---

## 파일

| 파일 | 역할 |
| --- | --- |
| `cgv_api.py` | CGV BFF 조회 (UA 상수 주의) |
| `notifier.py` | 텔레그램 전송, 챗 ID 탐색 |
| `watcher.py` | 감시 본체 |
| `setup_gui.py` | 설정 프로그램 (Tkinter) |
| `config.json` | 감시 대상 — 커밋됨 |
| `state.json` | 이미 알린 회차 — Actions가 커밋 |
| `.env` | 로컬 실행용 토큰 — **커밋 안 됨** |

## 명령어

```bash
python watcher.py              # 1회 실행 (Actions가 이걸 쓴다)
python watcher.py --dry-run    # 알림 없이 결과만 출력
python watcher.py --loop 30    # 30초 주기 상시 실행
python setup_gui.py            # 설정 프로그램
```

## 문제가 생기면

**알림이 안 온다** — `python watcher.py --dry-run` 을 돌려 회차가 잡히는지 본다.
잡히는데 안 오면 GUI의 [테스트 전송]으로 텔레그램 쪽을 확인한다.

**403 경고가 왔다** — Cloudflare 정책이 바뀐 것이다. `gh workflow run smoke-test.yml` 로
Actions에서도 막히는지 확인하고, 막히면 로컬 실행으로 옮긴다.

**같은 알림이 반복된다** — `state.json` 이 저장/커밋되지 않고 있다.
Actions 로그에서 "state.json 커밋" 스텝을 확인한다.

**처음부터 다시 보고 싶다** — `state.json` 을 지우면 다음 실행이 기준선을 다시 잡는다.
