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
        self.gate_calls += 1
        movies = {r["movNm"] for d in self.data.get(site, {}).values() for r in d
                  if r["tcscnsGradNm"] == "아이맥스"}
        return [{"comCdvalNm": "아이맥스", "schdCnt": str(len(movies))}]

    def _dates(self, site):
        return sorted(self.data.get(site, {}))

    def _sched(self, site, ymd):
        self.schedule_calls += 1
        return list(self.data.get(site, {}).get(ymd, []))


def fresh_state():
    return {"initialized": False, "run_no": 0, "seen": [], "gates": {}}


def target(site="0345", nm="대구", screen="아이맥스", kw="오디세이",
           notify="schedule"):
    return {"site_no": site, "site_nm": nm, "screen": screen,
            "movie_keyword": kw, "notify": notify}


def config(*targets, **kw):
    cfg = {"targets": list(targets), "full_scan_every_runs": 30,
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
    #      full_scan_every_runs 마다 도는 강제 전체 스캔이 안전망이다.
    fake.set("0345", D2, fake.data["0345"][D2] + [
        row("0345", "M1", "오디세이", D2, "1500")])
    msgs, st = run(cfg, st)
    check("증회는 게이트로 즉시 잡히지 않는다 (설계상)", len(msgs) == 0, msgs)

    st["run_no"] = cfg["full_scan_every_runs"]      # 다음 실행이 강제 전체 스캔
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

        notifier.send = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("전송 실패"))
        watcher.mark_blocked()
        watcher.cycle(cfg, dry_run=False)
        check("복구 알림 전송이 실패하면 기록을 남겨 다음에 다시 시도한다",
              watcher.load_state().get("blocked") is True)
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
        "dates": list(store["dates"]), "movie_cnt": str(len(store["rows"]))}
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
