"""메가박스 조회 클라이언트.

예매 페이지가 쓰는 엔드포인트 하나면 다 된다. 토큰도 쿠키도 없이 POST 하면
그 지점·그 날짜의 전체 시간표와, 예매 가능한 날짜 목록과, 전국 지점 목록이
한 번에 온다. (2026-09 실측)

--------------------------------------------------------------------------
중요: 돌려주는 회차 dict 는 일부러 CGV 응답과 같은 필드 이름을 쓴다.

watcher 는 이미 CGV 필드 이름으로 회차를 다루고 있다(15군데). 극장사를
늘리자고 그걸 전부 뜯으면 잘 돌고 있는 CGV 알림이 흔들린다. 그래서
watcher 를 고치는 대신 여기서 CGV 형태로 맞춰 내보낸다. 대응표는
to_row() 주석에 있다.
--------------------------------------------------------------------------

표준 라이브러리만 사용한다 (pip install 불필요).
"""

from __future__ import annotations

import html
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"
BOOK_URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/simpleBookingPage.do"

# 이 엔드포인트는 브라우저에서 부르는 것처럼 보여야 응답한다.
# Origin/Referer/X-Requested-With 중 하나라도 빠지면 막힐 수 있다.
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": "https://www.megabox.co.kr",
    "Referer": BOOK_URL,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

TIMEOUT = 15
RETRIES = 3

# theabKindCd -> 사람이 쓰는 이름. 감시 대상의 'screen' 값과 맞춰야 한다.
SCREEN_KINDS = {
    "NOR": "일반",
    "DBC": "돌비시네마",
    "DVA": "돌비비전",
    "MX": "돌비애트모스",
    "MX4D": "MX4D",
    "LUMINEON": "LED",
    "RCL": "리클라이너",
    "TBQ": "부티크",
    "TBS": "부티크",
    "CFT": "컴포트",
}


class MegaboxError(RuntimeError):
    """메가박스 조회 실패."""


class Blocked(MegaboxError):
    """403 등으로 막혔다. CGV 의 CloudflareBlocked 와 같은 취급을 한다."""


def _jitter():
    """서버를 배려한 요청 간 지연."""
    time.sleep(random.uniform(0.3, 0.8))


def _post(brch_no, ymd):
    """지점 + 날짜로 조회한다. 특별관 필터는 걸지 않는다(전 상영관)."""
    body = {
        "arrMovieNo": "", "playDe": ymd, "brchNoListCnt": 1,
        "brchNo1": brch_no, "brchNo2": "", "brchNo3": "",
        "areaCd1": "", "areaCd2": "", "areaCd3": "",
        "spclbYn1": "", "spclbYn2": "", "spclbYn3": "",
        "theabKindCd1": "", "theabKindCd2": "", "theabKindCd3": "",
        "brchAll": "", "brchSpcl": "",
        "movieNo1": "", "movieNo2": "", "movieNo3": "", "sellChnlCd": "",
    }
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode("utf-8"),
        headers=HEADERS, method="POST")

    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise Blocked(
                    "403 Forbidden: " + URL + "\n"
                    "메가박스가 요청을 봇으로 판정했습니다. "
                    "megabox_api.HEADERS 를 확인하세요."
                ) from exc
            last = exc
        except Exception as exc:            # 네트워크 오류 등
            last = exc
        else:
            try:
                # 응답 앞에 BOM 이 붙어 온다.
                return json.loads(raw.decode("utf-8-sig"))
            except ValueError as exc:
                raise MegaboxError("JSON 이 아닌 응답: " + repr(raw[:200])) from exc

        if attempt < RETRIES - 1:
            time.sleep(1.5 ** attempt)

    raise MegaboxError("메가박스 요청 실패: " + repr(last))


def _text(value):
    """이름에 &#40; 같은 HTML 이스케이프가 섞여 온다."""
    return html.unescape(value or "")


