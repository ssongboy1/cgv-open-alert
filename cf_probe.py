#!/usr/bin/env python3
"""searchMovScnInfo 만 403 이다. 왜인지 좁힌다. 확인 후 지운다.

가능성 세 갈래를 가른다.
  (a) 간헐적/속도제한   -> 같은 요청을 여러 번 해보면 섞여 나온다
  (b) Referer 가 부족   -> 지점 페이지 주소로 바꿔본다
  (c) 쿠키가 필요        -> HTML 페이지를 먼저 열어 쿠키를 받고 다시 부른다
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import cgv_api

KST = timezone(timedelta(hours=9))
TOMORROW = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")
SITE = "0345"
HEAVY = "searchMovScnInfo"
HEAVY_P = {"rtctlScopCd": "01", "siteNo": SITE, "scnYmd": TOMORROW}
LIGHT = "searchSscnsSchdExistList"
LIGHT_P = {"siteNo": SITE}

BASEH = dict(cgv_api.BROWSER_HINTS)
BASEH.update({
    "User-Agent": cgv_api.UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": "https://cgv.co.kr",
})


def hdr(referer):
    h = dict(BASEH)
    h["Referer"] = referer
    return h


def hit(path, params, headers, opener=None):
    p = dict(params)
    p.setdefault("coCd", "A420")
    url = "https://cgv.co.kr/api/v1/booking/" + path + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=headers)
    try:
        op = opener or urllib.request.build_opener()
        with op.open(req, timeout=12) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = json.loads(raw.decode("utf-8"))
            d = body.get("data")
            return "200  {}건".format(len(d) if isinstance(d, list) else "?")
    except urllib.error.HTTPError as exc:
        return "{}".format(exc.code)
    except Exception as exc:
        return "ERR {}".format(type(exc).__name__)


MB = "https://cgv.co.kr/cnm/movieBook"
CINEMA = MB + "/cinema?siteNo={}&siteNm={}".format(SITE, urllib.parse.quote("대구"))

print("KST", datetime.now(KST).isoformat(timespec="seconds"))

print("\n1) 지금 헤더 그대로, 같은 요청을 5번 (간헐적인지 본다)")
for i in range(5):
    print("   시도 {}: 시간표 {}   가벼운쪽 {}".format(
        i + 1, hit(HEAVY, HEAVY_P, hdr(MB)), hit(LIGHT, LIGHT_P, hdr(MB))))
    time.sleep(1.5)

print("\n2) Referer 를 바꿔본다 (시간표만)")
for name, ref in (("movieBook (지금)", MB),
                  ("movieBook/cinema?siteNo=", CINEMA),
                  ("루트 cgv.co.kr/", "https://cgv.co.kr/")):
    print("   {:28} -> {}".format(name, hit(HEAVY, HEAVY_P, hdr(ref))))
    time.sleep(1.5)

print("\n3) 날짜를 바꿔본다 (그 날짜만 막힌 건지)")
for off in (0, 1, 2, 7):
    ymd = (datetime.now(KST) + timedelta(days=off)).strftime("%Y%m%d")
    print("   +{}일 {} -> {}".format(
        off, ymd, hit(HEAVY, dict(HEAVY_P, scnYmd=ymd), hdr(MB))))
    time.sleep(1.5)

print("\n4) 지점을 바꿔본다 (그 지점만 막힌 건지)")
for s in ("0345", "0013", "0089"):
    print("   siteNo {} -> {}".format(
        s, hit(HEAVY, dict(HEAVY_P, siteNo=s), hdr(MB))))
    time.sleep(1.5)

print("\n5) HTML 페이지로 쿠키를 먼저 받고 다시 부른다")
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
page_h = dict(BASEH)
page_h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
               "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
               "Upgrade-Insecure-Requests": "1"})
page_h.pop("Origin", None)
page_h.pop("Referer", None)
for url in (MB,):
    try:
        with op.open(urllib.request.Request(url, headers=page_h), timeout=12) as r:
            print("   페이지 {} -> {} ({} bytes)".format(url, r.status, len(r.read(2000))))
    except urllib.error.HTTPError as exc:
        print("   페이지 {} -> {}".format(url, exc.code))
    except Exception as exc:
        print("   페이지 {} -> ERR {}".format(url, exc))
print("   받은 쿠키: {}".format([c.name for c in jar] or "없음"))
time.sleep(1.5)
print("   쿠키 들고 시간표 -> {}".format(hit(HEAVY, HEAVY_P, hdr(MB), op)))
