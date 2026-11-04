#!/usr/bin/env python3
"""알리미 자체 점검.

    python selftest.py          로직 테스트만 (CGV 호출 없음, 몇 초)
    python selftest.py --live   실제 CGV/텔레그램 연결까지 확인

CGV 응답을 가짜로 넣어 결정적으로 돌린다. 지금까지 실제로 터졌던
버그들을 회귀 테스트로 박아둔다.
"""

from __future__ import annotations

import copy
import sys
import time
from datetime import datetime, timedelta

import cgv_api
import chains
import megabox_api
import merge_state
import watcher

PASS, FAIL = [], []


def ymd(offset):
    """오늘 기준 상영일자.

    상영일자를 고정 날짜로 박아두면 그 날이 지나는 순간 prune_seen 이
    회차 키를 정상적으로 버리고, 그 탓에 멀쩡한 로직 테스트가 통째로
    거짓 실패한다. 실제로 [1][2] 가 그렇게 몇 주 동안 죽어 있었다.
    """
    return (datetime.now(watcher.KST) + timedelta(days=offset)).strftime("%Y%m%d")


D1, D2 = ymd(1), ymd(2)          # 감지 테스트용 '앞으로 열릴' 상영일자


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  {} {}{}".format("PASS" if cond else "FAIL", name,
                             "" if cond else "  <- " + str(detail)))


# ------------------------------------------------------------ 가짜 CGV

def row(site, mov_no, mov_nm, ymd, tm, scns="018", grade="아이맥스",
        fmt="IMAX LASER 2D", free="10", total="241"):
    return {"siteNo": site, "movNo": mov_no, "movNm": mov_nm, "scnYmd": ymd,
            "scnsrtTm": tm, "scnsNo": scns, "tcscnsGradNm": grade,
            "movkndDsplNm": fmt, "scnsNm": "IMAX관",
            "frSeatCnt": free, "stcnt": total}


class FakeCgv:
    """지점별 시간표를 들고 있는 가짜 CGV."""

    def __init__(self):
        self.data = {}                 # site -> {ymd: [row, ...]}
        self.gate_calls = 0
        self.schedule_calls = 0

    def reset(self):
        self.data.clear()
        self.gate_calls = self.schedule_calls = 0

    def set(self, site, ymd, rows):
        self.data.setdefault(site, {})[ymd] = rows

    def install(self):
        self._orig = (cgv_api.get_special_screens, cgv_api.get_open_dates,
                      cgv_api.get_schedules, cgv_api._jitter)
        cgv_api.get_special_screens = self._screens
        cgv_api.get_open_dates = self._dates
        cgv_api.get_schedules = self._sched
        cgv_api._jitter = lambda: None
        return self

    def restore(self):
        (cgv_api.get_special_screens, cgv_api.get_open_dates,
         cgv_api.get_schedules, cgv_api._jitter) = self._orig

    def _screens(self, site):
        """실제 CGV 는 특별관만 돌려준다. 일반관은 이 목록에 없다."""
        self.gate_calls += 1
        counts = {}
        for day in self.data.get(site, {}).values():
            for r in day:
                grade = r["tcscnsGradNm"]
                if grade == "일반":
                    continue
                counts.setdefault(grade, set()).add(r["movNm"])
        return [{"comCdvalNm": g, "schdCnt": str(len(m))}
                for g, m in sorted(counts.items())]

    def _dates(self, site):
        return sorted(self.data.get(site, {}))

    def _sched(self, site, ymd):
        self.schedule_calls += 1
        return list(self.data.get(site, {}).get(ymd, []))


def old_scan_time():
    """전체 스캔 주기가 지난 것처럼 보이게 하는 시각."""
    return (datetime.now(watcher.KST) - timedelta(hours=2)).isoformat(
        timespec="seconds")


def fresh_state():
    return {"initialized": False, "run_no": 0, "seen": [], "gates": {}}


def target(site="0345", nm="대구", screen="아이맥스", kw="오디세이",
           notify="schedule"):
    return {"site_no": site, "site_nm": nm, "screen": screen,
            "movie_keyword": kw, "notify": notify}


def config(*targets, **kw):
    cfg = {"targets": list(targets), "full_scan_every_minutes": 30,
           "notify_on_first_run": False}
    cfg.update(kw)
    return cfg


def run(cfg, state):
    msgs, state = watcher.run_once(cfg, state, dry_run=True)
    # dry_run 이면 deliver 가 seen 을 갱신하지 않으므로 실제 전송을 흉내낸다
    seen = set(state["seen"])
    for _text, keys, _kb in msgs:
        seen.update(keys)
    state["seen"] = sorted(seen)
    return msgs, state


# ------------------------------------------------------------ 테스트

def test_core(fake):
    print("\n[1] 기본 감지")
    fake.reset()
    fake.set("0345", D1, [
        row("0345", "M1", "오디세이", D1, "0800"),
        row("0345", "M1", "오디세이", D1, "1120"),
    ])
    cfg, st = config(target()), fresh_state()

    msgs, st = run(cfg, st)
    check("첫 실행은 알리지 않고 기준선만 잡는다", len(msgs) == 0, msgs)
    check("기준선에 회차가 기록된다", len(st["seen"]) == 2, st["seen"])

    msgs, st = run(cfg, st)
    check("변화가 없으면 알리지 않는다", len(msgs) == 0, msgs)

    # (가) 새 날짜가 열리는 경우 -- 실제 예매 오픈의 주된 형태.
    #      게이트의 날짜 목록이 바뀌므로 즉시 잡혀야 한다.
    fake.set("0345", D2, [row("0345", "M1", "오디세이", D2, "0800")])
    msgs, st = run(cfg, st)
    check("새 날짜가 열리면 즉시 알린다", len(msgs) == 1, msgs)
    check("새로 생긴 회차만 담는다",
          msgs and msgs[0][0].count("IMAX LASER 2D") == 1, msgs)

    msgs, st = run(cfg, st)
    check("같은 회차를 다시 알리지 않는다", len(msgs) == 0, msgs)

    # (나) 새 영화가 걸리는 경우 -- 게이트의 편수가 바뀌므로 즉시 잡힌다.
    fake.set("0777", D1, [row("0777", "M1", "오디세이", D1, "0800")])
    cfg2 = config(target(site="0777", kw=""))
    st2 = fresh_state()
    _, st2 = run(cfg2, st2)
    fake.set("0777", D1, fake.data["0777"][D1] + [
        row("0777", "M7", "신작", D1, "2000")])
    msgs, st2 = run(cfg2, st2)
    check("새 영화가 걸리면 즉시 알린다", len(msgs) == 1, msgs)

    # (다) 이미 열린 날짜에 같은 영화의 회차만 추가되는 경우.
    #      게이트(편수·날짜)가 그대로라 즉시로는 못 잡는다. 설계상 그렇고,
    #      full_scan_every_minutes 마다 도는 강제 전체 스캔이 안전망이다.
    fake.set("0345", D2, fake.data["0345"][D2] + [
        row("0345", "M1", "오디세이", D2, "1500")])
    msgs, st = run(cfg, st)
    check("증회는 게이트로 즉시 잡히지 않는다 (설계상)", len(msgs) == 0, msgs)

    st["last_full_scan"] = old_scan_time()         # 다음 실행이 강제 전체 스캔
    msgs, st = run(cfg, st)
    check("증회도 강제 전체 스캔에서는 잡힌다  <- 안전망", len(msgs) == 1, msgs)


def test_new_target(fake):
    print("\n[2] 감시 대상 추가  (실제로 터졌던 버그)")
    fake.reset()
    fake.set("0128", D1, [
        row("0128", "M1", "오디세이", D1, "0730"),
        row("0128", "M1", "오디세이", D1, "1055"),
    ])
    cfg, st = config(target()), fresh_state()
    _, st = run(cfg, st)

    cfg["targets"].append(target(site="0128", nm="울산삼산"))
    msgs, st = run(cfg, st)
    check("돌고 있는 중에 대상을 추가해도 기존 회차를 알리지 않는다",
          len(msgs) == 0, msgs)
    check("추가한 대상의 회차가 기준선에 들어간다",
          len([k for k in st["seen"] if k.startswith("0128|")]) == 2, st["seen"])

    fake.set("0128", D2, [row("0128", "M1", "오디세이", D2, "1420")])
    msgs, st = run(cfg, st)
    check("추가한 뒤 새로 열린 회차는 알린다", len(msgs) == 1, msgs)


