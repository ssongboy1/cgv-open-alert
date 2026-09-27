#!/usr/bin/env python3
"""2차: 어느 헤더가 통과의 열쇠인지, 그리고 압축을 안전하게 줄여도 되는지.

1차 결과: 지금 코드 헤더(A)는 전 엔드포인트 403. 크롬 흉내 최대(D)는 전부 200.
D 는 Accept-Encoding 에 br 을 넣는데 파이썬 표준 라이브러리는 br 을 못 푼다.
그래서 br 을 뺀 D1 도 통하는지 반드시 확인해야 한다.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

BASE = "https://cgv.co.kr/api/v1/booking"
KST = timezone(timedelta(hours=9))
TOMORROW = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")

UA131 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA140 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
REFERER = "https://cgv.co.kr/cnm/movieBook"

CH = {
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
FETCH = {
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
BASE_HDR = {
    "User-Agent": UA140,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": REFERER,
    "Origin": "https://cgv.co.kr",
    "Connection": "keep-alive",
}

D  = dict(BASE_HDR, **CH, **FETCH, **{"Accept-Encoding": "gzip, deflate, br"})
D1 = dict(BASE_HDR, **CH, **FETCH, **{"Accept-Encoding": "gzip, deflate"})
D2 = dict(BASE_HDR, **CH, **FETCH)                      # Accept-Encoding 없음
D3 = dict(BASE_HDR, **FETCH, **{"Accept-Encoding": "gzip, deflate"})   # ch 없음
D4 = dict(BASE_HDR, **CH, **{"Accept-Encoding": "gzip, deflate"})      # fetch 없음
D5 = dict(D1); D5.pop("Origin")                         # Origin 없음
D6 = dict(D1, **{"User-Agent": UA131})                  # UA 만 옛 버전
D7 = dict(D1); D7.pop("Connection")                     # Connection 없음

SETS = [("D  br 포함(1차에서 통한 것)", D), ("D1 br 제외", D1),
        ("D2 압축헤더 없음", D2), ("D3 sec-ch-ua 없음", D3),
        ("D4 Sec-Fetch 없음", D4), ("D5 Origin 없음", D5),
        ("D6 UA만 131", D6), ("D7 Connection 없음", D7)]


def hit(path, headers, params=None):
    p = dict(params or {})
    p.setdefault("coCd", "A420")
    url = BASE + "/" + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            enc = (resp.headers.get("Content-Encoding") or "없음")
            raw = resp.read()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            try:
                body = json.loads(raw.decode("utf-8"))
                n = body.get("data")
                note = "statusCode={} 데이터 {}".format(
                    body.get("statusCode"), len(n) if isinstance(n, list) else "?")
            except Exception as exc:
                note = "본문 해석 실패 {}: {!r}".format(type(exc).__name__, raw[:60])
            return "{:3} enc={:8} {}".format(resp.status, enc, note)
    except urllib.error.HTTPError as exc:
        return "{:3} (차단)".format(exc.code)
    except Exception as exc:
        return "ERR {}: {}".format(type(exc).__name__, exc)


def main():
    print("KST", datetime.now(KST).isoformat(timespec="seconds"))
    print("\n1) searchRegnList 로 헤더 하나씩 빼본다")
    for name, hdr in SETS:
        print("  {:28} -> {}".format(name, hit("searchRegnList", hdr)))
        time.sleep(1.2)

    print("\n2) 안전한 조합(D1)이 무거운 엔드포인트에도 통하나")
    for path, params in (
            ("searchSscnsSchdExistList", {"siteNo": "0345"}),
            ("searchSiteScnscYmdListBySite", {"siteNo": "0345"}),
            ("searchMovScnInfo", {"rtctlScopCd": "01", "siteNo": "0345",
                                  "scnYmd": TOMORROW}),
            ("searchAtktTopPostrList", {})):
        print("  {:30} -> {}".format(path, hit(path, D1, params)))
        time.sleep(1.2)


if __name__ == "__main__":
    main()
