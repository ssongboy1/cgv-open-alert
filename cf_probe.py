#!/usr/bin/env python3
"""시간표 엔드포인트만 막혔다. CGV 프런트가 지금 무슨 주소를 쓰는지 본다.

가설: searchMovScnInfo 가 구버전이 되어 Cloudflare 가 아예 막아둔 것.
실제 예매 페이지의 JS 를 긁어 booking/ 으로 시작하는 주소를 전부 뽑는다.
"""
from __future__ import annotations

import gzip
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import cgv_api

H = dict(cgv_api.BROWSER_HINTS)
H.update({
    "User-Agent": cgv_api.UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Upgrade-Insecure-Requests": "1",
})
JS_H = dict(H)
JS_H.update({"Accept": "*/*", "Sec-Fetch-Dest": "script",
             "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Site": "same-origin",
             "Referer": "https://cgv.co.kr/cnm/movieBook"})


def get(url, headers):
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=15) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return "ERR", str(exc)[:80]


PAGE = "https://cgv.co.kr/cnm/movieBook"
code, html = get(PAGE, H)
print("예매 페이지 {} -> {} ({} 글자)".format(PAGE, code, len(html)))
if not html:
    raise SystemExit("페이지를 못 받았다")

srcs = re.findall(r'src="(/_next/static/[^"]+\.js)"', html)
srcs += re.findall(r'"(/_next/static/chunks/[^"]+\.js)"', html)
srcs = list(dict.fromkeys(srcs))
print("JS 파일 {}개 발견".format(len(srcs)))

inline = sorted(set(re.findall(r'booking/[A-Za-z0-9_]+', html)))
if inline:
    print("HTML 안에서 바로 보이는 주소: {}".format(inline))

found = {}
for i, src in enumerate(srcs[:30]):
    st, body = get("https://cgv.co.kr" + src, JS_H)
    if st != 200:
        continue
    for m in set(re.findall(r'(?:api/v\d/)?(booking/[A-Za-z0-9_]+)', body)):
        found.setdefault(m, []).append(src.rsplit("/", 1)[-1])
    time.sleep(0.3)

print("\n--- 프런트가 쓰는 booking 주소 ---")
if not found:
    print("  하나도 못 찾음 (주소가 런타임에 조립되는 듯)")
for path in sorted(found):
    mark = "  <= 우리가 쓰는 것" if "searchMovScnInfo" in path else ""
    print("  {:44} ({}개 파일){}".format(path, len(found[path]), mark))

print("\n--- 우리가 쓰는 4개가 프런트에 남아있나 ---")
for p in ("booking/searchRegnList", "booking/searchSscnsSchdExistList",
          "booking/searchSiteScnscYmdListBySite", "booking/searchMovScnInfo",
          "booking/searchAtktTopPostrList"):
    print("  {:40} {}".format(p, "있음" if p in found else "없음"))