def test_zero_match_target(fake):
    print("\n[3] 아직 안 열린 영화 감시  (가장 중요한 경로)")
    fake.reset()
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])
    cfg = config(target(kw="아바타"))
    st = fresh_state()

    msgs, st = run(cfg, st)
    check("매칭 0건이어도 알림 없음", len(msgs) == 0, msgs)
    check("매칭 0건이어도 기준선에 기록된다",
          watcher.target_id(cfg["targets"][0]) in st["baselined"], st["baselined"])

    # 기다리던 영화가 드디어 열렸다
    fake.set("0345", D1, fake.data["0345"][D1] + [
        row("0345", "M9", "아바타-파이어 앤 애쉬", D1, "1900")])
    msgs, st = run(cfg, st)
    check("기다리던 영화가 열리면 반드시 알린다  <- 놓치면 알리미가 무의미",
          len(msgs) == 1, msgs)


def test_shared_site(fake):
    print("\n[4] 한 지점에 대상 여러 개  (실제로 터졌던 버그)")
    fake.reset()
    fake.set("0345", D1, [
        row("0345", "M1", "오디세이", D1, "0800"),
        row("0345", "M2", "스파이더맨", D1, "1000", grade="일반",
            fmt="2D"),
    ])
    cfg = config(target(kw="오디세이"),
                 target(screen="ALL", kw="", notify="movie"))
    st = fresh_state()
    _, st = run(cfg, st)

    fake.gate_calls = 0
    fake.set("0345", D2, [
        row("0345", "M1", "오디세이", D2, "1300"),
        row("0345", "M3", "신작", D2, "1500", grade="일반", fmt="2D"),
    ])
    msgs, st = run(cfg, st)
    check("게이트는 지점당 한 번만 조회한다", fake.gate_calls == 1, fake.gate_calls)
    check("두 번째 대상도 게이트 변화를 본다  <- 놓치면 조용히 실패",
          len(msgs) == 2, [m[0].splitlines()[0] for m in msgs])


def test_granularity(fake):
    print("\n[5] 알림 단위")
    fake.reset()
    fake.set("0089", D1, [
        row("0089", "M5", "신작", D1, "1000"),
        row("0089", "M5", "신작", D1, "1300"),
        row("0089", "M5", "신작", D1, "1600"),
    ])
    cfg = config(target(site="0089", nm="센텀", kw="", notify="movie"))
    st = fresh_state()
    st["baselined"] = [watcher.target_id(cfg["targets"][0])]
    st["initialized"] = True
    msgs, st = run(cfg, st)
    check("영화 단위는 회차가 3개여도 알림 1건", len(msgs) == 1, msgs)
    check("영화 단위 메시지에 회차 수가 나온다",
          msgs and "총 3회" in msgs[0][0], msgs and msgs[0][0])

    fake.set("0089", D1, fake.data["0089"][D1] + [
        row("0089", "M5", "신작", D1, "1900")])
    msgs, st = run(cfg, st)
    check("같은 영화의 회차가 늘어도 다시 알리지 않는다", len(msgs) == 0, msgs)


def test_filters(fake):
    print("\n[6] 상영관 · 제목 필터")
    fake.reset()
    fake.set("0345", D1, [
        row("0345", "M1", "오디세이", D1, "0800", grade="아이맥스"),
        row("0345", "M2", "스파이더맨", D1, "1000", grade="4DX", fmt="4DX 2D"),
        row("0345", "M3", "코난", D1, "1200", grade="일반", fmt="2D"),
    ])
    base = dict(fresh_state(), initialized=True)

    def count(tg):
        st = copy.deepcopy(base)
        st["baselined"] = [watcher.target_id(tg)]
        msgs, _ = run(config(tg), st)
        return sum(m[0].count("\n  ") for m in msgs)

    check("아이맥스 필터는 아이맥스만", count(target(kw="")) == 1)
    check("4DX 필터는 4DX만", count(target(screen="4DX", kw="")) == 1)
    check("전 상영관은 전부", count(target(screen="ALL", kw="")) == 3)
    check("빈 키워드는 모든 영화", count(target(screen="ALL", kw="")) == 3)
    check("제목 부분일치", count(target(screen="ALL", kw="스파이더")) == 1)
    check("공백 무시 매칭", watcher.matches("스파이더맨-브랜드 뉴 데이", "스파이더맨브랜드"))
    check("붙임표 무시 매칭  <- 사용자는 보통 띄어쓰기로 적는다",
          watcher.matches("스파이더맨-브랜드 뉴 데이", "스파이더맨 브랜드 뉴 데이"))
    check("대소문자 무시", watcher.matches("IMAX Special", "imax special"))


def test_prune():
    print("\n[7] state 정리")
    old, yesterday, today = ymd(-3), ymd(-1), ymd(0)
    keys = {
        "0345|M1|{}|0800|018".format(old),        # 한참 지난 회차
        "0345|M1|{}|0800|018".format(yesterday),  # 어제 회차
        "0345|M1|{}|0800|018".format(today),      # 오늘 회차
        "0345|M1|{}|0800|018".format(D1),         # 앞으로 열릴 회차
        "M|0345|M1",                       # 살아있는 영화
        "M|0345|M9",                       # 사라진 영화
        "M|0128|M7",                       # 스캔 안 한 지점
    }
    kept = watcher.prune_seen(keys, alive_movie_keys={"M|0345|M1"})
    check("한참 지난 상영일 회차는 버린다",
          not any(old in k for k in kept), kept)
    check("어제 회차는 하루 더 남긴다  <- CGV가 자정 넘어도 어제를 열어둬서, "
          "지우면 이미 알린 회차를 새 회차로 다시 알린다",
          any(yesterday in k for k in kept), kept)
    check("오늘과 앞으로의 회차는 남긴다",
          any(today in k for k in kept) and any(D1 in k for k in kept), kept)
    check("스캔한 지점에서 사라진 영화 키는 버린다", "M|0345|M9" not in kept, kept)
    check("살아있는 영화 키는 남긴다", "M|0345|M1" in kept, kept)
    check("스캔 안 한 지점의 영화 키는 건드리지 않는다", "M|0128|M7" in kept, kept)


def test_merge():
    print("\n[8] 겹쳐 실행됐을 때 state 병합")
    ours = {"seen": ["a", "b"], "baselined": ["t1"], "run_no": 12,
            "tg_offset": 500, "gates": {"0345": {"x": 1}}, "initialized": True}
    theirs = {"seen": ["b", "c"], "baselined": ["t2"], "run_no": 9,
              "tg_offset": 480, "gates": {"0089": {"y": 2}}, "initialized": True}
    m = merge_state.merge(ours, theirs)
    check("알림 기록은 합집합  <- 잃으면 중복 알림", m["seen"] == ["a", "b", "c"], m["seen"])
    check("기준선도 합집합  <- 잃으면 오픈을 놓침",
          m["baselined"] == ["t1", "t2"], m["baselined"])
    check("run_no 는 큰 쪽", m["run_no"] == 12)
    check("tg_offset 은 큰 쪽  <- 작으면 명령 재처리", m["tg_offset"] == 500)
    check("게이트는 양쪽 지점 모두 유지", set(m["gates"]) == {"0345", "0089"})

    # 합집합만 하면 '지우는 일'이 영원히 안 일어난다. 실제로 터졌던 버그 둘.
    live, dead = watcher.target_id(target()), "0345|ALL|스파이더맨|schedule"
    m = merge_state.merge({"baselined": [live]}, {"baselined": [dead]},
                          target_ids={live})
    check("삭제한 대상의 기준선은 병합에서도 지운다  <- 남으면 재등록 때 몽땅 알림",
          m["baselined"] == [live], m["baselined"])
    m = merge_state.merge({"baselined": [live]}, {"baselined": [dead]})
    check("config 를 못 읽으면 기준선을 거르지 않는다  <- 잘못 지우는 쪽이 더 나쁘다",
          m["baselined"] == sorted([live, dead]), m["baselined"])

    old_key = "0345|M1|{}|0800|018".format(ymd(-3))
    new_key = "0345|M1|{}|0800|018".format(D1)
    m = merge_state.merge({"seen": [new_key]}, {"seen": [old_key, new_key]})
    check("오래된 회차 키는 병합에서도 버린다  <- 되살아나면 state 가 계속 큰다",
          m["seen"] == [new_key], m["seen"])

    check("config 에서 감시 대상 id 를 읽어낸다",
          merge_state.valid_target_ids("config.json")
          == {watcher.target_id(t) for t in watcher.load_config()["targets"]})
    check("config 가 없으면 None  <- 거르지 않는다는 신호",
          merge_state.valid_target_ids("없는파일.json") is None)


