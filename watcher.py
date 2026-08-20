#!/usr/bin/env python3
"""CGV 예매 오픈 감시.

    python watcher.py              1회 실행 후 종료 (GitHub Actions용)
    python watcher.py --loop 30    30초 주기 상시 실행 (로컬 스퍼트용)
    python watcher.py --dry-run    알림을 보내지 않고 결과만 출력

감시 대상 (config.json 의 targets)
----------------------------------
    site_no        지점 코드            "0345"
    site_nm        지점 이름            "대구"
    screen         상영관 필터          "아이맥스" | "4DX" | "SCREENX" | "ALL"
    movie_keyword  영화 제목 부분일치    "오디세이"  (빈 문자열이면 전체)
    notify         알림 단위            "schedule" = 새 회차마다
                                        "movie"    = 새 영화가 걸릴 때 한 번만

감지 방식
---------
지점당 매 실행 2요청("게이트")만 보내고, 게이트 값이 바뀌었을 때에만
날짜별 시간표를 상세 조회한다. 게이트는

    (1) searchSscnsSchdExistList 의 아이맥스 상영 편수
    (2) searchSiteScnscYmdListBySite 의 예매 가능 날짜 목록

두 가지다. 이미 열린 날짜 안에서 회차/영화만 늘어나는 경우는 게이트로
잡히지 않으므로 full_scan_every_runs 회마다 강제로 전체 스캔한다.
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

# 사용자가 어떻게 적든 CGV의 tcscnsGradNm 값으로 맞춘다.
SCREEN_ALIASES = {
    "IMAX": "아이맥스", "imax": "아이맥스", "Imax": "아이맥스", "아이맥스": "아이맥스",
    "4DX": "4DX", "4dx": "4DX",
    "SCREENX": "SCREENX", "screenx": "SCREENX", "ScreenX": "SCREENX",
    "일반": "일반", "일반관": "일반",
    "ALL": SCREEN_ALL, "all": SCREEN_ALL, "전체": SCREEN_ALL, "전 상영관": SCREEN_ALL,
}

DEFAULT_CONFIG = {
    "targets": [],
    "full_scan_every_runs": 30,
    "notify_on_first_run": False,
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


def warn_once_per_hour():
    """403 경고를 1시간에 한 번만 보내도록 state 에 시각을 기록한다."""
    now = time.time()
    try:
        state = load_state()
    except Exception:
        return True
    if now - float(state.get("last_block_warn", 0)) < 3600:
        return False
    state["last_block_warn"] = now
    try:
        save_state(state)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- 대상 해석

def screen_of(target):
    """예전 설정의 'sscns': 'IMAX' 도 그대로 받아준다."""
    raw = target.get("screen") or target.get("sscns") or "아이맥스"
    return SCREEN_ALIASES.get(raw, raw)


def granularity_of(target):
    return "movie" if target.get("notify") == "movie" else "schedule"


def target_id(target):
    """감시 대상 하나를 식별하는 값. 기준선을 잡았는지 기록하는 데 쓴다."""
    return "{}|{}|{}|{}".format(
        target["site_no"], screen_of(target),
        normalize(target.get("movie_keyword", "")), granularity_of(target))


def describe(target):
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
    return "{}/cinema?siteNo={}&siteNm={}".format(
        BOOK_URL, site_no, urllib.parse.quote(site_nm))


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
    # 앱이 없거나 실패하면 중계 페이지가 알아서 웹 예매로 떨어뜨리므로
    # 버튼은 하나면 충분하다. 본문에도 웹 주소가 그대로 들어간다.
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


def prune_seen(seen, alive_movie_keys=None):
    """오래된 키를 버려 state.json 이 무한히 커지지 않게 한다.

    * 회차 키(5조각): 상영일자가 오늘보다 이전이면 버린다.
    * 영화 키('M|'): 그 지점을 이번에 전체 스캔했는데 더 이상 안 걸려 있으면 버린다.
      (스캔하지 않은 지점의 영화 키는 건드리지 않는다)
    """
    today = datetime.now(KST).strftime("%Y%m%d")
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
        if len(parts) == 5 and parts[2] < today:
            continue
        kept.add(key)
    return kept


# ---------------------------------------------------------------- 메시지

def build_schedule_message(site_nm, mov_nm, rows, site_no=""):
    esc = html.escape
    lines = [
        "\U0001F3AC <b>CGV 예매 오픈!</b>",
        "",
        "\U0001F4CD CGV {}".format(esc(site_nm)),
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
        "\U0001F195 <b>CGV {}에 새 영화가 열렸습니다</b>".format(esc(site_nm)),
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


# ---------------------------------------------------------------- 감시

def gate_snapshot(site_no):
    """지점당 2요청. 상세 스캔이 필요한지 판단하는 값."""
    screens = cgv_api.get_special_screens(site_no)
    imax = next((s for s in screens if s.get("comCdvalNm") == cgv_api.IMAX_GRADE), None)
    return {
        "imax_cnt": (imax or {}).get("schdCnt", "0"),
        "dates": cgv_api.get_open_dates(site_no),
    }


def scan_site(site_no, dates):
    """지점의 모든 열린 날짜에서 전체 시간표를 모은다 (상영관 필터 없음)."""
    rows = []
    for ymd in dates:
        rows.extend(cgv_api.get_schedules(site_no, ymd))
        cgv_api._jitter()
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


def run_once(cfg, state, dry_run=False):
    """1회 점검. (알림 메시지 목록, 갱신된 state) 반환."""
    state["run_no"] = state.get("run_no", 0) + 1
    every = max(1, int(cfg.get("full_scan_every_runs", 30)))
    force_scan = state["run_no"] % every == 1
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
    messages = []

    if not cfg["targets"]:
        log("감시 대상이 없습니다. 설정 프로그램에서 추가하세요.")
        # 여기서 그냥 나가면 지워진 대상의 기준선 기록이 남는다. 나중에
        # 다시 등록했을 때 '이미 기준선을 잡은 대상'으로 오해해, 그동안
        # seen 이 비워진 상태에서 현재 회차를 몽땅 알리게 된다.
        state["baselined"] = []
        state["gates"] = {}
        return messages, state

    for target in cfg["targets"]:
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

        # 게이트는 지점당 한 번만 조회한다. 같은 지점을 여러 대상이
        # 보고 있어도 요청이 늘지 않는다.
        if site_no not in new_gates:
            new_gates[site_no] = gate_snapshot(site_no)
        snap = new_gates[site_no]
        changed = old_gates.get(site_no) != snap

        if not (changed or force_scan or first_run or new_target):
            log("  {}: 변화 없음".format(label))
            continue

        why = ("새 대상 기준선" if new_target else
               "첫 실행" if first_run else
               "게이트 변화" if changed else "정기 전체 스캔")
        if site_no not in scanned:
            scanned[site_no] = scan_site(site_no, snap["dates"])
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
    state["baselined"] = sorted(baselined & {target_id(t) for t in cfg["targets"]})
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
        site_no = target["site_no"]
        try:
            dates = cgv_api.get_open_dates(site_no)
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
    log("감시 대상 {}건, 강제 전체 스캔 주기 {}회".format(
        len(cfg["targets"]), cfg.get("full_scan_every_runs")))

    while True:
        try:
            cycle(cfg, args.dry_run)
        except cgv_api.CloudflareBlocked as exc:
            log("403 차단: {}".format(exc))
            if warn_once_per_hour():
                try:
                    notifier.send(
                        "⚠️ <b>CGV 접근이 403으로 차단됐습니다.</b>\n\n"
                        "Cloudflare가 User-Agent를 봇으로 판정했거나 정책이 "
                        "강화된 것으로 보입니다. 알림이 동작하지 않습니다.\n\n"
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