def to_row(item):
    """메가박스 회차 하나를 watcher 가 쓰는 형태(=CGV 필드명)로 바꾼다.

        메가박스                        내부
        movieNo                         movNo
        movieNm                         movNm
        playDe                          scnYmd
        playStartTime "15:50"           scnsrtTm "1550"
        theabNo                         scnsNo
        theabKindCd DBC/NOR/..          tcscnsGradNm 돌비시네마/일반/..
        theabExpoNm                     movkndDsplNm
        restSeatCnt                     frSeatCnt
        totSeatCnt                      stcnt

    시작시간은 콜론을 떼어 CGV 와 같은 4자리로 만든다. 그래야 fmt_time 이
    그대로 동작한다. 메가박스는 CGV 처럼 24시를 넘겨 쓰지 않는다(실측 최대
    23:40). 자정 이후 회차는 그 다음 날짜로 들어오는 것으로 보인다.
    """
    kind = item.get("theabKindCd")
    return {
        "movNo": str(item.get("movieNo") or ""),
        "movNm": _text(item.get("movieNm")),
        "scnYmd": str(item.get("playDe") or ""),
        "scnsrtTm": str(item.get("playStartTime") or "").replace(":", ""),
        "scnsNo": str(item.get("theabNo") or ""),
        "tcscnsGradNm": SCREEN_KINDS.get(kind, kind or "일반"),
        "movkndDsplNm": _text(item.get("theabExpoNm")),
        "frSeatCnt": item.get("restSeatCnt"),
        "stcnt": item.get("totSeatCnt"),
    }


# ---------------------------------------------------------------- 조회 API

def get_schedules(site_no, scn_ymd):
    """지점 + 날짜의 전체 시간표. CGV 형태의 회차 목록을 돌려준다."""
    data = _post(site_no, scn_ymd)
    return [to_row(r) for r in (data.get("movieFormList") or [])]


def get_gate(site_no):
    """지점당 1요청으로 게이트 값을 만든다.

    CGV 는 특별관 편수와 날짜 목록에 2요청이 드는데, 메가박스는 한 응답에
    둘 다 들어 있어서 1요청이면 된다.

    오늘이 아니라 '내일'로 물어본다. 응답의 회차 목록은 이미 지나간 회차가
    빠져서 오기 때문이다(실측: 15:02 조회 -> 15:25 부터, 15:58 조회 ->
    16:10 부터). 오늘로 물어보면 상영이 끝날 때마다 값이 줄어 게이트가
    하루 종일 바뀌고, 그때마다 열린 날짜를 전부 다시 훑어 요청만 낭비한다.
    내일치는 자정 전까지 시간 때문에 줄어들 일이 없다.

    날짜 목록은 어느 날짜로 물어보든 같이 오므로 요청 수는 그대로 1이다.
    """
    tomorrow = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")
    data = _post(site_no, tomorrow)
    dates = [d["playDe"] for d in (data.get("movieFormDeList") or [])
             if d.get("formAt") == "Y" and d.get("playDe")]
    # 이미 열린 날짜에 회차·영화가 늘어나는 것도 잡으려고 회차 수를 함께 본다.
    return {"dates": dates, "shows": str(len(data.get("movieFormList") or []))}


def get_branches():
    """전국 지점 목록. [{'area': '서울', 'name': '강남', 'no': '1372'}, ...]

    어느 지점으로 물어보든 전국 목록이 딸려 온다.
    """
    today = datetime.now(KST).strftime("%Y%m%d")
    data = _post("1351", today)          # 아무 지점이나. 코엑스로 물어본다.
    out = []
    for b in (data.get("areaBrchList") or []):
        if not b.get("brchNo"):
            continue
        out.append({"area": _text(b.get("areaCdNm")),
                    "name": _text(b.get("brchNm")),
                    "no": b["brchNo"]})
    return out


def booking_url(site_no):
    """지점 예매 화면. 지점이 미리 선택되는지는 확인하지 못했다."""
    return "{}?brchNo1={}".format(BOOK_URL, site_no)


def get_screen_kinds(site_no):
    """그 지점에 오늘 걸려 있는 상영관 종류. [('DBC', '돌비시네마'), ...]

    /add 에서 그 지점에 실제로 있는 상영관만 보여주는 데 쓴다.
    오늘 상영이 없는 특별관은 안 잡히지만, 목록을 얻자고 요청을 여러 번
    보내는 것보다 낫다.
    """
    today = datetime.now(KST).strftime("%Y%m%d")
    kinds = {}
    for item in (_post(site_no, today).get("movieFormList") or []):
        cd = item.get("theabKindCd")
        if cd:
            kinds[cd] = SCREEN_KINDS.get(cd, cd)
    return sorted(kinds.items())