def test_target_lifecycle(fake):
    print("\n[9] 대상 삭제 후 재등록")
    fake.reset()
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])
    tg = target()
    cfg, st = config(tg), fresh_state()
    _, st = run(cfg, st)
    check("등록 후 기준선 있음", watcher.target_id(tg) in st["baselined"])

    cfg["targets"] = []
    _, st = run(cfg, st)
    check("삭제하면 기준선 기록도 지운다", st["baselined"] == [], st["baselined"])

    cfg["targets"] = [tg]
    msgs, st = run(cfg, st)
    check("재등록하면 다시 기준선부터  <- 기존 회차를 쏟지 않는다",
          len(msgs) == 0, msgs)


def test_format():
    print("\n[10] 메시지 표기")
    # 요일 표기를 검증해야 하므로 여기만 날짜를 고정한다 (2026-08-21 = 금요일).
    # 이 테스트는 prune_seen 을 타지 않아 날짜가 지나도 썩지 않는다.
    rows = [
        row("0345", "M1", "오디세이", "20260821", "2440", free="9"),
        row("0345", "M1", "오디세이", "20260821", "0800", free="0"),
    ]
    body = watcher.build_schedule_message("대구", "오디세이", rows, site_no="0345")
    check("심야 회차를 24시 넘겨 표기 (앱과 동일)", "24:40" in body, body)
    check("매진은 '매진'으로", "매진" in body, body)
    check("잔여/총좌석 함께 표기", "9/241석" in body, body)
    check("요일 표기", "8/21(금)" in body, body)
    check("지점별 예매 링크", "siteNo=0345" in body, body)
    kb = watcher.booking_button("0345", "대구")
    check("버튼은 https (텔레그램 제약)", kb[0][0]["url"].startswith("https://"))
    check("앱 열기 중계 페이지로 연결", "open.html" in kb[0][0]["url"])
    check("HTML 특수문자 escape",
          "&amp;" in watcher.build_schedule_message(
              "대구", "A & B", rows, site_no="0345"))


def test_delivery_safety(fake):
    print("\n[11] 전송 실패 시 안전장치")
    fake.reset()
    import notifier
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])
    cfg = config(target())
    st = dict(fresh_state(), initialized=True,
              baselined=[watcher.target_id(cfg["targets"][0])])
    msgs, st = watcher.run_once(cfg, st, dry_run=True)
    check("알림 대상이 잡힌다", len(msgs) == 1)

    orig = notifier.send
    notifier.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("전송 실패"))
    try:
        watcher.deliver(msgs, st, dry_run=False)
        failed = False
    except RuntimeError:
        failed = True
    finally:
        notifier.send = orig
    check("전송이 실패하면 예외가 위로 올라간다", failed)
    check("전송 실패한 회차는 seen 에 남지 않는다  <- 다음에 다시 시도",
          not any(k in st["seen"] for k in msgs[0][1]), st["seen"])


def test_quiet_hours():
    print("\n[12] 새벽 감속")
    cfg = {"quiet_hours": {"start": 1, "end": 7, "slowdown": 10}}
    import datetime as dt
    import unittest.mock as mock
    results = {}
    for h in (0, 1, 6, 7, 12, 23):
        with mock.patch.object(watcher, "datetime") as fake_dt:
            fake_dt.now.return_value = dt.datetime(2026, 8, 20, h, tzinfo=watcher.KST)
            results[h] = watcher.in_quiet_hours(cfg)
    check("01~06시는 감속", all(results[h] for h in (1, 6)), results)
    check("00시·07시 이후는 정상", not any(results[h] for h in (0, 7, 12, 23)), results)
    check("설정이 없으면 감속 안 함", not watcher.in_quiet_hours({}))


def test_required_headers():
    """Cloudflare 통과에 필요한 헤더가 살아있는지.

    UA 는 전 엔드포인트, Referer 는 searchMovScnInfo 에 필수다.
    둘 중 하나라도 빠지면 감시가 통째로 멈춘다.
    """
    print("\n[14] Cloudflare 통과 헤더")
    sent = {}
    import urllib.request as ur
    orig = ur.Request

    def spy(url, **kw):
        req = orig(url, **kw)
        sent.update({k.lower(): v for k, v in (kw.get("headers") or {}).items()})
        return req

    ur.Request = spy
    try:
        try:
            cgv_api._get("booking/searchRegnList")
        except Exception:
            pass
    finally:
        ur.Request = orig

    check("User-Agent 를 브라우저로 보낸다",
          "mozilla" in sent.get("user-agent", "").lower(), sent.get("user-agent"))
    check("봇 도구 UA 가 아니다",
          not any(b in sent.get("user-agent", "").lower()
                  for b in ("curl", "python", "urllib", "requests")),
          sent.get("user-agent"))
    check("Referer 를 보낸다  <- searchMovScnInfo 가 없으면 403",
          "cgv.co.kr" in sent.get("referer", ""), sent.get("referer"))

    # 2026-09 부터 Cloudflare 가 추가로 요구한다. 둘 중 한 묶음만 있어도
    # 통과하지만, 한쪽 규칙이 또 바뀔 때를 대비해 둘 다 보내야 한다.
    ch = [k for k in sent if k.startswith("sec-ch-ua")]
    fetch = [k for k in sent if k.startswith("sec-fetch-")]
    check("sec-ch-ua 3종을 보낸다  <- 없으면 전 엔드포인트 403",
          len(ch) >= 3, sorted(ch))
    check("Sec-Fetch 3종을 보낸다  <- 없으면 전 엔드포인트 403",
          len(fetch) >= 3, sorted(fetch))
    check("sec-ch-ua 의 크롬 버전이 UA 와 같다",
          cgv_api.CHROME_VER in sent.get("sec-ch-ua", "")
          and cgv_api.CHROME_VER in sent.get("user-agent", ""),
          "UA={} / sec-ch-ua={}".format(
              sent.get("user-agent", "")[-30:], sent.get("sec-ch-ua")))
    check("Accept-Encoding 은 보내지 않는다  <- br/zstd 를 못 푼다",
          "accept-encoding" not in sent, sent.get("accept-encoding"))


def test_block_recovery(fake):
    print("\n[15] 403 차단과 복구 알림")
    fake.reset()
    import tempfile
    import pathlib
    import notifier
    import telegram_bot
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])

    cfg = config(target())
    sent = []
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    saved = (watcher.STATE_FILE, notifier.send, telegram_bot.handle)
    watcher.STATE_FILE = tmp
    notifier.send = lambda text, **kw: sent.append(text)
    telegram_bot.handle = lambda *a, **kw: (False, None)
    try:
        watcher.save_state(dict(fresh_state(), initialized=True,
                                baselined=[watcher.target_id(cfg["targets"][0])]))

        watcher.mark_blocked()
        check("403 이면 차단 기록이 남는다",
              watcher.load_state().get("blocked") is True)

        watcher.cycle(cfg, dry_run=False)
        check("정상으로 돌아오면 복구를 알린다  <- 없으면 죽은 줄 안다",
              any("정상으로 돌아왔습니다" in t for t in sent), sent)
        check("복구를 알렸으면 차단 기록을 지운다",
              not watcher.load_state().get("blocked"))

        sent.clear()
        watcher.cycle(cfg, dry_run=False)
        check("복구 알림은 한 번만 보낸다",
              not any("정상으로 돌아왔습니다" in t for t in sent), sent)

        # 복구 알림 전송이 실패했을 때. 재시도 신호는 blocked 가 아니라
        # blocked_at 이다. run_once 가 정상 복귀 시 blocked 를 내리기 때문에
        # (그래야 지점별 403 격리가 성립한다) blocked 로 걸 수 없다.
        notifier.send = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("전송 실패"))
        watcher.mark_blocked()
        watcher.cycle(cfg, dry_run=False)
        check("복구 알림 전송이 실패하면 기록을 남긴다",
              watcher.load_state().get("blocked_at") is not None,
              watcher.load_state().get("blocked_at"))

        # 약속은 '기록을 남긴다'가 아니라 '다음에 다시 보낸다'다. 그걸 직접 본다.
        sent.clear()
        notifier.send = lambda text, **kw: sent.append(text)
        watcher.cycle(cfg, dry_run=False)
        check("전송이 실패했으면 다음 실행에서 복구 알림을 다시 보낸다",
              any("정상으로 돌아왔습니다" in t for t in sent), sent)
        check("다시 보냈으면 그때 기록을 지운다",
              watcher.load_state().get("blocked_at") is None)
    finally:
        watcher.STATE_FILE, notifier.send, telegram_bot.handle = saved


