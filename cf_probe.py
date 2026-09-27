#!/usr/bin/env python3
"""3차: A(지금 코드)에 헤더를 하나씩 '더해' 본다.

2차에서 D 계열 8종이 전부 200 이었다. 즉 sec-ch-ua / Sec-Fetch / Origin /
Connection / UA버전 / Accept-Encoding 중 어느 것도 단독 열쇠가 아니다.
그러면 남은 차이는 Accept 와 Accept-Language 값이다.

2차에는 A(403 나던 조합)를 다시 안 넣었으므로, 차단이 아직 살아 있는지도
확인하지 못했다. 이번에는 A 를 대조군으로 같은 실행에 넣는다.
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

UA131 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REFERER = "https://cgv.co.kr/cnm/movieBook"

# A = 지금 코드가 보내는 것 그대로. 1차에서 전 엔드포인트 403.
A = {"User-Agent": UA131, "Accept": "application/json",
     "Accept-Language": "ko-KR,ko;q=0.9", "Referer": REFERER}

VARIANTS = [
    ("A 지금 코드 (대조군)", A),
    ("A + Accept 를 axios 기본값", dict(A, **{
        "Accept": "application/json, text/plain, */*"})),
    ("A + Accept 를 */*", dict(A, **{"Accept": "*/*"})),
    ("A + Accept 아예 없음", {k: v for k, v in A.items() if k != "Accept"}),
    ("A + Accept-Language 길게", dict(A, **{
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"})),
    ("A + Origin", dict(A, **{"Origin": "https://cgv.co.kr"})),
    ("A + Connection", dict(A, **{"Connection": "keep-alive"})),
    ("A + Accept-Encoding", dict(A, **{"Accept-Encoding": "gzip, deflate"})),
    ("A + sec-ch-ua 3종", dict(A, **{
        "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"'})),
    ("A + Sec-Fetch 3종", dict(A, **{
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"})),
    ("A 다시 (대조군 2)", A),
]


def hit(path, headers, params=None):
    p = dict(params or {})
    p.setdefault("coCd", "A420")
    url = BASE + "/" + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            enc = resp.headers.get("Content-Encoding") or "없음"
            raw = resp.read()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            body = json.loads(raw.decode("utf-8"))
            n = body.get("data")
            return "200 enc={:6} 데이터 {}".format(
                enc, len(n) if isinstance(n, list) else "?")
    except urllib.error.HTTPError as exc:
        return "{} (차단)".format(exc.code)
    except Exception as exc:
        return "ERR {}: {}".format(type(exc).__name__, exc)


def main():
    print("KST", datetime.now(KST).isoformat(timespec="seconds"))
    print("\nsearchRegnList 에 A 부터 한 칸씩 더해본다")
    for name, hdr in VARIANTS:
        print("  {:26} -> {}".format(name, hit("searchRegnList", hdr)))
        time.sleep(1.5)

    # urllib 은 Request 에 Accept-Encoding 을 안 넣으면 아무것도 안 붙인다.
    # 헤더 이름 대소문자도 확인해 둔다 (urllib 이 제목형으로 정규화한다).
    req = urllib.request.Request(BASE + "/searchRegnList?coCd=A420", headers=A)
    print("\nurllib 이 실제로 보내는 헤더 이름:", sorted(req.headers.keys()))


if __name__ == "__main__":
    main()
