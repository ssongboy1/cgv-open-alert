#!/usr/bin/env python3
"""CGV 차단이 풀렸는지 한 번만 본다. 요청 2건. 확인 후 지운다."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import cgv_api

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)
TOMORROW = (now + timedelta(days=1)).strftime("%Y%m%d")
SITE, NAME = "0345", "대구"

print("KST", now.isoformat(timespec="seconds"))
print("듄3 개봉(12/16)까지 {}일".format(
    (datetime(2026, 12, 16, tzinfo=KST).date() - now.date()).days))

print("\n[1] 시간표 조회 — 막혀 있던 바로 그 요청")
try:
    rows = cgv_api.get_schedules(SITE, TOMORROW)
    print("  ✅ 풀렸습니다. {} {} -> {}회차".format(NAME, TOMORROW, len(rows)))
    imax = [r for r in rows if r.get("tcscnsGradNm") == "아이맥스"]
    print("     그중 아이맥스 {}회차: {}".format(
        len(imax), sorted({r.get("movNm") for r in imax}) or "없음"))
except cgv_api.CloudflareBlocked:
    print("  ❌ 아직 403. 그대로 물러납니다.")
except Exception as exc:
    print("  ? {}: {}".format(type(exc).__name__, str(exc)[:100]))

time.sleep(2)

print("\n[2] 열린 날짜 — 12월(듄3)이 열렸는지")
try:
    dates = sorted(cgv_api.get_open_dates(SITE))
    print("  {}개: {} ~ {}".format(len(dates), dates[0], dates[-1]))
    dec = [d for d in dates if d >= "20261201"]
    print("  12월 이후: {}".format(dec or "아직 없음"))
    if "20261216" in dates:
        print("  🎬 12/16 이 열렸습니다!")
except cgv_api.CloudflareBlocked:
    print("  ❌ 403")
except Exception as exc:
    print("  ? {}: {}".format(type(exc).__name__, str(exc)[:100]))