def test_megabox(fake):
    print("\n[16] 메가박스")
    fake.reset()

    # 2026-09-03 코엑스 실제 응답에서 그대로 가져온 항목
    raw = {
        "areaCd": "10", "areaCdNm": "서울", "brchNo": "1351", "brchNm": "코엑스",
        "playSchdlNo": "2609031351037", "theabNo": "01",
        "theabExpoNm": "르 리클라이너 5관&#40;마이어사운드&#41;",
        "theabSeatCnt": 378, "playStartTime": "15:50", "playEndTime": "18:52",
        "movieNm": "오디세이", "movieNo": "26018900",
        "restSeatCnt": 216, "totSeatCnt": 378,
        "theabKindCd": "DBC", "playDe": D1, "bokdAbleAt": "Y",
    }
    row = megabox_api.to_row(raw)
    check("영화·날짜를 옮긴다", row["movNm"] == "오디세이" and row["scnYmd"] == D1, row)
    check("시작시간의 콜론을 뗀다 (15:50 -> 1550)  <- fmt_time 이 그대로 동작",
          row["scnsrtTm"] == "1550", row)
    check("좌석을 잔여/총 으로 옮긴다", (row["frSeatCnt"], row["stcnt"]) == (216, 378), row)
    check("상영관 코드를 이름으로 (DBC -> 돌비시네마)",
          row["tcscnsGradNm"] == "돌비시네마", row)
    check("상영관 표기의 HTML 이스케이프를 푼다  <- &#40; 가 그대로 나가면 안 된다",
          row["movkndDsplNm"] == "르 리클라이너 5관(마이어사운드)", row)
    check("CGV 회차와 같은 필드 이름을 쓴다  <- watcher 를 안 고쳐도 되는 이유",
          set(row) == {"movNo", "movNm", "scnYmd", "scnsrtTm", "scnsNo",
                       "tcscnsGradNm", "movkndDsplNm", "frSeatCnt", "stcnt"},
          sorted(row))

    check("MB 접두어로 극장사를 가른다",
          chains.is_megabox("MB1351") and not chains.is_megabox("0345"))
    check("CGV 지점 값은 손대지 않는다  <- 쌓아둔 회차 기록이 그대로 유효",
          chains.code("0345") == "0345")
    check("메가박스는 접두어를 떼고 조회한다", chains.code("MB1351") == "1351")
    check("메시지에 붙는 극장사 이름",
          (chains.label("0345"), chains.label("MB1351")) == ("CGV", "메가박스"))

    cgv_key = watcher.schedule_key("1351", {
        "movNo": "26018900", "scnYmd": D1, "scnsrtTm": "1550", "scnsNo": "01"})
    mb_key = watcher.schedule_key("MB1351", row)
    check("지점번호가 같아도 극장사가 다르면 키가 다르다  <- 서로 덮어쓰지 않는다",
          cgv_key != mb_key, (cgv_key, mb_key))
    check("메가박스 키도 prune_seen 이 그대로 처리한다  <- 자정 수정이 안 깨진다",
          mb_key in watcher.prune_seen({mb_key}))

    # 실제 감지 흐름
    store = {"dates": [D1], "rows": {D1: [raw]}}
    saved = (megabox_api.get_gate, megabox_api.get_schedules, megabox_api._jitter)
    megabox_api.get_gate = lambda site: {
        "dates": list(store["dates"]),
        "shows": str(sum(len(v) for v in store["rows"].values()))}
    megabox_api.get_schedules = lambda site, ymd: [
        megabox_api.to_row(r) for r in store["rows"].get(ymd, [])]
    megabox_api._jitter = lambda: None
    try:
        cfg = config({"site_no": "MB1351", "site_nm": "코엑스", "screen": "돌비",
                      "movie_keyword": "오디세이", "notify": "schedule"})
        msgs, st = run(cfg, fresh_state())
        check("첫 실행은 기준선만 잡는다", len(msgs) == 0, msgs)

        store["dates"].append(D2)
        store["rows"][D2] = [dict(raw, playDe=D2, playStartTime="21:10", theabNo="02")]
        msgs, st = run(cfg, st)
        check("새 날짜가 열리면 알린다", len(msgs) == 1, msgs)

        body = msgs[0][0] if msgs else ""
        check("메시지에 메가박스라고 나온다", "메가박스 코엑스" in body, body)
        check("좌석을 표시한다", "216/378석" in body, body)
        check("상영관 표기가 들어간다", "르 리클라이너" in body, body)
        check("버튼이 메가박스 예매 페이지로 간다",
              "megabox.co.kr" in watcher.booking_button("MB1351", "코엑스")[0][0]["url"])

        msgs, st = run(cfg, st)
        check("같은 회차를 다시 알리지 않는다", len(msgs) == 0, msgs)
    finally:
        (megabox_api.get_gate, megabox_api.get_schedules,
         megabox_api._jitter) = saved

    # CGV 쪽 표기가 그대로인지 (극장사 이름을 붙이면서 바뀌면 안 된다)
    cgv_body = watcher.build_schedule_message(
        "대구", "오디세이",
        [row_ for row_ in [{"scnYmd": D1, "scnsrtTm": "0800", "frSeatCnt": "10",
                            "stcnt": "241", "movkndDsplNm": "IMAX LASER 2D"}]],
        site_no="0345")
    check("CGV 메시지 표기는 그대로  <- 'CGV 대구'", "CGV 대구" in cgv_body, cgv_body)


def test_add_flow():
    print("\n[17] /add 에서 극장사 고르기")
    import telegram_bot as tb

    _, kb = tb.chain_view()
    labels = [b["text"] for row_ in kb for b in row_]
    check("극장사를 먼저 고른다", labels == ["CGV", "메가박스"], labels)

    saved = (megabox_api.get_branches, megabox_api.get_screen_kinds)
    megabox_api.get_branches = lambda: [
        {"area": "서울", "name": "코엑스", "no": "1351"},
        {"area": "서울", "name": "강남", "no": "1372"},
        {"area": "경기", "name": "고양스타필드", "no": "0010"},
    ]
    megabox_api.get_screen_kinds = lambda site: [("DBC", "돌비시네마"),
                                                 ("NOR", "일반")]
    try:
        _, kb = tb.region_view("m")
        regions = [b["text"] for row_ in kb for b in row_]
        check("메가박스 지역 목록을 가져온다", regions == ["서울", "경기"], regions)

        _, kb = tb.site_view("m", "서울")
        datas = [b["callback_data"] for row_ in kb for b in row_]
        check("지점 값에 MB 접두어가 붙는다  <- CGV 지점과 안 섞인다",
              datas == ["s|MB1351|코엑스", "s|MB1372|강남"], datas)

        _, kb = tb.screen_view("코엑스", "MB1351")
        opts = [b["text"] for row_ in kb for b in row_]
        check("그 지점에 있는 특별관만 보여준다",
              opts == ["돌비시네마", "전 상영관"], opts)
        check("일반관은 특별관 목록에서 뺀다", "일반" not in opts, opts)

        target = tb._finish({"site_no": "MB1351", "site_nm": "코엑스",
                             "screen": "돌비시네마"}, "오디세이")
        check("등록된 대상이 메가박스로 잡힌다",
              chains.is_megabox(target["site_no"]), target)
        check("설명 문구가 제대로 나온다",
              watcher.describe(target) == "코엑스/돌비시네마/오디세이 (새 회차)",
              watcher.describe(target))
        check("상영관을 안 고르면 메가박스는 전 상영관이 기본",
              tb._finish({"site_no": "MB1351", "site_nm": "코엑스"},
                         "")["screen"] == "ALL")
        check("CGV 는 기본값이 그대로 아이맥스",
              tb._finish({"site_no": "0345", "site_nm": "대구"},
                         "")["screen"] == "아이맥스")
    finally:
        (megabox_api.get_branches, megabox_api.get_screen_kinds) = saved


