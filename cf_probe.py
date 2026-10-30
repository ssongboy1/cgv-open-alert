#!/usr/bin/env python3
"""지금 실제로 조회가 되는지. 확인 후 지운다."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import cgv_api, chains, megabox_api

KST = timezone(timedelta(hours=9))
print("KST", datetime.now(KST).isoformat(timespec="seconds"))
print("UA ", cgv_api.UA)

def go(label, fn):
    try:
        print("  OK   {:34} {}".format(label, fn()))
        return True
    except Exception as exc:
        print("  실패 {:34} {}: {}".format(label, type(exc).__name__, str(exc)[:120]))
        return False

print("\n--- CGV ---")
go("get_regions (지역 목록)",
   lambda: "{}개 지역".format(len(cgv_api.get_regions())))
go("get_now_playing (영화 목록)",
   lambda: "{}편".format(len(cgv_api.get_now_playing())))
g = {}
def gate():
    g.update(chains.gate("0345"))
    return "특별관 {} / 열린날짜 {}개".format(g["screens"], len(g["dates"]))
go("chains.gate 0345 (대구 게이트)", gate)
if g.get("dates"):
    go("chains.schedules (시간표)",
       lambda: "{}회차".format(len(chains.schedules("0345", g["dates"][0]))))

print("\n--- 메가박스 ---")
mg = {}
def mgate():
    mg.update(chains.gate("MB7011"))
    return "열린날짜 {}개 / 내일 회차 {}".format(len(mg["dates"]), mg["shows"])
go("chains.gate MB7011 (동대구 게이트)", mgate)
if mg.get("dates"):
    go("chains.schedules (시간표)",
       lambda: "{}회차".format(len(chains.schedules("MB7011", mg["dates"][0]))))
go("get_branches (지점 목록)",
   lambda: "{}개 지점".format(len(megabox_api.get_branches())))
