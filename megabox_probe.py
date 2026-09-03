#!/usr/bin/env python3
"""메가박스 구현에 필요한 세부값 확인용 임시 스크립트. 확인 후 지운다.

1차 프로브로 '읽을 수 있다'는 건 확인했다. 이번엔 실제 코드를 쓰기 위해
정확히 알아야 하는 것만 본다.

  1. 좌석 필드의 실제 값과 타입 (restSeatCnt / theabSeatCnt / totSeatCnt)
  2. 심야 회차 표기 - CGV는 24:35 로 넘겨 쓰는데 메가박스는 어떤가
  3. 상영관 종류 코드 (theabKindCd) 와 특별관 코드 (areaCd) 목록
  4. 예매 가능 날짜 목록의 형태 (게이트로 쓸 수 있는가)
"""

from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://www.megabox.co.kr",
    "Referer": "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/simpleBookingPage.do",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
}

COEX = "1351"


def payload(brch_no, ymd):
    return {
        "arrMovieNo": "", "playDe": ymd, "brchNoListCnt": 1,
        "brchNo1": brch_no, "brchNo2": "", "brchNo3": "",
        "areaCd1": "", "areaCd2": "", "areaCd3": "",
        "spclbYn1": "", "spclbYn2": "", "spclbYn3": "",
        "theabKindCd1": "", "theabKindCd2": "", "theabKindCd3": "",
        "brchAll": "", "brchSpcl": "",
        "movieNo1": "", "movieNo2": "", "movieNo3": "", "sellChnlCd": "",
    }


def fetch(brch_no, ymd):
    req = urllib.request.Request(
        URL, data=json.dumps(payload(brch_no, ymd)).encode("utf-8"),
        headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def main():
    today = datetime.now(KST).strftime("%Y%m%d")
    print("오늘(KST):", today)

    data = fetch(COEX, today)
    rows = data.get("movieFormList") or []
    print("\n코엑스 오늘 회차 수:", len(rows))

    print("\n" + "=" * 64)
    print("[1] 회차 항목 통째로 2건 - 좌석 필드의 실제 값과 타입 확인")
    for row in rows[:2]:
        print(json.dumps(row, ensure_ascii=False, indent=2))

    print("\n" + "=" * 64)
    print("[2] 좌석 필드 요약")
    for row in rows[:8]:
        print("  {} {} | 잔여 {!r} / 관좌석 {!r} / 총 {!r} | 예매가능 {!r}".format(
            row.get("playStartTime"), row.get("movieNm"),
            row.get("restSeatCnt"), row.get("theabSeatCnt"),
            row.get("totSeatCnt"), row.get("bokdAbleAt")))

    print("\n" + "=" * 64)
    print("[3] 시작시간 전체 - 심야 회차를 24시 넘겨 쓰는지 본다")
    times = sorted({r.get("playStartTime") for r in rows if r.get("playStartTime")})
    print("  ", ", ".join(times))
    print("   가장 이른 것:", times[0] if times else "-",
          "/ 가장 늦은 것:", times[-1] if times else "-")

    print("\n" + "=" * 64)
    print("[4] 상영관 종류 (theabKindCd -> 표기명)")
    kinds = {}
    for row in rows:
        kinds.setdefault(
            (row.get("theabKindCd"), row.get("areaCd")),
            html.unescape(row.get("theabExpoNm") or ""))
    for (kind, area), name in sorted(kinds.items(), key=lambda x: str(x[0])):
        print("   theabKindCd={!r} areaCd={!r} -> {}".format(kind, area, name))

    print("\n" + "=" * 64)
    print("[5] 특별관 코드 목록 (spclbBrchList 의 areaCd)")
    spcl = {}
    for b in data.get("spclbBrchList") or []:
        spcl.setdefault(b.get("areaCd"), b.get("areaCdNm"))
    for cd, nm in sorted(spcl.items(), key=lambda x: str(x[0])):
        print("   {!r} = {}".format(cd, nm))

    print("\n" + "=" * 64)
    print("[6] 예매 가능 날짜 목록 (게이트로 쓸 수 있는가)")
    for d in data.get("movieFormDeList") or []:
        print("   {} 예매가능={!r} ({})".format(
            d.get("playDe"), d.get("formAt"), d.get("dowKorNm")))

    print("\n" + "=" * 64)
    print("[7] 지점 목록 크기 (/add 에 쓸 수 있는가)")
    areas = {}
    for b in data.get("areaBrchList") or []:
        areas.setdefault(b.get("areaCdNm"), []).append(b.get("brchNm"))
    for area, brchs in areas.items():
        print("   {}: {}개 - {}".format(area, len(brchs), ", ".join(brchs[:5])))

    # 심야 회차는 자정 근처 지점에서 더 잘 보인다. 하루 뒤도 한 번 본다.
    time.sleep(2)
    tomorrow = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")
    rows2 = fetch(COEX, tomorrow).get("movieFormList") or []
    t2 = sorted({r.get("playStartTime") for r in rows2 if r.get("playStartTime")})
    print("\n[8] 내일({}) 시작시간: {}".format(tomorrow, ", ".join(t2)))


if __name__ == "__main__":
    main()