def test_gate_width(fake):
    print("\n[18] 게이트가 보는 범위")
    fake.reset()
    fake.set("0345", D1, [
        row("0345", "M1", "오디세이", D1, "0800"),
        row("0345", "M2", "스파이더맨", D1, "1000", grade="4DX", fmt="4DX 2D"),
    ])
    g = chains.gate("0345")
    check("게이트에 특별관 편수가 전부 들어간다  <- 예전엔 아이맥스만 봤다",
          set(g["screens"]) == {"아이맥스", "4DX"}, g)

    cfg = config(target(screen="ALL", kw=""))
    _, st = run(cfg, fresh_state())

    # 이미 열린 날짜에 + 아이맥스가 아닌 관에 + 새 영화가 끼어드는 경우.
    # 날짜 목록도 그대로, 아이맥스 편수도 그대로다.
    fake.set("0345", D1, fake.data["0345"][D1] + [
        row("0345", "M8", "신작", D1, "2000", grade="4DX", fmt="4DX 2D")])
    msgs, st = run(cfg, st)
    check("4DX 에 새 영화가 걸리면 게이트가 바로 잡는다  <- 전에는 전체 스캔까지 늦었다",
          len(msgs) == 1, msgs)

    # 일반관은 특별관 목록에 없어서 여전히 전체 스캔이 필요하다 (설계상)
    fake.set("0345", D1, fake.data["0345"][D1] + [
        row("0345", "M9", "일반신작", D1, "2200", grade="일반", fmt="2D")])
    msgs, st = run(cfg, st)
    check("일반관 새 영화는 게이트로는 못 잡는다 (설계상)", len(msgs) == 0, msgs)

    st["last_full_scan"] = old_scan_time()
    msgs, st = run(cfg, st)
    check("일반관도 정기 전체 스캔에서는 잡힌다  <- 안전망", len(msgs) == 1, msgs)


def test_commit_noise(fake):
    """커밋을 줄이려고 바꾼 두 가지가 제대로 도는지.

    main 에 커밋이 2만 개 넘게 쌓였고 거의 전부가 숫자 하나만 바뀐
    커밋이었다. 원인은 둘이다.
      (1) 전체 스캔 주기를 run_no 로 세서, run_no 를 매번 커밋해야 했다
      (2) 바뀐 게 run_no 뿐이어도 커밋했다
    """
    print("\n[19] 쓸데없는 커밋 줄이기")

    # (1) 전체 스캔 주기를 시각으로 잰다
    cfg = config(target(), full_scan_every_minutes=30)
    check("기록이 없으면 전체 스캔한다  <- 첫 실행",
          watcher.full_scan_due(cfg, {}))
    now = datetime.now(watcher.KST)
    just = {"last_full_scan": now.isoformat(timespec="seconds")}
    check("방금 했으면 안 한다", not watcher.full_scan_due(cfg, just))
    check("29분 뒤에도 안 한다",
          not watcher.full_scan_due(cfg, just, now + timedelta(minutes=29)))
    check("31분 뒤에는 한다",
          watcher.full_scan_due(cfg, just, now + timedelta(minutes=31)))
    check("기록이 깨져 있으면 그냥 한다  <- 멈추는 것보다 낫다",
          watcher.full_scan_due(cfg, {"last_full_scan": "엉터리"}))

    old_cfg = {"targets": [], "full_scan_every_runs": 45}   # 예전 설정 파일
    check("예전 설정 키(full_scan_every_runs)도 분으로 읽는다",
          watcher.full_scan_minutes(old_cfg) == 45)
    check("둘 다 없으면 30분", watcher.full_scan_minutes({}) == 30)

    # run_no 를 올려도 전체 스캔 판정은 흔들리지 않아야 한다.
    # (예전에는 run_no 가 판정 기준이라, 커밋을 생략하면 간격이 망가졌다)
    fake.reset()
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])
    st = fresh_state()
    st["last_full_scan"] = now.isoformat(timespec="seconds")
    st["run_no"] = 0
    msgs, st = run(cfg, st)
    before = st["last_full_scan"]
    for _ in range(40):                     # 예전 기준이면 중간에 걸렸을 횟수
        msgs, st = run(cfg, st)
    check("run_no 가 40 올라도 전체 스캔 시각은 그대로  <- 시각 기준이라서",
          st["last_full_scan"] == before, st["last_full_scan"])

    # 대상이 없으면 아무 기록도 남기지 않아야 한다. 남기면 '변화'로 보여서
    # 아무 일도 안 하면서 커밋만 하게 된다.
    empty_msgs, empty_st = watcher.run_once(
        config(), {"initialized": True, "run_no": 5, "seen": [], "gates": {}},
        dry_run=True)
    check("감시 대상이 없으면 전체 스캔 기록을 남기지 않는다",
          "last_full_scan" not in empty_st, empty_st.get("last_full_scan"))

    # (2) 무엇을 '의미 있는 변화'로 볼 것인가
    base = {"run_no": 10, "last_run": "2026-11-04T19:00:00+09:00",
            "seen": ["a"], "gates": {"0345": {"x": "1"}}, "tg_offset": 7}
    same = dict(base, run_no=99, last_run="2026-11-04T20:00:00+09:00")
    check("점검 횟수·시각만 바뀐 건 커밋 안 함",
          not merge_state.meaningful_diff(base, same))
    check("알림 보낸 기록이 바뀌면 커밋함",
          merge_state.meaningful_diff(base, dict(base, seen=["a", "b"])))
    check("게이트가 바뀌면 커밋함",
          merge_state.meaningful_diff(base, dict(base, gates={})))
    check("봇 오프셋이 바뀌면 커밋함  <- 안 하면 명령을 두 번 처리한다",
          merge_state.meaningful_diff(base, dict(base, tg_offset=8)))
    check("전체 스캔 시각이 바뀌면 커밋함",
          merge_state.meaningful_diff(base, dict(base, last_full_scan="x")))
    check("차단 기록이 바뀌면 커밋함",
          merge_state.meaningful_diff(base, dict(base, blocked=True)))


def test_bot_security():
    print("\n[13] 봇 보안")
    import telegram_bot
    src = open("telegram_bot.py", encoding="utf-8").read()
    check("등록된 챗 외에는 무시한다", src.count("!= my_chat") >= 2)
    check("설정한 챗으로만 보낸다", "credentials()" in src)


# ------------------------------------------------------------ 실연결

def test_live():
    print("\n[L] 실제 연결")
    try:
        ok, cnt = cgv_api.has_imax("0345")
        check("CGV 조회 (대구 아이맥스)", ok, cnt)
    except Exception as exc:
        check("CGV 조회", False, exc)

    try:
        cgv_api.UA, saved = "python-requests/2.32.0", cgv_api.UA
        blocked = False
        try:
            cgv_api.get_special_screens("0345")
        except cgv_api.CloudflareBlocked:
            blocked = True
        finally:
            cgv_api.UA = saved
        check("봇 UA 는 403 으로 차단됨 (UA 상수가 살아있다는 증거)", blocked)
    except Exception as exc:
        check("403 경로", False, exc)

    try:
        import notifier
        token, chat = notifier.credentials()
        info = notifier._call(token, "getMe", {})
        check("텔레그램 봇 연결", info.get("ok"), info)
        check("챗 ID 설정됨", bool(chat))
    except Exception as exc:
        check("텔레그램", False, exc)


