#!/usr/bin/env python3
"""12월 날짜가 이미 열렸는지 본다. 막히지 않은 조회만 쓴다. 확인 후 지운다."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import cgv_api

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y%m%d")
print("KST", datetime.now(KST).isoformat(timespec="seconds"))

SITES = [("0345", "대구"), ("0089", "센텀시티"), ("0128", "울산삼산"),
         ("0013", "용산아이파크몰")]

for no, nm in SITES:
    print("\n" + "=" * 56)
    print("CGV {} ({})".format(nm, no))
    try:
        dates = sorted(cgv_api.get_open_dates(no))
    except Exception as exc:
        print("  날짜 조회 실패: {}: {}".format(type(exc).__name__, str(exc)[:80]))
        continue
    if not dates:
        print("  열린 날짜 없음")
        continue
    # 연속 구간과 뚝 떨어진 날짜를 구분해서 보여준다.
    # 사전예매는 '한참 뒤 날짜 하나'로 튀어나온다.
    runs = []
    for d in dates:
        dt = datetime.strptime(d, "%Y%m%d")
        if runs and (dt - runs[-1][-1]).days == 1:
            runs[-1].append(dt)
        else:
            runs.append([dt])
    print("  열린 날짜 {}개".format(len(dates)))
    for r in runs:
        if len(r) == 1:
            print("    {}  (단독 날짜 <= 사전예매/특별상영 신호)".format(
                r[0].strftime("%m/%d")))
        else:
            print("    {} ~ {}  ({}일 연속)".format(
                r[0].strftime("%m/%d"), r[-1].strftime("%m/%d"), len(r)))
    far = [d for d in dates if d >= "20261201"]
    print("  12월 이후 열린 날짜: {}".format(far or "아직 없음"))
    try:
        scr = cgv_api.get_special_screens(no)
        print("  특별관 현황: {}".format(
            {s["comCdvalNm"]: s.get("schdCnt") for s in scr if s.get("comCdvalNm")}))
    except Exception as exc:
        print("  특별관 조회 실패: {}".format(type(exc).__name__))
