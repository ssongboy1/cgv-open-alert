#!/usr/bin/env python3
"""의심 두 가지를 확인하는 임시 스크립트. 확인 후 지운다.

1. 게이트가 시간에 따라 흔들리는가
   응답의 movieFormList 는 '지나간 회차'가 빠져서 온다. 그러면
   movieList 의 formAt='Y' 편수도 같이 줄어드는가?
   줄어든다면 내 게이트는 하루 종일 바뀌고, 그때마다 23일치 전체
   스캔(23요청)이 헛돈다.

2. 지점명 중 가장 긴 것이 텔레그램 콜백 64바이트를 넘기는가
"""

from __future__ import annotations

import html
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://www.megabox.co.kr",
    "Referer": "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/simpleBookingPage.do",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
}
COEX = "1351"


def fetch(brch, ymd):
    body = {"arrMovieNo": "", "playDe": ymd, "brchNoListCnt": 1,
            "brchNo1": brch, "brchNo2": "", "brchNo3": "",
            "areaCd1": "", "areaCd2": "", "areaCd3": "",
            "spclbYn1": "", "spclbYn2": "", "spclbYn3": "",
            "theabKindCd1": "", "theabKindCd2": "", "theabKindCd3": "",
            "brchAll": "", "brchSpcl": "", "movieNo1": "", "movieNo2": "",
            "movieNo3": "", "sellChnlCd": ""}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def look(label, ymd):
    d = fetch(COEX, ymd)
    rows = d.get("movieFormList") or []
    showing = {r.get("movieNm") for r in rows}
    form_y = [m for m in (d.get("movieList") or []) if m.get("formAt") == "Y"]
    times = sorted(r.get("playStartTime") for r in rows if r.get("playStartTime"))
    print("\n[{}] {}".format(label, ymd))
    print("  남은 회차 수          : {}".format(len(rows)))
    print("  첫 회차 / 마지막 회차 : {} / {}".format(
        times[0] if times else "-", times[-1] if times else "-"))
    print("  회차에 등장하는 영화  : {}편".format(len(showing)))
    print("  movieList formAt=Y    : {}편   <- 내 게이트가 세는 값".format(len(form_y)))
    print("  두 값이 같은가        : {}".format(
        "예 → 시간이 갈수록 같이 줄어든다 (게이트 요동)"
        if len(showing) == len(form_y) else
        "아니오 → formAt 은 그날 전체 기준 (게이트 안정)"))
    return len(form_y)


def main():
    now = datetime.now(KST)
    print("지금(KST):", now.strftime("%Y-%m-%d %H:%M"))
    today = now.strftime("%Y%m%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y%m%d")

    look("오늘", today)
    time.sleep(2)
    look("내일", tomorrow)

    time.sleep(2)
    print("\n[지점명 길이] 텔레그램 콜백은 64바이트 제한")
    data = fetch(COEX, today)
    names = [html.unescape(b.get("brchNm") or "")
             for b in (data.get("areaBrchList") or [])]
    worst = sorted(names, key=lambda n: len(n.encode("utf-8")))[-5:]
    for n in worst:
        cb = "s|MB0019|{}".format(n[:20])
        print("  {:<22} 콜백 {}바이트 {}".format(
            n, len(cb.encode("utf-8")),
            "← 초과!" if len(cb.encode("utf-8")) > 64 else ""))


if __name__ == "__main__":
    main()