def test_date_watch(fake):
    """막힌 시간표를 건드리지 않고 '그 날짜가 열렸나'만 보는 감시."""
    print("\n[20] 예매일 감시 (CGV 시간표 403 우회 아님 — 안 쓰는 것)")
    fake.reset()

    def date_target(site="0345", nm="대구", when="20261216"):
        t = {"site_no": site, "site_nm": nm, "notify": "date"}
        if when is not None:
            t["watch_date"] = when
        return t

    # --- 단위 판정
    check("notify=date 는 알림 단위가 date",
          watcher.granularity_of(date_target()) == "date")
    check("예전 설정은 그대로 schedule/movie",
          (watcher.granularity_of(target()) == "schedule"
           and watcher.granularity_of(target(notify="movie")) == "movie"))
    check("watch_date 를 하나만 적어도 목록으로 읽는다",
          watcher.watch_dates_of(date_target(when="20261216")) == {"20261216"})
    check("watch_date 를 안 적으면 '날짜 전부'",
          watcher.watch_dates_of(date_target(when=None)) == set())

    # --- 노리는 날짜가 아직 안 열렸을 때
    fake.set("0345", "20261104", [row("0345", "M1", "타짜", "20261104", "2120")])
    cfg = config(date_target())
    state = fresh_state()
    msgs, state = run(cfg, state)
    check("노리는 날짜가 없으면 알리지 않는다", not msgs, msgs)
    check("그래도 기준선은 잡는다  <- 안 잡으면 열리는 순간을 놓친다",
          watcher.target_id(cfg["targets"][0]) in state["baselined"])

    # --- 그 날짜가 열리는 순간
    before = fake.schedule_calls
    fake.set("0345", "20261216", [])
    msgs, state = run(cfg, state)
    check("노리는 날짜가 열리면 알린다", len(msgs) == 1, msgs)
    check("알림에 그 날짜가 들어간다",
          msgs and "12/16" in msgs[0][0], msgs and msgs[0][0])
    check("예매 버튼이 붙는다", msgs and msgs[0][2], msgs and msgs[0][2])
    check("시간표는 한 번도 조회하지 않는다  <- 403 인 주소다",
          fake.schedule_calls == before, fake.schedule_calls - before)

    # --- 두 번 알리지 않는다
    msgs, state = run(cfg, state)
    check("같은 날짜를 두 번 알리지 않는다", not msgs, msgs)

    # --- 새로 등록하면 이미 열린 날짜는 알리지 않는다
    state2 = dict(fresh_state(), initialized=True)
    msgs, state2 = run(config(date_target()), state2)
    check("새 대상은 이미 열려 있던 날짜를 알리지 않는다", not msgs, msgs)
    check("대신 기준선으로 저장한다",
          watcher.date_key("0345", "20261216") in state2["seen"])

    # --- 날짜를 안 적으면 새로 열리는 날짜 전부
    cfg_all = config(date_target(when=None))
    state3 = dict(fresh_state(), initialized=True,
                  baselined=[watcher.target_id(cfg_all["targets"][0])],
                  seen=[watcher.date_key("0345", "20261104"),
                        watcher.date_key("0345", "20261216")])
    fake.set("0345", "20261229", [])
    msgs, state3 = run(cfg_all, state3)
    check("watch_date 가 없으면 새로 열린 날짜를 알린다", len(msgs) == 1, msgs)
    check("그 알림은 새로 생긴 날짜다",
          msgs and "12/29" in msgs[0][0], msgs and msgs[0][0])

    # --- 요청 수: 날짜만 보는 지점은 특별관 현황을 조회하지 않는다
    fake.gate_calls = 0
    run(config(date_target()), dict(fresh_state(), initialized=True))
    check("날짜만 보는 지점은 특별관 현황을 조회하지 않는다  <- 요청 1건",
          fake.gate_calls == 0, fake.gate_calls)

    fake.gate_calls = 0
    run(config(date_target(), target()), dict(fresh_state(), initialized=True))
    check("같은 지점에 회차 감시도 있으면 특별관 현황을 조회한다",
          fake.gate_calls == 1, fake.gate_calls)

    # --- 오래된 예매일 키는 버린다
    old = (datetime.now(watcher.KST) - timedelta(days=5)).strftime("%Y%m%d")
    keep = (datetime.now(watcher.KST) + timedelta(days=5)).strftime("%Y%m%d")
    kept = watcher.prune_seen({watcher.date_key("0345", old),
                               watcher.date_key("0345", keep)})
    check("지난 예매일 키는 버린다  <- 안 버리면 state.json 이 계속 큰다",
          watcher.date_key("0345", old) not in kept, sorted(kept))
    check("아직 안 지난 예매일 키는 남긴다",
          watcher.date_key("0345", keep) in kept, sorted(kept))


def test_block_isolation(fake):
    """한 극장사가 403 이어도 다른 극장사 감시는 계속 돈다."""
    print("\n[21] 지점별 403 격리")
    fake.reset()
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])

    boom = cgv_api.CloudflareBlocked("403 Forbidden: 테스트")
    saved = cgv_api.get_open_dates

    def blocked_dates(site):
        raise boom

    # CGV 는 막혔고 메가박스 대상은 정상인 상황
    mb = {"site_no": "MB7011", "site_nm": "동대구", "screen": "ALL",
          "movie_keyword": "듄", "notify": "schedule"}
    saved_mb = (megabox_api.get_gate, megabox_api.get_schedules)
    mb_calls = []
    megabox_api.get_gate = lambda site: (mb_calls.append(site) or
                                         {"screens": {}, "dates": [D1]})
    megabox_api.get_schedules = lambda site, ymd: [
        row("MB7011", "D1", "듄: 파트3", D1, "1900")]
    cgv_api.get_open_dates = blocked_dates
    try:
        cfg = config(target(), mb)
        # CGV 대상은 '새로 등록된' 상태로 둔다. 막힌 대상이 기준선을 잡은
        # 것으로 기록되면, 나중에 403 이 풀렸을 때 그동안 열려 있던 회차를
        # 전부 새것으로 알리게 된다. 그걸 보려면 새 대상이어야 한다.
        state = dict(fresh_state(), initialized=True,
                     baselined=[watcher.target_id(mb)])
        msgs, state = watcher.run_once(cfg, state, dry_run=True)

        check("CGV 가 403 이어도 예외가 올라오지 않는다  <- 예전엔 사이클이 죽었다",
              True)
        check("메가박스 게이트는 그대로 조회됐다", mb_calls == ["7011"], mb_calls)
        check("메가박스 알림은 정상으로 나온다",
              any("듄" in m[0] for m in msgs), [m[0][:40] for m in msgs])
        check("403 경고도 함께 온다",
              any("403" in m[0] for m in msgs), [m[0][:40] for m in msgs])
        check("차단 기록이 state 에 남는다", state.get("blocked") is True)
        check("무엇이 막혔는지도 남는다  <- 없으면 몇 주를 돌려도 모른다",
              state.get("blocked_sites") == ["0345"],
              state.get("blocked_sites"))

        # 막힌 대상은 기준선을 '잡았다'고 기록하면 안 된다. 그러면 다음에
        # 풀렸을 때 지금 열려 있는 회차를 몽땅 새것으로 알린다.
        check("막힌 새 대상은 기준선을 잡은 것으로 기록하지 않는다"
              "  <- 기록하면 풀릴 때 몽땅 알린다",
              watcher.target_id(target()) not in state["baselined"],
              state["baselined"])
        check("막히지 않은 대상의 기준선은 그대로 남는다",
              watcher.target_id(mb) in state["baselined"], state["baselined"])

        # 경고 간격
        state["last_block_warn"] = time.time()
        msgs2, state = watcher.run_once(cfg, state, dry_run=True)
        check("403 경고는 설정한 간격에 한 번만 보낸다",
              not any("403" in m[0] for m in msgs2),
              [m[0][:40] for m in msgs2])

        check("기본 경고 간격은 24시간", watcher.block_warn_hours({}) == 24)
        check("설정으로 간격을 바꿀 수 있다",
              watcher.block_warn_hours({"block_warn_hours": 1}) == 1)
    finally:
        cgv_api.get_open_dates = saved
        megabox_api.get_gate, megabox_api.get_schedules = saved_mb

    # 모두 정상이면 차단 기록이 내려간다
    cfg = config(target())
    state = dict(fresh_state(), initialized=True, blocked=True,
                 baselined=[watcher.target_id(cfg["targets"][0])])
    _msgs, state = watcher.run_once(cfg, state, dry_run=True)
    check("정상으로 돌아오면 차단 기록이 내려간다",
          state.get("blocked") is False, state.get("blocked"))
    check("막힌 목록도 치운다",
          "blocked_sites" not in state, state.get("blocked_sites"))


