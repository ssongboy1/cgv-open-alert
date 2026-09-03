#!/usr/bin/env python3
"""메가박스를 읽을 수 있는지 확인하는 임시 스크립트. 확인 끝나면 지운다.

세 가지만 본다.
  1. 쿠키 없이도 응답이 오는가        (참고 코드는 브라우저 쿠키를 박아뒀다)
  2. 응답에 회차·좌석이 있는가        (없으면 '날짜 열림'까지만 알릴 수 있다)
  3. 특별관 필터를 빼면 전체가 오는가  (CGV처럼 지점 전체를 볼 수 있는가)

엔드포인트와 파라미터 형식은 공개 저장소 0w0i0n0g0/megabox-open-push 에서
확인한 것이다. 구현은 우리 방식(표준 라이브러리만)으로 새로 썼다.
요청은 총 3번만 보내고 사이에 텀을 둔다.
"""

from __future__ import annotations

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

COEX = "1351"          # 코엑스 (참고 저장소에서 확인)


def payload(brch_no, ymd, special=None):
    """special 이 None 이면 특별관 필터 없이 전체를 요청한다."""
    kind = special or ""
    return {
        "arrMovieNo": "", "playDe": ymd, "brchNoListCnt": 1,
        "brchNo1": brch_no, "brchNo2": "", "brchNo3": "",
        "areaCd1": kind, "areaCd2": "", "areaCd3": "",
        "spclbYn1": "Y" if special else "", "spclbYn2": "", "spclbYn3": "",
        "theabKindCd1": kind, "theabKindCd2": "", "theabKindCd3": "",
        "brchAll": "", "brchSpcl": kind,
        "movieNo1": "", "movieNo2": "", "movieNo3": "", "sellChnlCd": "",
    }


def describe(data, indent="  "):
    """응답 구조만 보여준다. 통째로 찍으면 읽을 수가 없다."""
    if not isinstance(data, dict):
        print(indent + "딕셔너리가 아님: " + type(data).__name__)
        return
    for key, val in data.items():
        if isinstance(val, list):
            print("{}{}: 리스트 {}개".format(indent, key, len(val)))
            if val and isinstance(val[0], dict):
                print("{}    필드: {}".format(indent, ", ".join(sorted(val[0]))))
                print("{}    첫 항목: {}".format(
                    indent, json.dumps(val[0], ensure_ascii=False)[:500]))
        elif isinstance(val, dict):
            print("{}{}: 딕셔너리, 키 {}개".format(indent, key, len(val)))
        else:
            print("{}{}: {}".format(indent, key, str(val)[:100]))


def ask(label, body):
    print("\n" + "=" * 64)
    print("[{}]".format(label))
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode("utf-8"),
        headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        print("HTTP {} / {} bytes  <- 쿠키 없이 성공".format(resp.status, len(raw)))
    except urllib.error.HTTPError as exc:
        print("HTTP 오류 {} {}".format(exc.code, exc.reason))
        print("  본문 앞부분:", exc.read()[:300].decode("utf-8", "replace"))
        return
    except Exception as exc:
        print("실패:", repr(exc))
        return

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        print("JSON 이 아님. 앞부분:", raw[:300].decode("utf-8", "replace"))
        return
    describe(data)


def main():
    today = datetime.now(KST).strftime("%Y%m%d")
    later = (datetime.now(KST) + timedelta(days=7)).strftime("%Y%m%d")
    print("오늘(KST):", today)

    ask("1. 코엑스 돌비관, 오늘 - 참고 저장소와 같은 요청", payload(COEX, today, "DBC"))
    time.sleep(2)
    ask("2. 코엑스 전 상영관, 오늘 - 필터를 빼면 전체가 오는가", payload(COEX, today))
    time.sleep(2)
    ask("3. 코엑스 전 상영관, 7일 뒤({}) - 미래 날짜도 되는가".format(later),
        payload(COEX, later))


if __name__ == "__main__":
    main()
