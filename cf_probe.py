#!/usr/bin/env python3
"""고친 코드가 실제로 통하는지 끝까지 확인한다. 확인 후 지운다.

프로브 자체 요청 코드가 아니라 운영에 쓰이는 cgv_api / chains / watcher
경로를 그대로 돌린다. /add 가 부르는 목록 조회까지 포함한다.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone

import cgv_api
import chains
import watcher

KST = timezone(timedelta(hours=9))
DAEGU = "0345"


def show(label, fn):
    print("\n" + "=" * 62)
    print("[{}]".format(label))
    try:
        return fn()
    except Exception as exc:
        print("  실패: {}: {}".format(type(exc).__name__, exc))
        return None


def main():
    print("KST", datetime.now(KST).isoformat(timespec="seconds"))
    print("UA :", cgv_api.UA)

    regions = show("cgv_api.get_regions() - /add 가 403 나던 바로 그 호출",
                   cgv_api.get_regions)
    if regions:
        print("  지역 {}개: {}".format(
            len(regions), ", ".join(r["regnGrpNm"] for r in regions)))
        first = regions[0]["siteList"][0]
        print("  예: {} {}".format(first["siteNo"], first["siteNm"]))

    gate = show("chains.gate('0345') - 감시 게이트", lambda: chains.gate(DAEGU))
    if gate:
        print("  특별관: {}".format(json.dumps(gate["screens"], ensure_ascii=False)))
        print("  열린 날짜 {}개: {} ...".format(
            len(gate["dates"]), ", ".join(gate["dates"][:5])))

    if gate and gate["dates"]:
        ymd = gate["dates"][0]
        rows = show("chains.schedules('0345', {}) - 시간표".format(ymd),
                    lambda: chains.schedules(DAEGU, ymd))
        if rows:
            print("  회차 {}개. 첫 회차: {}".format(
                len(rows), json.dumps(rows[0], ensure_ascii=False)[:180]))

    show("cgv_api.get_now_playing() - 현재 상영작",
         lambda: print("  {}편".format(len(cgv_api.get_now_playing()))))

    print("\n" + "=" * 62)
    print("[watcher.run_once - 감지부터 메시지까지]")
    cfg = {"targets": [{"site_no": DAEGU, "site_nm": "대구", "screen": "아이맥스",
                        "movie_keyword": "", "notify": "movie"}],
           "full_scan_every_runs": 30, "notify_on_first_run": True}
    state = {"initialized": False, "run_no": 0, "seen": [], "gates": {}}
    try:
        msgs, state = watcher.run_once(cfg, state, dry_run=True)
        print("  보낼 메시지 {}건".format(len(msgs)))
        for text, keys, kb in msgs[:1]:
            print("\n--- 실제로 나갈 메시지 ---")
            print(text)
            print("  링크: {}".format(kb[0][0]["url"]))
    except Exception:
        print("  실패:\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