def test_screen_watch(fake):
    """특별관 편수 변화 감시. 제목이 없어도 '새 영화 들어옴'은 잡는다."""
    print("\n[22] 특별관 편수 감시")
    fake.reset()

    def scr(site="0345", nm="대구", screen="아이맥스"):
        return {"site_no": site, "site_nm": nm,
                "screen": screen, "notify": "screen"}

    check("notify=screen 은 알림 단위가 screen",
          watcher.granularity_of(scr()) == "screen")
    check("설명에 지점과 상영관이 들어간다",
          watcher.describe(scr()) == "대구 아이맥스 편수",
          watcher.describe(scr()))

    # 아이맥스 1편 상태에서 시작
    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])
    cfg = config(scr())
    state = fresh_state()
    msgs, state = run(cfg, state)
    check("새 대상이면 지금 편수는 기준선으로만 잡는다", not msgs, msgs)
    check("기준선 키가 저장된다",
          watcher.screen_key("0345", "아이맥스", "1") in state["seen"],
          state["seen"])

    # 같은 편수면 조용하다
    msgs, state = run(cfg, state)
    check("편수가 그대로면 알리지 않는다", not msgs, msgs)

    # 아이맥스에 새 영화가 들어온다: 1편 -> 2편
    fake.set("0345", D2, [row("0345", "M2", "듄", D2, "1900")])
    msgs, state = run(cfg, state)
    check("편수가 늘면 알린다", len(msgs) == 1, msgs)
    check("알림에 1편 -> 2편 이 들어간다",
          msgs and "1편" in msgs[0][0] and "2편" in msgs[0][0],
          msgs and msgs[0][0])
    check("예매 버튼이 붙는다", msgs and msgs[0][2])

    msgs, state = run(cfg, state)
    check("같은 편수를 두 번 알리지 않는다", not msgs, msgs)

    # 다른 상영관 편수가 바뀌어도 아이맥스 감시는 조용하다
    fake.set("0345", D2, fake.data["0345"][D2] + [
        row("0345", "M3", "코난", D2, "1200", grade="4DX", fmt="4DX 2D")])
    msgs, state = run(cfg, state)
    check("다른 상영관이 바뀌어도 아이맥스 감시는 조용하다", not msgs, msgs)

    # 전송 실패를 흉내낸다: seen 에 안 들어가면 다음에 다시 알린다
    fake.set("0345", D1, fake.data["0345"][D1] + [
        row("0345", "M4", "아바타", D1, "2100")])
    import copy as _copy
    msgs, _state2 = watcher.run_once(cfg, _copy.deepcopy(state), dry_run=True)
    check("편수가 또 늘면 알린다", len(msgs) == 1, msgs)
    check("알림에 직전 편수가 제대로 들어간다",
          msgs and "2편" in msgs[0][0] and "3편" in msgs[0][0],
          msgs and msgs[0][0])
    # deliver 가 실패해 seen 이 갱신되지 않은 상태를 그대로 다시 돌린다
    msgs2, _ = watcher.run_once(cfg, _copy.deepcopy(state), dry_run=True)
    check("전송이 실패했으면 다음 실행에서 다시 알린다  <- 저장해두고 비교하면 놓친다",
          len(msgs2) == 1, msgs2)
    check("다시 보낼 때도 직전 편수가 그대로다  <- '3편 -> 3편' 이 나가면 안 된다",
          msgs2 and "2편" in msgs2[0][0] and "3편" in msgs2[0][0],
          msgs2 and msgs2[0][0])

    # 설정에서 빼면 편수 기록도 함께 지워진다
    _msgs, state3 = run(config(target()), dict(state))
    check("지운 대상의 편수 기록은 남지 않는다",
          not state3.get("screen_counts"), state3.get("screen_counts"))

    # 요청 수: 편수 감시는 게이트에 들어 있는 값이라 추가 요청이 없다
    fake.gate_calls = 0
    fake.schedule_calls = 0
    run(config(scr()), dict(fresh_state(), initialized=True))
    check("편수 감시는 게이트 1회 말고 추가 요청이 없다",
          fake.gate_calls == 1 and fake.schedule_calls == 0,
          (fake.gate_calls, fake.schedule_calls))


def test_empty_screen_response(fake):
    """특별관 조회가 빈 응답일 때 '0편' 으로 읽으면 헛알림이 간다."""
    print("\n[23] 특별관 빈 응답")
    fake.reset()
    scr = {"site_no": "0345", "site_nm": "대구",
           "screen": "아이맥스", "notify": "screen"}

    fake.set("0345", D1, [row("0345", "M1", "오디세이", D1, "0800")])
    cfg = config(scr)
    state = fresh_state()
    _msgs, state = run(cfg, state)
    check("기준선: 아이맥스 1편",
          watcher.screen_key("0345", "아이맥스", "1") in state["seen"],
          state["seen"])

    # CGV 가 특별관 목록을 빈 배열로 준다. 라이브에서 실제로 일어났다.
    saved = cgv_api.get_special_screens
    cgv_api.get_special_screens = lambda site: []
    try:
        msgs, state = run(cfg, state)
        check("빈 응답을 0편으로 읽지 않는다  <- 읽으면 헛알림이 간다",
              not msgs, msgs)
        check("빈 응답일 때 0편 기록을 남기지 않는다",
              watcher.screen_key("0345", "아이맥스", "0") not in state["seen"],
              state["seen"])
    finally:
        cgv_api.get_special_screens = saved

    msgs, state = run(cfg, state)
    check("응답이 돌아오면 다시 조용하다  <- 복귀가 변화로 보이면 안 된다",
          not msgs, msgs)

    fake.set("0345", D2, [row("0345", "M2", "듄", D2, "1900")])
    msgs, state = run(cfg, state)
    check("진짜로 늘면 그때는 알린다", len(msgs) == 1, msgs)


def test_two_date_targets(fake):
    """같은 지점에 '그 날짜' 감시와 '새 날짜 아무거나' 감시를 같이 건다."""
    print("\n[25] 한 지점에 날짜 감시 두 개")
    fake.reset()

    aimed = {"site_no": "0345", "site_nm": "대구", "notify": "date",
             "watch_date": "20261216", "movie_keyword": "듄"}
    anyd = {"site_no": "0345", "site_nm": "대구", "notify": "date",
            "movie_keyword": "듄"}

    check("둘의 id 가 다르다  <- 같으면 한쪽만 동작한다",
          watcher.target_id(aimed) != watcher.target_id(anyd),
          (watcher.target_id(aimed), watcher.target_id(anyd)))
    check("노리는 날짜가 id 에 들어간다",
          "20261216" in watcher.target_id(aimed), watcher.target_id(aimed))

    fake.set("0345", "20261104", [row("0345", "M1", "타짜", "20261104", "2120")])
    cfg = config(aimed, anyd)
    state = fresh_state()
    msgs, state = run(cfg, state)
    check("첫 실행에는 둘 다 조용하다", not msgs, msgs)
    check("둘 다 기준선을 잡는다",
          len(state["baselined"]) == 2, state["baselined"])

    # 정규 시간표가 풀려 새 날짜가 생긴다 (12/16 은 아직 아님)
    fake.set("0345", "20261112", [])
    msgs, state = run(cfg, state)
    check("새 날짜가 열리면 '아무거나' 쪽이 알린다", len(msgs) == 1, msgs)
    check("그 알림은 새로 생긴 날짜다",
          msgs and "11/12" in msgs[0][0], msgs and msgs[0][0])

    # 드디어 12/16 이 열린다
    fake.set("0345", "20261216", [])
    msgs, state = run(cfg, state)
    check("12/16 이 열리면 두 대상이 각각 알린다  <- 노린 쪽과 아무거나 쪽",
          len(msgs) == 2, [m[0][:30] for m in msgs])
    check("둘 다 12/16 을 가리킨다",
          all("12/16" in m[0] for m in msgs), [m[0][:40] for m in msgs])

    msgs, state = run(cfg, state)
    check("두 번 알리지 않는다", not msgs, msgs)


def test_all_screens_watch(fake):
    """특별관 전체를 한 덩어리로 보는 감시. 아이맥스 숫자만으로는 놓친다."""
    print("\n[26] 특별관 전체 감시")
    fake.reset()

    alls = {"site_no": "0345", "site_nm": "대구",
            "screen": "ALL", "notify": "screen"}
    imax = {"site_no": "0345", "site_nm": "대구",
            "screen": "아이맥스", "notify": "screen"}

    check("전체 감시와 아이맥스 감시는 다른 대상",
          watcher.target_id(alls) != watcher.target_id(imax))
    check("설명이 구분된다",
          watcher.describe(alls) == "대구 특별관 전체"
          and watcher.describe(imax) == "대구 아이맥스 편수",
          (watcher.describe(alls), watcher.describe(imax)))

    # 아이맥스 1편, 4DX 1편으로 시작
    fake.set("0345", D1, [
        row("0345", "M1", "오디세이", D1, "0800"),
        row("0345", "M2", "코난", D1, "1000", grade="4DX", fmt="4DX 2D")])
    cfg = config(alls, imax)
    state = fresh_state()
    msgs, state = run(cfg, state)
    check("첫 실행은 둘 다 기준선만", not msgs, msgs)

    # 4DX 에만 영화가 는다. 아이맥스는 그대로.
    fake.set("0345", D2, [
        row("0345", "M3", "스파이더맨", D2, "1500", grade="4DX", fmt="4DX 2D")])
    msgs, state = run(cfg, state)
    check("4DX 만 바뀌면 전체 감시만 알린다  <- 아이맥스 감시는 조용",
          len(msgs) == 1, [m[0][:40] for m in msgs])
    check("알림에 바뀐 현황이 들어간다",
          msgs and "4DX" in msgs[0][0] and "아이맥스" in msgs[0][0],
          msgs and msgs[0][0])

    # 아이맥스에 새 영화가 들어온다: 둘 다 알린다
    fake.set("0345", D2, fake.data["0345"][D2] + [
        row("0345", "M4", "듄", D2, "1900")])
    msgs, state = run(cfg, state)
    check("아이맥스가 늘면 둘 다 알린다", len(msgs) == 2,
          [m[0][:40] for m in msgs])

    msgs, state = run(cfg, state)
    check("두 번 알리지 않는다", not msgs, msgs)

    # 빈 응답은 전체 감시에서도 건너뛴다
    saved = cgv_api.get_special_screens
    cgv_api.get_special_screens = lambda site: []
    try:
        msgs, state = run(cfg, state)
        check("빈 응답이면 전체 감시도 건너뛴다  <- '(없음)' 으로 알리면 안 된다",
              not msgs, msgs)
    finally:
        cgv_api.get_special_screens = saved

    check("현황 한 줄 만들기",
          watcher.fmt_screens({"아이맥스": "2", "4DX": "9"})
          == "4DX 9편, 아이맥스 2편",
          watcher.fmt_screens({"아이맥스": "2", "4DX": "9"}))


