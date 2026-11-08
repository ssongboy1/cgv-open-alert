#!/usr/bin/env python3
"""CGV·메가박스 예매 오픈 감시.

    python watcher.py              1회 실행 후 종료 (GitHub Actions용)
    python watcher.py --loop 30    30초 주기 상시 실행 (로컬 스퍼트용)
    python watcher.py --dry-run    알림을 보내지 않고 결과만 출력

감시 대상 (config.json 의 targets)
----------------------------------
    site_no        지점 코드            "0345"   CGV 대구
                                        "MB1351" 메가박스 코엑스 (MB 접두어)
    site_nm        지점 이름            "대구"
    screen         상영관 필터          CGV      "아이맥스" | "4DX" | "SCREENX"
                                        메가박스  "돌비시네마" | "MX4D" | "리클라이너" ...
                                        "ALL" 이면 전 상영관
    movie_keyword  영화 제목 부분일치    "오디세이"  (빈 문자열이면 전체)
    notify         알림 단위            "schedule" = 새 회차마다
                                        "movie"    = 새 영화가 걸릴 때 한 번만

감지 방식
---------
지점당 매 실행 최소 요청("게이트")만 보내고, 게이트 값이 바뀌었을 때에만
날짜별 시간표를 상세 조회한다. 게이트는

    CGV      (1) 아이맥스 상영 편수  (2) 예매 가능 날짜 목록   = 2요청
    메가박스  (1) 예매 가능 날짜 목록 (2) 상영 편수            = 1요청
             (한 응답에 둘 다 들어 있다)

이미 열린 날짜 안에서 회차/영화만 늘어나는 경우는 게이트로 잡히지 않으므로
full_scan_every_minutes 분마다 강제로 전체 스캔한다.

극장사별 차이는 chains.py 가 전부 흡수한다. 회차 dict 는 두 극장사 모두
CGV 필드 이름을 쓰므로 이 파일은 극장사를 거의 의식하지 않는다.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cgv_api
import chains
import megabox_api
import notifier
import telegram_bot

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "state.json"
BOOK_URL = "https://cgv.co.kr/cnm/movieBook"
# 앱을 열어주는 중계 페이지 (docs/open.html, GitHub Pages)
APP_OPEN_URL = "https://ssongboy1.github.io/cgv-open-alert/open.html"

SCREEN_ALL = "ALL"

# 사용자가 어떻게 적든 각 극장사의 상영관 표기로 맞춘다. chains 가 갖는다.
SCREEN_ALIASES = dict(chains.SCREEN_ALIASES)

DEFAULT_CONFIG = {
    "targets": [],
    "full_scan_every_minutes": 30,
    "notify_on_first_run": False,
    # 403 경고를 며칠에 한 번까지 보낼지. 막힌 지점을 건너뛰고 계속 도는
    # 구조라, 같은 경고가 시간마다 오면 알림만 시끄럽고 할 일은 없다.
    "block_warn_hours": 24,
    # CGV 예매 오픈은 주 단위 배치로 주간에 일어난다. 새벽에는 감속해서
    # 불필요한 요청을 줄인다 (그래도 완전히 멈추지는 않는다).
    "quiet_hours": {"start": 1, "end": 7, "slowdown": 10},
}


# ---------------------------------------------------------------- 저장소

def load_config():
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return cfg


def load_state():
    if not STATE_FILE.exists():
        return {"initialized": False, "run_no": 0, "seen": [], "gates": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state):
    """임시 파일에 쓴 뒤 교체한다.

    워크플로는 루프 도중 SIGTERM 으로 종료되므로, 직접 덮어쓰면
    state.json 이 반쯤 쓰인 채로 남을 수 있다.
    """
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def block_warn_hours(cfg):
    """403 경고를 몇 시간에 한 번까지 보낼지."""
    try:
        return max(0.1, float(cfg.get("block_warn_hours", 24)))
    except (TypeError, ValueError):
        return 24.0


def note_blocked(cfg, state):
    """403 차단을 건네받은 state 에 남기고, 경고 보낼 차례인지 알려준다.

    경고는 간격을 두고 보내지만 차단 사실은 매번 기록한다. 다음 실행이
    정상으로 돌아오면 이 기록을 보고 복구 알림을 보낸다. 차단 알림만
    오고 끝나면 감시가 아예 멈춘 건지 알 수 없기 때문이다.

    경고를 실제로 내보냈을 때만 block_warned 를 세운다. 복구 알림은 이
    표시를 보고 나간다. CGV 403 은 한 사이클만 깜빡였다가 바로 풀리는
    일이 잦은데, 그때마다 복구 알림을 보내면 '막혔다' 는 말은 24시간
    제한에 걸려 안 가고 '풀렸다' 는 말만 계속 가게 된다.
    """
    now = time.time()
    warn = now - float(state.get("last_block_warn", 0)) >= block_warn_hours(cfg) * 3600
    if warn:
        state["last_block_warn"] = now
        state["block_warned"] = True
    if not state.get("blocked"):
        state["blocked_at"] = datetime.now(KST).isoformat(timespec="seconds")
    state["blocked"] = True
    return warn


def mark_blocked(cfg=None):
    """note_blocked 와 같지만 state 를 직접 읽고 쓴다.

    대상 루프 밖에서 터진 차단(= state 를 손에 들고 있지 않은 자리)용이다.
    """
    try:
        state = load_state()
    except Exception:
        return True
    warn = note_blocked(cfg or {}, state)
    try:
        save_state(state)
    except Exception:
        pass
    return warn


# ---------------------------------------------------------------- 대상 해석

def screen_of(target):
    """예전 설정의 'sscns': 'IMAX' 도 그대로 받아준다."""
    raw = target.get("screen") or target.get("sscns") or "아이맥스"
    return SCREEN_ALIASES.get(raw, raw)


def granularity_of(target):
    """알림 단위. schedule=회차, movie=영화, date=예매 열린 날짜.

    date 는 시간표를 전혀 조회하지 않는다. 열린 날짜 목록만 본다. CGV 의
    시간표 주소가 403 으로 막혀 있어도 '그 날짜가 열렸나'는 알 수 있다.
    """
    notify = target.get("notify")
    if notify in ("date", "screen", "listing"):
        return notify
    return "movie" if notify == "movie" else "schedule"


def watch_dates_of(target):
    """date 대상이 노리는 날짜들. 비어 있으면 '새로 열리는 날짜 전부'."""
    raw = target.get("watch_date") or target.get("watch_dates") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(d) for d in raw}


def target_id(target):
    """감시 대상 하나를 식별하는 값. 기준선을 잡았는지 기록하는 데 쓴다.

    날짜 감시는 노리는 날짜까지 넣어야 한다. 안 그러면 같은 지점에
    '12/16 열리면' 과 '새 날짜 아무거나' 를 같이 걸 수 없다. 둘의 id 가
    같아서 한쪽이 기준선을 잡으면 다른 쪽도 잡은 것으로 쳐버린다.
    """
    base = "{}|{}|{}|{}".format(
        target.get("site_no", "CGV"), screen_of(target),
        normalize(target.get("movie_keyword", "")), granularity_of(target))
    if granularity_of(target) == "date":
        base += "|" + ",".join(sorted(watch_dates_of(target)))
    return base


def describe(target):
    if granularity_of(target) == "listing":
        return "CGV 예매 목록에 '{}' 뜨면".format(
            target.get("movie_keyword", "") or "새 영화")
    if granularity_of(target) == "screen":
        scr = screen_of(target)
        return "{} {}".format(
            target.get("site_nm", target["site_no"]),
            "특별관 전체" if scr == SCREEN_ALL else scr + " 편수")
    if granularity_of(target) == "date":
        wanted = sorted(watch_dates_of(target))
        what = ("/".join(fmt_date(d) for d in wanted) + " 열리면"
                if wanted else "새 예매일")
        return "{} ({})".format(
            target.get("site_nm", target["site_no"]), what)
    screen = screen_of(target)
    screen_txt = "전 상영관" if screen == SCREEN_ALL else screen
    movie_txt = target.get("movie_keyword") or "전체 영화"
    unit = "새 영화" if granularity_of(target) == "movie" else "새 회차"
    return "{}/{}/{} ({})".format(
        target.get("site_nm", target["site_no"]), screen_txt, movie_txt, unit)


# ---------------------------------------------------------------- 포맷

def in_quiet_hours(cfg):
    """새벽 감속 구간인지. start <= 시각 < end (자정을 넘겨도 동작)."""
    quiet = cfg.get("quiet_hours") or {}
    start, end = quiet.get("start"), quiet.get("end")
    if start is None or end is None:
        return False
    hour = datetime.now(KST).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def log(msg):
    print("[{}] {}".format(datetime.now(KST).strftime("%H:%M:%S"), msg), flush=True)


_PUNCT = re.compile(r"[^0-9a-z가-힣ㄱ-ㅎㅏ-ㅣ]+")


def normalize(text):
    """제목 매칭용 정규화.

    공백뿐 아니라 문장부호도 지운다. CGV 제목은 '스파이더맨-브랜드 뉴 데이'
    처럼 붙임표를 쓰는데, 사용자는 보통 '스파이더맨 브랜드 뉴 데이'로
    적는다. 붙임표를 남겨두면 이런 입력이 매칭되지 않는다.
    """
    return _PUNCT.sub("", (text or "").lower())


def matches(mov_nm, keyword):
    if not keyword:
        return True
    return normalize(keyword) in normalize(mov_nm)


def fmt_date(ymd):
    day = datetime.strptime(ymd, "%Y%m%d")
    return "{}/{}({})".format(day.month, day.day, "월화수목금토일"[day.weekday()])


def fmt_time(hhmm):
    # CGV는 심야 회차를 2440(=다음날 00:40)처럼 24시를 넘겨서 준다. 앱 표기와 같다.
    return "{}:{}".format(hhmm[:2], hhmm[2:])


def fmt_seats(row):
    seats, total = row.get("frSeatCnt"), row.get("stcnt")
    if str(seats) == "0":
        return "매진"
    return "{}/{}석".format(seats, total)


def screen_label(row):
    """앱이 표시하는 규격 그대로. 'IMAX LASER 2D' 처럼 나온다."""
    return row.get("movkndDsplNm") or row.get("scnsNm") or "일반"


def booking_url(site_no, site_nm):
    """해당 지점 예매 화면 웹링크. 메시지 본문과 PC 용."""
    return chains.booking_url(site_no, site_nm)


def app_open_url(site_no, site_nm):
    """CGV 앱을 여는 중계 페이지.

    텔레그램은 http/https 가 아닌 링크를 링크로 만들어주지 않고,
    인라인 버튼 URL 도 https 만 받는다. 그래서 cgv:// 를 메시지에
    직접 넣을 수 없다. https 페이지를 한 번 거쳐 앱으로 넘긴다.
    (앱이 없으면 그 페이지가 알아서 웹 예매로 떨어뜨린다.)
    """
    return "{}?site={}&nm={}".format(
        APP_OPEN_URL, site_no, urllib.parse.quote(site_nm))


def booking_button(site_no, site_nm):
    # CGV 는 앱이 없거나 실패하면 중계 페이지가 알아서 웹 예매로 떨어뜨리므로
    # 버튼은 하나면 충분하다. 본문에도 웹 주소가 그대로 들어간다.
    # 메가박스는 앱 중계 페이지가 없어서 예매 페이지로 바로 보낸다.
    if chains.is_megabox(site_no):
        return [[{"text": "🎟 메가박스 예매 페이지",
                  "url": chains.booking_url(site_no, site_nm)}]]
    return [[{"text": "📱 CGV 앱으로 열기",
              "url": app_open_url(site_no, site_nm)}]]


# ---------------------------------------------------------------- 키

def schedule_key(site_no, row):
    """회차 단위 키. 5조각이며 3번째가 상영일자다 (prune_seen 이 이걸 본다)."""
    return "|".join([
        site_no,
        str(row.get("movNo")),
        str(row.get("scnYmd")),
        str(row.get("scnsrtTm")),
        str(row.get("scnsNo")),
    ])


def movie_key(site_no, row):
    """영화 단위 키. 'M|' 로 시작해 회차 키와 구분한다."""
    return "M|{}|{}".format(site_no, row.get("movNo"))


def date_key(site_no, ymd):
    """예매일 단위 키. 'D|' 로 시작해 다른 키와 구분한다."""
    return "D|{}|{}".format(site_no, ymd)


def listing_key(mov_no, mov_nm):
    """예매 목록 키. 영화번호가 있으면 그걸 쓰고 없으면 제목으로."""
    return "L|{}".format(mov_no or normalize(mov_nm))


def screen_key(site_no, screen, count):
    """특별관 편수 키. 이 편수로 이미 알렸는지를 seen 으로 판단한다.

    편수를 state 에 따로 저장해 비교하지 않고 seen 을 쓰는 이유: 알림
    전송이 실패하면 seen 에 안 들어가므로 다음 실행에서 다시 보낸다.
    저장해두고 비교하면 전송 실패한 변화를 영영 놓친다.
    """
    return "S|{}|{}|{}".format(site_no, screen, count)


def prune_seen(seen, alive_movie_keys=None):
    """오래된 키를 버려 state.json 이 무한히 커지지 않게 한다.

    * 회차 키(5조각): 상영일자가 어제보다 이전이면 버린다.
    * 예매일 키('D|'): 그 날짜가 어제보다 이전이면 버린다. 이 가지가 없으면
      5조각도 'M|' 도 아니라서 영원히 안 지워지고 state.json 이 계속 큰다.
    * 영화 키('M|'): 그 지점을 이번에 전체 스캔했는데 더 이상 안 걸려 있으면 버린다.
      (스캔하지 않은 지점의 영화 키는 건드리지 않는다)

    하루(어제까지) 여유를 두는 이유: 자정이 지나 '오늘'이 바뀌는 순간 바로
    지워버리면, CGV가 자정 넘어서도 한동안 어제 날짜를 열린 날짜 목록에
    남겨두는 사이에 정기 전체 스캔이 돌아 '이미 알림 보낸 어제 회차'를
    seen에서 사라진 채로 다시 '새 회차'로 오인해 중복 알림을 보내게 된다.
    """
    cutoff = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")
    scanned_sites = {k.split("|")[1] for k in (alive_movie_keys or set())}
    kept = set()
    for key in seen:
        parts = key.split("|")
        if key.startswith("M|"):
            if alive_movie_keys is not None and parts[1] in scanned_sites \
                    and key not in alive_movie_keys:
                continue
            kept.add(key)
            continue
        if len(parts) == 3 and parts[0] == "D":
            if parts[2] < cutoff:
                continue
            kept.add(key)
            continue
        if len(parts) == 5 and parts[2] < cutoff:
            continue
        kept.add(key)
    return kept


# ---------------------------------------------------------------- 메시지

def build_schedule_message(site_nm, mov_nm, rows, site_no=""):
    esc = html.escape
    lines = [
        "\U0001F3AC <b>CGV 예매 오픈!</b>",
        "",
        "\U0001F4CD {} {}".format(chains.label(site_no), esc(site_nm)),
        "\U0001F39E {}".format(esc(mov_nm)),
        "",
    ]
    for row in sorted(rows, key=lambda r: (r.get("scnYmd", ""), r.get("scnsrtTm", ""))):
        lines.append("  {} {}  {}  {}".format(
            fmt_date(row["scnYmd"]),
            fmt_time(row["scnsrtTm"]),
            esc(screen_label(row)),
            fmt_seats(row),
        ))
    lines += ["", "\U0001F517 {}".format(booking_url(site_no, site_nm)), "",
              datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")]
    return "\n".join(lines)


def build_movie_message(site_nm, mov_nm, rows, site_no=""):
    esc = html.escape
    dates = sorted({r["scnYmd"] for r in rows})
    screens = sorted({screen_label(r) for r in rows})
    period = fmt_date(dates[0])
    if len(dates) > 1:
        period += " ~ " + fmt_date(dates[-1])
    return "\n".join([
        "\U0001F195 <b>{} {}에 새 영화가 열렸습니다</b>".format(
            chains.label(site_no), esc(site_nm)),
        "",
        "\U0001F39E <b>{}</b>".format(esc(mov_nm)),
        "",
        "  상영관   {}".format(esc(", ".join(screens))),
        "  기간     {}".format(period),
        "  회차     총 {}회".format(len(rows)),
        "",
        "\U0001F517 {}".format(booking_url(site_no, site_nm)),
        "",
        datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
    ])


def build_date_message(site_nm, ymd, site_no=""):
    """'그 날짜 예매가 열렸다'는 알림.

    시간표를 못 읽으니 영화 제목도 회차도 넣을 수 없다. 날짜와 예매
    버튼만 준다. 그래도 '열렸다'는 사실이 제일 중요하다.
    """
    esc = html.escape
    return "\n".join([
        "\U0001F4C5 <b>{} 예매일이 열렸습니다!</b>".format(chains.label(site_no)),
        "",
        "  <b>{}</b>".format(esc(site_nm)),
        "  <b>{}</b> 예매 시작".format(fmt_date(ymd)),
        "",
        "회차와 영화는 이 알림으로 알 수 없습니다 "
        "(CGV 시간표 조회가 막혀 있습니다). 바로 들어가서 확인하세요.",
        "",
        esc(booking_url(site_no, site_nm)),
    ])


def fmt_screens(mapping):
    """특별관 현황을 사람이 읽고 비교할 수 있는 한 줄로."""
    if not mapping:
        return "(없음)"
    return ", ".join("{} {}편".format(k, mapping[k]) for k in sorted(mapping))


def _fmt_count(value):
    """숫자면 'N편', 아니면(전체 현황 문자열) 그대로."""
    return "{}편".format(value) if str(value).isdigit() else str(value)


def build_listing_message(mov_nm, rate=None):
    """CGV 예매 목록(예매율 순)에 그 영화가 올라왔다는 알림.

    지점은 알 수 없다. 전국 목록이기 때문이다. 하지만 '예매가 열렸다'는
    사실 자체를 제목으로 확인해주는 유일한 신호다.
    """
    esc = html.escape
    lines = [
        "\U0001F39F <b>CGV 예매 목록에 떴습니다!</b>",
        "",
        "  <b>{}</b>".format(esc(mov_nm)),
    ]
    if rate not in (None, ""):
        lines.append("  예매율 {}".format(esc(str(rate))))
    lines += [
        "",
        "CGV 어딘가에서 <b>예매가 열렸다</b>는 뜻입니다. "
        "어느 지점인지는 이 알림으로 알 수 없습니다. 바로 확인하세요.",
        "",
        esc(BOOK_URL),
    ]
    return "\n".join(lines)


def build_screen_message(site_nm, screen, prev, cur, site_no=""):
    """특별관 편수가 바뀌었다는 알림.

    제목은 알 수 없다 (시간표가 403). 편수는 회차 수가 아니라 그
    상영관에서 상영 중인 영화 편수여서, 늘었다면 새 영화가 들어온 것이다.
    """
    esc = html.escape
    what = "특별관" if screen == SCREEN_ALL else screen
    if prev is None:
        moved = ["  <b>{}</b>".format(esc(_fmt_count(cur)))]
    elif screen == SCREEN_ALL:
        moved = ["  {}".format(esc(_fmt_count(prev))),
                 "  → <b>{}</b>".format(esc(_fmt_count(cur)))]
    else:
        moved = ["  {} → <b>{}</b>".format(
            esc(_fmt_count(prev)), esc(_fmt_count(cur)))]
    return "\n".join([
        "\U0001F3AC <b>{} {} 라인업이 바뀌었습니다.</b>".format(
            chains.label(site_no), esc(what)),
        "",
        "  <b>{}</b>".format(esc(site_nm)),
    ] + moved + [
        "",
        "어떤 영화인지는 이 알림으로 알 수 없습니다 "
        "(CGV 시간표 조회가 막혀 있습니다). 들어가서 확인하세요.",
        "",
        esc(booking_url(site_no, site_nm)),
    ])


def build_blocked_message(blocked, cfg=None):
    """일부 지점만 403 일 때의 경고. 나머지는 계속 돈다는 걸 분명히 한다."""
    esc = html.escape
    lines = ["\u26A0\uFE0F <b>일부 지점 조회가 403으로 막혔습니다.</b>", ""]
    for site_no in sorted(blocked):
        lines.append("  · {} {}".format(chains.label(site_no), esc(str(site_no))))
    hours = block_warn_hours(cfg or {})
    lines += [
        "",
        "막힌 지점만 건너뛰고 <b>나머지 감시는 그대로 돌아갑니다.</b> "
        "다음 실행에서 자동으로 다시 시도하고, 풀리면 복구 알림을 보냅니다.",
        "",
        "이 경고는 {:g}시간에 한 번만 보냅니다.".format(hours),
        "",
        "<code>{}</code>".format(
            esc(str(blocked[sorted(blocked)[0]])[:300])),
    ]
    return "\n".join(lines)


def build_recovered_message(blocked_at):
    """403 차단에서 풀렸을 때 보내는 알림.

    차단 경고만 보내고 끝나면 감시가 아예 죽은 건지 알 수 없다.
    다시 정상 조회되는 순간 한 번 알려서 살아 있음을 확인시켜 준다.
    """
    lines = ["✅ <b>CGV 접근이 정상으로 돌아왔습니다.</b>", ""]
    try:
        when = datetime.fromisoformat(blocked_at).strftime("%H:%M")
        lines.append("{}에 403으로 막혔지만 지금은 정상 조회됩니다.".format(when))
    except (TypeError, ValueError):
        lines.append("403으로 막혔지만 지금은 정상 조회됩니다.")
    lines += ["감시를 계속합니다.", "",
              datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")]
    return "\n".join(lines)


# ---------------------------------------------------------------- 감시

def gate_snapshot(site_no, dates_only=False):
    """상세 스캔이 필요한지 판단하는 값. 극장사별 차이는 chains 가 흡수한다."""
    return chains.gate(site_no, dates_only=dates_only)


def scan_site(site_no, dates):
    """지점의 모든 열린 날짜에서 전체 시간표를 모은다 (상영관 필터 없음)."""
    rows = []
    for ymd in dates:
        rows.extend(chains.schedules(site_no, ymd))
        chains.jitter(site_no)
    return rows


def select_rows(rows, screen, keyword):
    out = []
    for row in rows:
        if screen != SCREEN_ALL and row.get("tcscnsGradNm") != screen:
            continue
        if not matches(row.get("movNm"), keyword):
            continue
        out.append(row)
    return out


def full_scan_minutes(cfg):
    """강제 전체 스캔 간격(분). 예전 설정 키도 읽어준다."""
    minutes = cfg.get("full_scan_every_minutes")
    if minutes is None:
        # full_scan_every_runs 는 '몇 사이클마다'였고 1사이클이 약 1분이었다.
        # 그대로 분으로 읽으면 기존 설정과 같은 간격이 된다.
        minutes = cfg.get("full_scan_every_runs", 30)
    try:
        return max(1, int(minutes))
    except (TypeError, ValueError):
        return 30


def full_scan_due(cfg, state, now=None):
    """강제 전체 스캔을 할 때가 됐나.

    예전에는 run_no % N 으로 셌다. 그러면 간격을 지키려고 run_no 를 매
    실행 커밋해야 하는데, 그 커밋이 5분마다 git 에 쌓여 main 에 2만 개가
    넘었다(그중 거의 전부가 숫자 하나만 바뀐 커밋이다). 시각으로 재면
    run_no 를 커밋하지 않아도 간격이 정확히 지켜진다.
    """
    last = state.get("last_full_scan")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    now = now or datetime.now(KST)
    return now - prev >= timedelta(minutes=full_scan_minutes(cfg))


def run_once(cfg, state, dry_run=False):
    """1회 점검. (알림 메시지 목록, 갱신된 state) 반환."""
    state["run_no"] = state.get("run_no", 0) + 1
    first_run = not state.get("initialized", False)

    seen = set(state.get("seen", []))
    # 이번 실행 내내 '지난번 값'은 고정이어야 한다. 루프 도중에 덮어쓰면
    # 같은 지점을 보는 두 번째 대상부터는 방금 쓴 값과 비교하게 되어
    # 영원히 '변화 없음'으로 판정된다.
    old_gates = dict(state.get("gates", {}))
    new_gates = {}
    baselined = set(state.get("baselined", []))
    scanned = {}          # site_no -> 전체 회차 (지점당 1회만 조회)
    alive_movie_keys = set()
    blocked = {}          # site_no -> 403 예외. 그 지점만 건너뛴다.
    messages = []

    if not cfg["targets"]:
        log("감시 대상이 없습니다. 설정 프로그램에서 추가하세요.")
        # 여기서 그냥 나가면 지워진 대상의 기준선 기록이 남는다. 나중에
        # 다시 등록했을 때 '이미 기준선을 잡은 대상'으로 오해해, 그동안
        # seen 이 비워진 상태에서 현재 회차를 몽땅 알리게 된다.
        state["baselined"] = []
        state["gates"] = {}
        return messages, state

    # 대상이 있을 때만 판정한다. 대상이 없는데 기록을 남기면 '의미 있는
    # 변화'로 보여서 아무 일도 안 하면서 커밋만 하게 된다.
    # 열린 날짜만 보는 지점은 특별관 현황을 조회할 필요가 없다. 그 지점을
    # 보는 대상이 전부 날짜 감시면 게이트 요청이 2건에서 1건으로 줄어든다.
    scan_sites = {t["site_no"] for t in cfg["targets"]
                  if granularity_of(t) not in ("date", "listing")}

    # 전국 예매 목록 감시. 지점과 무관하고 사이클당 요청 1건이다.
    # CGV 에서 '제목' 을 볼 수 있는 유일하게 안 막힌 길이라, 어느 지점인지는
    # 몰라도 "그 영화 예매가 열렸다" 는 사실은 제목으로 확인해준다.
    listing = [t for t in cfg["targets"] if granularity_of(t) == "listing"]
    if listing:
        try:
            playing = cgv_api.get_now_playing()
        except chains.BLOCKED_ERRORS as exc:
            blocked["예매목록"] = exc
            playing = None
            log("  예매 목록: 403 — 건너뜁니다")
        for target in (listing if playing is not None else []):
            tid = target_id(target)
            new_target = tid not in baselined
            label = describe(target)
            keyword = target.get("movie_keyword", "")
            fresh = {}
            for m in playing:
                if not keyword or not matches(m.get("movNm"), keyword):
                    continue
                key = listing_key(m.get("movNo"), m.get("movNm"))
                if key not in seen:
                    fresh[key] = m
            # 0건이어도 기준선은 잡은 것으로 친다. 아직 안 올라온 영화를
            # 기다리는 게 이 감시의 목적이다.
            baselined.add(tid)
            if not fresh:
                log("  {}: 아직 목록에 없음".format(label))
                continue
            if (first_run or new_target) and not cfg.get("notify_on_first_run", False):
                log("    {}이라 {}건을 기준선으로만 저장: {}".format(
                    "첫 실행" if first_run else "새 대상", len(fresh),
                    ", ".join(str(m.get("movNm")) for m in fresh.values())))
                seen.update(fresh)
                continue
            for key, m in sorted(fresh.items()):
                log("  {}: '{}' 올라옴".format(label, m.get("movNm")))
                messages.append((
                    build_listing_message(m.get("movNm"), m.get("atktRate")),
                    [key],
                    [[{"text": "\U0001F39F CGV 예매 페이지", "url": BOOK_URL}]]))

    force_scan = full_scan_due(cfg, state)
    if force_scan:
        state["last_full_scan"] = datetime.now(KST).isoformat(timespec="seconds")

    for target in cfg["targets"]:
        if granularity_of(target) == "listing":
            continue          # 지점이 없는 대상. 위에서 이미 처리했다.
        site_no = target["site_no"]
        site_nm = target.get("site_nm", site_no)
        screen = screen_of(target)
        keyword = target.get("movie_keyword", "")
        unit = granularity_of(target)
        label = describe(target)

        # 이번에 새로 등록된 대상인가. 시스템 전체의 첫 실행과는 별개다.
        # 이미 돌고 있는 상태에서 대상을 추가하면 그 대상에 대한 seen 이
        # 비어 있어 현재 열린 회차가 전부 '신규'로 보인다. 그대로 두면
        # 추가하자마자 지금 열려 있는 회차를 몽땅 알리게 된다.
        tid = target_id(target)
        new_target = tid not in baselined

        # 한 지점이 403 이어도 다른 지점 감시는 계속 돈다. 예전에는 403 이
        # main() 까지 올라가 사이클 전체를 끝내서, CGV 가 막히면 메가박스
        # 감시까지 같이 멈췄다.
        if site_no in blocked:
            log("  {}: 이 지점은 이번에 403 — 건너뜁니다".format(label))
            continue

        # 게이트는 지점당 한 번만 조회한다. 같은 지점을 여러 대상이
        # 보고 있어도 요청이 늘지 않는다.
        if site_no not in new_gates:
            try:
                new_gates[site_no] = gate_snapshot(
                    site_no, dates_only=site_no not in scan_sites)
            except chains.BLOCKED_ERRORS as exc:
                blocked[site_no] = exc
                # 예전 게이트 값을 그대로 물려준다. 지워버리면 다음 실행이
                # '게이트 변화'로 오인해 쓸데없이 전체 스캔을 돈다.
                if site_no in old_gates:
                    new_gates[site_no] = old_gates[site_no]
                log("  {}: 게이트 403 — 이 지점만 건너뜁니다".format(label))
                continue
        snap = new_gates[site_no]
        changed = old_gates.get(site_no) != snap

        if not (changed or force_scan or first_run or new_target):
            log("  {}: 변화 없음".format(label))
            continue

        why = ("새 대상 기준선" if new_target else
               "첫 실행" if first_run else
               "게이트 변화" if changed else "정기 전체 스캔")
        # 특별관 편수 감시. 게이트에 이미 들어 있는 값이라 추가 요청이 없다.
        if unit == "screen":
            # 특별관 조회가 간헐적으로 빈 응답을 준다 (403 과는 별개로
            # 빈 배열이 온다). 그걸 '0편'으로 읽으면 1편 -> 0편 -> 1편 으로
            # 요동치며 헛알림이 나간다. 모르는 것은 모르는 대로 둔다.
            # None 은 '조회가 막혀서 모름', {} 는 '빈 응답'. 둘 다 모르는
            # 것이므로 건너뛴다. 0편으로 읽으면 헛알림이 나간다.
            screens = snap.get("screens")
            if not screens:
                log("  {}: 특별관을 못 읽음({}) — 이번은 건너뜁니다".format(
                    label, "차단" if screens is None else "빈 응답"))
                continue
            # screen 이 ALL 이면 특별관 전체를 한 덩어리로 본다. 어느
            # 상영관이든 편수가 달라지면 알린다. 아이맥스관에 비아이맥스
            # 영화가 들어가 있는 경우처럼, 아이맥스 숫자만 봐서는 라인업
            # 변화를 놓칠 수 있기 때문이다.
            cur = (fmt_screens(screens) if screen == SCREEN_ALL
                   else str(screens.get(screen, "0")))
            counts = state.setdefault("screen_counts", {})
            prev = counts.get(tid)
            key = screen_key(site_no, screen, cur)
            baselined.add(tid)
            if key in seen:
                counts[tid] = cur
                log("  {}: {} -> {} (알린 적 있음)".format(
                    label, why, _fmt_count(cur)))
                continue
            if (first_run or new_target) and not cfg.get("notify_on_first_run", False):
                log("    {}이라 '{}' 을 기준선으로만 저장".format(
                    "첫 실행" if first_run else "새 대상", _fmt_count(cur)))
                seen.add(key)
                counts[tid] = cur
                continue
            log("  {}: {} -> {} -> {}".format(
                label, why, _fmt_count(prev), _fmt_count(cur)))
            # 여기서는 편수를 갱신하지 않는다. 갱신은 그 편수를 '알린
            # 것으로 인정'했을 때(= seen 에 들어갔을 때)만 위 가지에서
            # 한다. 지금 갱신해버리면 전송이 실패해 다시 보낼 때
            # '3편 -> 3편' 처럼 직전 값이 사라진 메시지가 나간다.
            messages.append((
                build_screen_message(site_nm, screen, prev, cur, site_no),
                [key], booking_button(site_no, site_nm)))
            continue

        # 날짜 감시는 여기서 끝난다. 시간표를 조회하지 않으므로 막힌
        # 주소를 건드리지 않고, 추가 요청도 없다 (게이트에 날짜가 들어 있다).
        if unit == "date":
            wanted = watch_dates_of(target)
            dates = [d for d in snap.get("dates", [])
                     if not wanted or d in wanted]
            fresh = {date_key(site_no, d): d for d in dates
                     if date_key(site_no, d) not in seen}
            # 0건이어도 기준선은 잡은 것으로 친다. 아직 안 열린 날짜를
            # 노리는 게 바로 이 기능의 목적이다.
            baselined.add(tid)
            if not fresh:
                log("  {}: {} -> 해당 날짜 없음".format(label, why))
                continue
            log("  {}: {} -> 새 예매일 {}건".format(label, why, len(fresh)))
            if (first_run or new_target) and not cfg.get("notify_on_first_run", False):
                log("    {}이라 {}건을 기준선으로만 저장".format(
                    "첫 실행" if first_run else "새 대상", len(fresh)))
                seen.update(fresh)
                continue
            for key, ymd in sorted(fresh.items(), key=lambda kv: kv[1]):
                messages.append((build_date_message(site_nm, ymd, site_no),
                                 [key], booking_button(site_no, site_nm)))
            continue

        if site_no not in scanned:
            try:
                scanned[site_no] = scan_site(site_no, snap["dates"])
            except chains.BLOCKED_ERRORS as exc:
                blocked[site_no] = exc
                log("  {}: 시간표 403 — 이 지점만 건너뜁니다".format(label))
                continue
        all_rows = scanned[site_no]
        alive_movie_keys.update(movie_key(site_no, r) for r in all_rows)

        rows = select_rows(all_rows, screen, keyword)
        log("  {}: {} -> {}회차".format(label, why, len(rows)))

        # 스캔을 마쳤으면 기준선을 잡은 것으로 친다. 매칭이 0건이어도
        # 반드시 여기서 기록해야 한다. 이 줄이 아래 'if not rows' 뒤에
        # 있으면, 아직 안 열린 영화(= 0건)를 감시할 때 영원히 '새 대상'으로
        # 남아 정작 예매가 열리는 순간 알림 대신 기준선만 잡고 끝난다.
        baselined.add(tid)

        if not rows:
            continue

        # 알림 단위에 따라 묶는다
        groups = {}       # 키 -> (영화명, 회차들)
        for row in rows:
            key = movie_key(site_no, row) if unit == "movie" \
                else schedule_key(site_no, row)
            if unit == "movie":
                bucket = groups.setdefault(key, [row.get("movNm"), []])
                bucket[1].append(row)
            else:
                groups[key] = [row.get("movNm"), [row]]

        fresh = {k: v for k, v in groups.items() if k not in seen}
        if not fresh:
            continue

        # 시스템 첫 실행이든 대상을 새로 추가한 것이든, 그 시점에 이미
        # 열려 있던 회차는 알리지 않는다. "새로 열리는 것"만 알린다는 약속.
        if (first_run or new_target) and not cfg.get("notify_on_first_run", False):
            log("    {}이라 {}건을 기준선으로만 저장".format(
                "첫 실행" if first_run else "새 대상", len(fresh)))
            seen.update(fresh)
            continue

        if unit == "movie":
            for key, (mov_nm, group) in fresh.items():
                messages.append((build_movie_message(site_nm, mov_nm, group, site_no),
                                 [key], booking_button(site_no, site_nm)))
        else:
            by_movie = {}
            for key, (mov_nm, group) in fresh.items():
                entry = by_movie.setdefault(mov_nm, [[], []])
                entry[0].extend(group)
                entry[1].append(key)
            for mov_nm, (group, keys) in by_movie.items():
                messages.append((build_schedule_message(site_nm, mov_nm, group, site_no),
                                 keys, booking_button(site_no, site_nm)))

    # 설정에서 지워진 대상의 기록은 함께 버린다. 나중에 다시 추가하면
    # 그때 새 대상으로 보고 기준선을 다시 잡는 게 맞다.
    live_ids = {target_id(t) for t in cfg["targets"]}
    state["baselined"] = sorted(baselined & live_ids)
    if "screen_counts" in state:
        state["screen_counts"] = {
            k: v for k, v in state["screen_counts"].items() if k in live_ids}
    if blocked:
        # 무엇이 막혔는지 남긴다. 'blocked: true' 만 있으면 몇 주를 돌려도
        # 어느 조회가 말썽인지 알 수 없다. 경고는 하루 한 번뿐이라 더욱
        # 그렇다. state 에 적어두면 아무 때나 들여다볼 수 있다.
        state["blocked_sites"] = sorted(blocked)
        if note_blocked(cfg, state):
            messages.append((build_blocked_message(blocked, cfg), [], None))
        else:
            log("  차단 경고는 최근에 이미 보냈으므로 생략")
    else:
        state["blocked"] = False
        # 키를 지우지 않고 빈 목록을 넣는다. state 병합은 '우리에게 없는
        # 키' 를 원격 값으로 남겨두기 때문에, 지우면 막혔던 기록이
        # 영원히 되살아난다. 빈 값은 제대로 전달된다.
        state["blocked_sites"] = []

    state["seen"] = sorted(prune_seen(seen, alive_movie_keys or None))
    state["gates"] = new_gates
    state["initialized"] = True
    state["last_run"] = datetime.now(KST).isoformat(timespec="seconds")
    return messages, state


def deliver(messages, state, dry_run):
    """알림 전송. 전송에 성공한 것만 seen 에 넣는다."""
    seen = set(state["seen"])
    sent = 0
    for text, keys, keyboard in messages:
        if dry_run:
            print("\n----- (dry-run) 전송하지 않음 -----")
            print(text)
            print("-----------------------------------\n")
            continue
        notifier.send(text, keyboard=keyboard)
        seen.update(keys)
        sent += 1
        log("  알림 전송 완료")
    if not dry_run:
        state["seen"] = sorted(seen)
    return sent


def save_config(cfg):
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_summary():
    """/check 명령 응답. 지금 조회한 현황을 짧게 정리한다."""
    cfg = load_config()
    if not cfg["targets"]:
        return "감시 중인 대상이 없습니다. /add 로 추가하세요."
    lines = []
    for target in cfg["targets"]:
        unit = granularity_of(target)
        if unit == "listing":
            lines.append("{} — 전국 예매 목록".format(describe(target)))
            continue
        site_no = target["site_no"]
        if unit in ("date", "screen"):
            # 시간표를 조회하지 않는 대상이다. 회차를 세어봐야 403 만 난다.
            try:
                snap = gate_snapshot(site_no, dates_only=(unit == "date"))
            except Exception as exc:
                lines.append("{} — 조회 실패: {}".format(
                    describe(target), str(exc)[:60]))
                continue
            if unit == "date":
                wanted = sorted(watch_dates_of(target))
                hit = [d for d in snap["dates"] if not wanted or d in wanted]
                lines.append("{} — {}".format(
                    describe(target),
                    "열림: " + ", ".join(fmt_date(d) for d in hit[:6])
                    if hit else "아직 안 열림"))
            else:
                lines.append("{} — {}".format(
                    describe(target), fmt_screens(snap.get("screens"))))
            continue
        try:
            dates = gate_snapshot(site_no)["dates"]
            rows = select_rows(scan_site(site_no, dates),
                               screen_of(target), target.get("movie_keyword", ""))
        except Exception as exc:
            lines.append("{} — 조회 실패: {}".format(describe(target), exc))
            continue
        lines.append("<b>{}</b>".format(describe(target)))
        if not rows:
            lines.append("  현재 열린 회차 없음")
        else:
            movies = sorted({r.get("movNm") for r in rows})
            days = sorted({r.get("scnYmd") for r in rows})
            lines.append("  {}회차 · {} ~ {}".format(
                len(rows), fmt_date(days[0]), fmt_date(days[-1])))
            for mov in movies[:6]:
                cnt = sum(1 for r in rows if r.get("movNm") == mov)
                lines.append("  · {} ({}회)".format(mov, cnt))
        lines.append("")
    return "\n".join(lines).strip()


def serve_until(cfg, seconds):
    """다음 감시까지 기다리는 동안 텔레그램 명령을 즉시 처리한다.

    그냥 sleep 하면 버리는 시간이다. 텔레그램 long polling 으로 바꾸면
    버튼을 눌렀을 때 1초 안에 반응한다. CGV 요청은 늘지 않는다.
    """
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 1:
            return
        state = load_state()
        try:
            changed, _ = telegram_bot.handle(
                cfg, state, describe, check_summary,
                long_poll=min(25, int(remaining)))
        except Exception as exc:
            log("봇 명령 처리 실패(무시): {}".format(exc))
            time.sleep(min(5, remaining))
            continue
        if changed:
            save_config(cfg)
            log("설정 변경됨 → 감시 대상 {}건".format(len(cfg["targets"])))
        save_state(state)


def cycle(cfg, dry_run):
    state = load_state()
    was_first = not state.get("initialized", False)
    # blocked 여부로 걸지 않는다. run_once 가 정상 복귀 시 blocked 를
    # 내리므로, 그걸로 걸면 복구 알림 전송이 한 번 실패했을 때 다시 보낼
    # 기회가 사라진다. blocked_at 은 전송에 성공했을 때만 지운다.
    blocked_at = state.get("blocked_at")
    block_warned = bool(state.get("block_warned"))

    # 텔레그램으로 들어온 설정 명령을 먼저 처리한다.
    if not dry_run:
        try:
            changed, _ = telegram_bot.handle(cfg, state, describe, check_summary)
            if changed:
                save_config(cfg)
                log("설정이 텔레그램에서 변경되었습니다: {}건".format(len(cfg["targets"])))
        except Exception as exc:
            log("봇 명령 처리 실패(무시): {}".format(exc))

    messages, state = run_once(cfg, state, dry_run)
    sent = deliver(messages, state, dry_run)

    # 여기까지 왔으면 조회가 정상이었다는 뜻이다. 직전에 403으로 막혀
    # 있었다면 풀렸다고 알린다. 전송에 성공했을 때만 기록을 지워서,
    # 텔레그램이 잠시 죽어 있었으면 다음 실행에서 다시 시도한다.
    if blocked_at and not dry_run and not state.get("blocked"):
        if not block_warned:
            # 막혔다고 알린 적이 없으면 풀렸다고 알릴 것도 없다. 사용자
            # 입장에서는 아무 일도 없었던 것이다. 기록만 조용히 치운다.
            state["blocked_at"] = None
            log("  잠깐 막혔다 풀림(경고 안 나갔음) — 복구 알림 생략")
        else:
            try:
                notifier.send(build_recovered_message(blocked_at))
                state["blocked_at"] = None
                state["block_warned"] = False
                state["blocked"] = False
                log("  복구 알림 전송 완료")
            except Exception as exc:
                log("복구 알림 실패(무시): {}".format(exc))

    if not dry_run:
        save_state(state)

    if was_first and not dry_run and cfg["targets"]:
        try:
            notifier.send(
                "✅ <b>CGV 예매 오픈 알리미가 시작됐습니다.</b>\n\n"
                + "\n".join("  · " + describe(t) for t in cfg["targets"])
                + "\n\n현재 열려 있는 회차는 기준선으로 저장했고, "
                  "이제부터 <b>새로 열리는 것</b>만 알려드립니다."
            )
        except Exception as exc:
            log("시작 알림 실패(무시): {}".format(exc))
    return sent


def main():
    parser = argparse.ArgumentParser(description="CGV 예매 오픈 감시")
    parser.add_argument("--loop", type=int, metavar="SEC",
                        help="지정한 초 간격으로 계속 실행")
    parser.add_argument("--dry-run", action="store_true",
                        help="알림을 보내지 않고 결과만 출력")
    args = parser.parse_args()

    cfg = load_config()
    log("감시 대상 {}건, 강제 전체 스캔 주기 {}분".format(
        len(cfg["targets"]), full_scan_minutes(cfg)))

    while True:
        try:
            cycle(cfg, args.dry_run)
        except chains.BLOCKED_ERRORS as exc:
            log("403 차단: {}".format(exc))
            if mark_blocked(cfg):
                try:
                    notifier.send(
                        "⚠️ <b>CGV 접근이 403으로 차단됐습니다.</b>\n\n"
                        "Cloudflare가 이번 요청을 봇으로 판정했습니다. "
                        "이번 조회만 건너뛰고 <b>다음 실행에서 자동으로 다시 "
                        "시도</b>합니다. 정상으로 돌아오면 복구 알림을 보냅니다.\n\n"
                        "복구 알림 없이 이 경고만 계속 반복되면 그때는 "
                        "cgv_api.UA 를 확인해야 합니다.\n\n"
                        "<code>{}</code>".format(html.escape(str(exc)[:500]))
                    )
                except Exception as inner:
                    log("경고 알림도 실패: {}".format(inner))
            else:
                log("경고는 최근에 이미 보냈으므로 생략")
            # 루프 모드에서도 즉시 종료한다. 계속 돌아봐야 막힌 서버를
            # 두드릴 뿐이고, 다음 트리거 때 재시도된다.
            return 1
        except Exception:
            log("예상치 못한 오류:\n" + traceback.format_exc())
            if not args.loop:
                return 1

        if not args.loop:
            return 0

        # 새벽에는 감속하고, 간격에 지터를 줘서 정확히 N초마다 오는
        # 기계적인 패턴을 피한다.
        interval = args.loop
        if in_quiet_hours(cfg):
            interval *= int(cfg.get("quiet_hours", {}).get("slowdown", 10))
        interval *= random.uniform(0.9, 1.15)

        if args.dry_run:
            time.sleep(interval)
        else:
            serve_until(cfg, interval)


if __name__ == "__main__":
    sys.exit(main())
