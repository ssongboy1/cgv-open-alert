#!/usr/bin/env python3
"""403 원인 좁히기용 임시 스크립트. 확인 후 지운다.

무엇이 막히는지(엔드포인트) / 무엇 때문에 막히는지(헤더인지 IP인지)를
가른다. 요청은 다 합쳐 20개 미만이고 사이에 지연을 둔다.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://cgv.co.kr/api/v1"
KST = timezone(timedelta(hours=9))
TOMORROW = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")

UA131 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA140 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
REFERER = "https://cgv.co.kr/cnm/movieBook"

# A = 지금 코드가 보내는 것 그대로
A = {"User-Agent": UA131, "Accept": "application/json",
     "Accept-Language": "ko-KR,ko;q=0.9", "Referer": REFERER}
# B = UA 만
B = {"User-Agent": UA131}
# C = A 에서 UA 만 최신으로
C = dict(A, **{"User-Agent": UA140})
# D = 실제 크롬이 보내는 것에 최대한 가깝게
D = {
    "User-Agent": UA140,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": REFERER,
    "Origin": "https://cgv.co.kr",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}
# E = 대조군. 봇 UA. 여기서 200 이 나오면 UA 규칙 자체가 사라진 것이다.
E = {"User-Agent": "python-urllib/3.12"}

SETS = [("A 현재코드 그대로", A), ("B UA만(131)", B), ("C UA만 최신(140)", C),
        ("D 크롬 흉내 최대", D), ("E 봇UA(대조군)", E)]

ENDPOINTS = [
    ("searchRegnList", {}),
    ("searchSscnsSchdExistList", {"siteNo": "0345"}),
    ("searchSiteScnscYmdListBySite", {"siteNo": "0345"}),
    ("searchMovScnInfo", {"rtctlScopCd": "01", "siteNo": "0345",
                          "scnYmd": TOMORROW}),
]


def hit(path, headers, params):
    p = dict(params)
    p.setdefault("coCd", "A420")
    url = BASE + "/booking/" + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            try:
                body = json.loads(raw.decode("utf-8"))
                note = "statusCode={} 데이터 {}건".format(
                    body.get("statusCode"),
                    len(body.get("data") or []) if isinstance(body.get("data"), list) else "?")
            except Exception:
                note = repr(raw[:80])
            return resp.status, note, {}
    except urllib.error.HTTPError as exc:
        interesting = {k: v for k, v in exc.headers.items()
                       if k.lower() in ("server", "cf-ray", "cf-mitigated",
                                        "cf-cache-status", "retry-after")}
        body = b""
        try:
            body = exc.read()[:200]
        except Exception:
            pass
        return exc.code, repr(body), interesting
    except Exception as exc:
        return "ERR", "{}: {}".format(type(exc).__name__, exc), {}


def main():
    print("오늘(KST):", datetime.now(KST).isoformat(timespec="seconds"))

    print("\n" + "=" * 70)
    print("1) 같은 엔드포인트(searchRegnList)에 헤더만 바꿔본다")
    print("=" * 70)
    for name, hdr in SETS:
        code, note, extra = hit("searchRegnList", hdr, {})
        print("  {:22} -> {}  {}".format(name, code, note))
        if extra:
            print("     {}".format(extra))
        time.sleep(1.2)

    print("\n" + "=" * 70)
    print("2) 헤더는 '현재 코드 그대로'로 두고 엔드포인트만 바꿔본다")
    print("=" * 70)
    for path, params in ENDPOINTS:
        code, note, extra = hit(path, A, params)
        print("  {:32} -> {}  {}".format(path, code, note))
        if extra:
            print("     {}".format(extra))
        time.sleep(1.2)

    print("\n" + "=" * 70)
    print("3) 1) 에서 통한 조합이 있으면 무거운 엔드포인트에도 통하나")
    print("=" * 70)
    for name, hdr in (("D 크롬 흉내 최대", D), ("C UA만 최신(140)", C)):
        for path, params in ENDPOINTS[1:]:
            code, note, _ = hit(path, hdr, params)
            print("  {:22} {:32} -> {}  {}".format(name, path, code, note))
            time.sleep(1.2)

    print("\n" + "=" * 70)
    print("4) 메가박스는 지금 되나 (같은 IP에서)")
    print("=" * 70)
    try:
        import megabox_api
        gate = megabox_api.get_gate("1351")
        print("  메가박스 OK. 열린 날짜 {}개, 내일 회차 {}".format(
            len(gate["dates"]), gate["shows"]))
    except Exception as exc:
        print("  메가박스 실패: {}: {}".format(type(exc).__name__, exc))

    print("\n" + "=" * 70)
    print("5) cgv.co.kr 일반 페이지는 열리나 (API 만 막힌 건지)")
    print("=" * 70)
    for url in ("https://cgv.co.kr/", "https://cgv.co.kr/cnm/movieBook"):
        req = urllib.request.Request(url, headers={"User-Agent": UA140})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                print("  {} -> {} ({} bytes)".format(url, resp.status,
                                                     len(resp.read(4000))))
        except urllib.error.HTTPError as exc:
            print("  {} -> {}".format(url, exc.code))
        except Exception as exc:
            print("  {} -> ERR {}".format(url, exc))
        time.sleep(1.2)


if __name__ == "__main__":
    main()