def test_screen_failure_spares_dates(fake):
    """특별관이 403 이어도 같은 지점의 날짜 감시는 살아 있어야 한다.

    실제로 터졌던 회귀다. 대구에 특별관 감시를 붙이자 게이트가
    '특별관 + 날짜' 를 한 덩어리로 받게 됐는데, 특별관만 403 이 나면서
    지점 전체가 건너뛰어졌다. 멀쩡히 되는 듄3 12/16 감시까지 죽었다.
    """
    print("\n[27] 특별관 403 이 날짜 감시를 죽이면 안 된다")
    fake.reset()

    aimed = {"site_no": "0345", "site_nm": "대구", "notify": "date",
             "watch_date": "20261216"}
    alls = {"site_no": "0345", "site_nm": "대구",
            "screen": "ALL", "notify": "screen"}

    fake.set("0345", "20261104", [row("0345", "M1", "타짜", "20261104", "2120")])

    saved = cgv_api.get_special_screens
    cgv_api.get_special_screens = lambda site: (_ for _ in ()).throw(
        cgv_api.CloudflareBlocked("403 Forbidden: 특별관"))
    try:
        cfg = config(aimed, alls)
        state = fresh_state()
        msgs, state = run(cfg, state)

        check("특별관이 403 이어도 예외가 올라오지 않는다", True)
        check("날짜 감시는 기준선을 잡는다  <- 이게 안 되면 12/16 을 놓친다",
              watcher.target_id(aimed) in state["baselined"],
              state["baselined"])
        check("특별관 감시는 기준선을 안 잡는다  <- 아직 못 읽었으니까",
              watcher.target_id(alls) not in state["baselined"],
              state["baselined"])
        check("날짜는 게이트에 제대로 들어왔다",
              state["gates"]["0345"]["dates"] == ["20261104"],
              state["gates"]["0345"])
        check("특별관은 '모름'으로 남는다",
              state["gates"]["0345"]["screens"] is None,
              state["gates"]["0345"]["screens"])

        # 특별관이 계속 막혀 있어도 12/16 이 열리면 알린다
        fake.set("0345", "20261216", [])
        msgs, state = run(cfg, state)
        check("특별관이 막힌 채로도 12/16 열리면 알린다  <- 핵심",
              len(msgs) == 1 and "12/16" in msgs[0][0],
              [m[0][:40] for m in msgs])
    finally:
        cgv_api.get_special_screens = saved

    # 특별관이 돌아오면 그때 기준선을 잡는다
    msgs, state = run(cfg, state)
    check("특별관이 돌아오면 그때 기준선을 잡는다",
          watcher.target_id(alls) in state["baselined"], state["baselined"])
    check("돌아온 것만으로 알리지는 않는다", not msgs, msgs)


def test_listing_watch(fake):
    """전국 예매 목록에 그 영화가 뜨는지 본다. CGV 에서 제목을 보는 유일한 길."""
    print("\n[28] 예매 목록 감시 (제목으로 확인)")
    fake.reset()

    def mov(no, nm, rate="0.00"):
        return {"movNo": no, "movNm": nm, "atktRate": rate}

    listing = {"notify": "listing", "movie_keyword": "듄"}

    check("notify=listing 은 단위가 listing",
          watcher.granularity_of(listing) == "listing")
    check("지점 없이도 id 가 만들어진다",
          watcher.target_id(listing).startswith("CGV|"),
          watcher.target_id(listing))
    check("설명이 읽힌다",
          watcher.describe(listing) == "CGV 예매 목록에 '듄' 뜨면",
          watcher.describe(listing))

    saved = cgv_api.get_now_playing
    # 지금은 듄 1편만 올라와 있다 (실측과 같은 상황)
    now = [mov("30001342", "타짜-벨제붑의 노래", "8.1"),
           mov("30000111", "듄", "2.94")]
    cgv_api.get_now_playing = lambda: list(now)
    try:
        cfg = config(listing)
        state = fresh_state()
        msgs, state = run(cfg, state)
        check("이미 올라와 있던 '듄' 은 알리지 않는다  <- 기준선", not msgs, msgs)
        check("기준선으로 저장된다",
              watcher.listing_key("30000111", "듄") in state["seen"],
              state["seen"])

        msgs, state = run(cfg, state)
        check("변화 없으면 조용하다", not msgs, msgs)

        # 듄: 파트 3 가 목록에 올라온다 = CGV 예매가 열렸다
        now.append(mov("30009999", "듄: 파트 3", "0.00"))
        msgs, state = run(cfg, state)
        check("듄: 파트 3 가 올라오면 알린다", len(msgs) == 1, msgs)
        check("알림에 제목이 들어간다",
              msgs and "듄: 파트 3" in msgs[0][0], msgs and msgs[0][0])
        check("예매 버튼이 붙는다", msgs and msgs[0][2], msgs and msgs[0][2])

        msgs, state = run(cfg, state)
        check("두 번 알리지 않는다", not msgs, msgs)

        # 상관없는 영화는 무시한다
        now.append(mov("30008888", "아바타-파이어 앤 애쉬", "5.0"))
        msgs, state = run(cfg, state)
        check("찾는 영화가 아니면 조용하다", not msgs, msgs)

        # 403 이어도 다른 감시는 돈다
        cgv_api.get_now_playing = lambda: (_ for _ in ()).throw(
            cgv_api.CloudflareBlocked("403 Forbidden: 예매목록"))
        fake.set("MB7011", D1, [])
        mb = {"site_no": "MB7011", "site_nm": "동대구", "screen": "ALL",
              "movie_keyword": "듄", "notify": "schedule"}
        saved_mb = (megabox_api.get_gate, megabox_api.get_schedules)
        megabox_api.get_gate = lambda site: {"screens": {}, "dates": [D1]}
        megabox_api.get_schedules = lambda site, ymd: [
            row("MB7011", "D9", "듄: 파트 3", D1, "1900")]
        try:
            cfg2 = config(listing, mb)
            st = dict(fresh_state(), initialized=True,
                      baselined=[watcher.target_id(mb)])
            msgs, st = watcher.run_once(cfg2, st, dry_run=True)
            check("예매 목록이 403 이어도 메가박스 알림은 나간다",
                  any("듄" in m[0] for m in msgs), [m[0][:40] for m in msgs])
            check("차단 기록이 남는다", st.get("blocked") is True)
        finally:
            megabox_api.get_gate, megabox_api.get_schedules = saved_mb
    finally:
        cgv_api.get_now_playing = saved


def main():
    fake = FakeCgv().install()
    try:
        test_core(fake)
        test_new_target(fake)
        test_zero_match_target(fake)
        test_shared_site(fake)
        test_granularity(fake)
        test_filters(fake)
        test_prune()
        test_merge()
        test_target_lifecycle(fake)
        test_format()
        test_delivery_safety(fake)
        test_quiet_hours()
        test_required_headers()
        test_bot_security()
        test_block_recovery(fake)
        test_megabox(fake)
        test_add_flow()
        test_gate_width(fake)
        test_commit_noise(fake)
        test_date_watch(fake)
        test_block_isolation(fake)
        test_screen_watch(fake)
        test_empty_screen_response(fake)
        test_two_date_targets(fake)
        test_all_screens_watch(fake)
        test_screen_failure_spares_dates(fake)
        test_listing_watch(fake)
    finally:
        fake.restore()

    if "--live" in sys.argv:
        test_live()

    print("\n" + "=" * 52)
    print("통과 {}건 / 실패 {}건".format(len(PASS), len(FAIL)))
    for name in FAIL:
        print("  실패: " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
