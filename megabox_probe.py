#!/usr/bin/env python3
"""실제 코드 경로를 그대로 태워보는 임시 스크립트. 확인 후 지운다.

지금까지 프로브는 자체 요청 코드로 확인했다. 정작 운영에 쓰일
megabox_api / chains / watcher 경로는 한 번도 진짜 요청을 보낸 적이 없다.
여기서 그 경로를 그대로 돌린다.

파일은 건드리지 않는다. run_once 는 파일을 안 만지고, dry_run 이라
알림도 안 나간다.
"""

from __future__ import annotations

import json

import chains
import megabox_api
import watcher

COEX = "MB1351"          # chains 가 MB 를 떼고 1351 로 조회해야 한다


def show(label, fn):
    print("\n" + "=" * 60)
    print("[{}]".format(label))
    try:
        return fn()
    except Exception as exc:
        print("  실패: {}: {}".format(type(exc).__name__, exc))
        return None


def main():
    # 1. 진짜 megabox_api 로 게이트
    gate = show("chains.gate('MB1351') - 실제 게이트", lambda: chains.gate(COEX))
    if gate:
        print("  열린 날짜 {}개: {} ...".format(
            len(gate["dates"]), ", ".join(gate["dates"][:5])))
        print("  내일 회차 수: {}".format(gate.get("shows")))

    # 2. 진짜 megabox_api 로 시간표 (MB 접두어가 제대로 떨어지는지 포함)
    if gate and gate["dates"]:
        ymd = gate["dates"][0]
        rows = show("chains.schedules('MB1351', {}) - 실제 시간표".format(ymd),
                    lambda: chains.schedules(COEX, ymd))
        if rows:
            print("  회차 {}개. 첫 회차를 watcher 형태로:".format(len(rows)))
            print("  " + json.dumps(rows[0], ensure_ascii=False))
            print("  좌석 표기: {}".format(watcher.fmt_seats(rows[0])))
            print("  시간 표기: {}".format(watcher.fmt_time(rows[0]["scnsrtTm"])))
            print("  상영관 표기: {}".format(watcher.screen_label(rows[0])))

    # 3. 지점 목록 / 상영관 목록 (/add 가 쓰는 것)
    brs = show("megabox_api.get_branches() - /add 지점 목록",
               megabox_api.get_branches)
    if brs:
        print("  지점 {}개. 예: {}".format(
            len(brs), ", ".join("{}({})".format(b["name"], b["no"]) for b in brs[:3])))
    kinds = show("megabox_api.get_screen_kinds('1351') - /add 상영관",
                 lambda: megabox_api.get_screen_kinds("1351"))
    if kinds:
        print("  " + ", ".join("{}={}".format(c, n) for c, n in kinds))

    # 4. watcher 를 통째로. 감지 -> 메시지 생성까지 실제로 돌린다.
    print("\n" + "=" * 60)
    print("[watcher.run_once - 감지부터 메시지까지 실제 경로]")
    cfg = {
        "targets": [{"site_no": COEX, "site_nm": "코엑스",
                     "screen": "돌비시네마", "movie_keyword": "",
                     "notify": "movie"}],
        "full_scan_every_runs": 30, "notify_on_first_run": True,
    }
    state = {"initialized": False, "run_no": 0, "seen": [], "gates": {}}
    try:
        msgs, state = watcher.run_once(cfg, state, dry_run=True)
        print("  기준선 회차 {}건 기록됨".format(len(state["seen"])))
        print("  보낼 메시지 {}건".format(len(msgs)))
        for text, keys, kb in msgs[:2]:
            print("\n--- 실제로 나갈 메시지 ---")
            print(text)
            print("  버튼: {}".format(kb[0][0]["text"]))
            print("  링크: {}".format(kb[0][0]["url"]))
            print("  회차 키: {}".format(keys[:2]))
    except Exception as exc:
        import traceback
        print("  실패:\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
