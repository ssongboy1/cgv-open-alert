"""CGV 신규 사이트(BFF) 조회 클라이언트.

CGV는 2025년 사이트를 Next.js로 재구축하면서 구 예매 시스템
ticket.cgv.co.kr 을 내렸다. 현재 프런트는 api.cgv.co.kr 을 직접 부르지 않고
BFF(https://cgv.co.kr/api/v1/*)를 경유한다. 이 BFF는 토큰/쿠키가 필요 없고
coCd=A420 만 있으면 누구나 GET 으로 조회할 수 있다.

--------------------------------------------------------------------------
중요: cgv.co.kr 앞단 Cloudflare 를 통과하려면 세 가지가 필요하다.

1) User-Agent 를 브라우저 문자열로 (전 엔드포인트 공통)

    curl/8.4.0              -> 403
    python-requests/2.32.0  -> 403
    Mozilla/5.0 ... Chrome  -> 200

2) Referer 를 cgv.co.kr 페이지로 (searchMovScnInfo 에 필수)

3) 요즘 브라우저만 보내는 헤더 묶음 (2026-09 부터 필수가 됐다)

   sec-ch-ua 3종 또는 Sec-Fetch 3종. 둘 중 한 묶음만 있어도 통과한다.

   2026-09-27 실측. 같은 IP, 같은 UA, 같은 Referer 로 searchRegnList:
       (아무것도 없음)            403      <- 2026-08 까지는 200 이었다
       + Accept 를 바꿈           403
       + Accept-Language 를 바꿈  403
       + Origin                  403
       + Connection              403
       + Accept-Encoding         403
       + sec-ch-ua 3종           200      <- 열쇠
       + Sec-Fetch 3종           200      <- 열쇠

   같은 실행에서 전후로 대조군을 두 번 넣어 차단이 살아 있음을 확인했다.
   엔드포인트를 가리지 않는다. 전에는 searchMovScnInfo 만 Referer 를
   요구하고 나머지는 UA 만으로 200 이었는데, 이제는 넷 다 막힌다.

   두 묶음을 다 보낸다. 실제 크롬도 둘 다 보내고, 한쪽 규칙이 바뀌어도
   다른 쪽으로 버틴다. sec-ch-ua 의 버전은 UA 의 크롬 버전과 맞춰 둔다.
   어긋나 있으면 그 자체가 봇 신호가 된다.

Accept-Encoding 은 일부러 안 보낸다. 요즘 크롬은 br / zstd 를 같이
요구하는데 표준 라이브러리로는 못 푼다. 안 보내면 서버가 압축 없이
주고, 그래도 통과한다(위 실측에서 enc=없음으로 200).

아래 헤더 중 하나라도 빼면 감시가 통째로 멈춘다.
selftest.py 가 회귀 테스트로 잡고 있다.
--------------------------------------------------------------------------

표준 라이브러리만 사용한다 (pip install 불필요).
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://cgv.co.kr/api/v1"
CO_CD = "A420"           # CGV 회사코드
RTCTL_SCOP_CD = "01"     # 발매통제범위코드
IMAX_GRADE = "아이맥스"    # searchMovScnInfo 의 tcscnsGradNm 값

# Cloudflare 통과용. 위 주석 참고.
CHROME_VER = "140"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{}.0.0.0 Safari/537.36".format(CHROME_VER)
)

# 요즘 브라우저만 보내는 묶음. 둘 중 하나만 있어도 통과하지만 둘 다 보낸다.
BROWSER_HINTS = {
    "sec-ch-ua": '"Chromium";v="{v}", "Not=A?Brand";v="24", '
                 '"Google Chrome";v="{v}"'.format(v=CHROME_VER),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

TIMEOUT = 10
RETRIES = 3


class CgvError(RuntimeError):
    """CGV 조회 실패."""


class CloudflareBlocked(CgvError):
    """403. UA 규칙에 걸렸거나 Cloudflare 정책이 강화됐다."""


def _get(path, **params):
    params.setdefault("coCd", CO_CD)
    url = BASE + "/" + path + "?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://cgv.co.kr/cnm/movieBook",
        "Origin": "https://cgv.co.kr",
    }
    headers.update(BROWSER_HINTS)
    req = urllib.request.Request(url, headers=headers)

    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise CloudflareBlocked(
                    "403 Forbidden: " + url + "\n"
                    "Cloudflare가 이 요청을 봇으로 판정했습니다. "
                    "cgv_api 의 UA / Referer / BROWSER_HINTS 를 확인하세요. "
                    "(CGV가 통과 조건을 또 올렸을 수 있습니다)"
                ) from exc
            if exc.code == 429:
                # 속도 제한. 서버가 알려준 만큼 기다렸다가 다시 시도한다.
                wait = exc.headers.get("Retry-After")
                try:
                    wait = int(wait)
                except (TypeError, ValueError):
                    wait = 30 * (attempt + 1)
                time.sleep(min(wait, 120))
            last = exc
        except Exception as exc:  # 네트워크 오류 등
            last = exc
        else:
            status = body.get("statusCode")
            if status not in (0, "0"):
                raise CgvError(
                    path + ": " + str(body.get("statusMessage"))
                    + " / " + json.dumps(body.get("data"), ensure_ascii=False)
                )
            return body.get("data")

        if attempt < RETRIES - 1:
            time.sleep(1.5 ** attempt)

    raise CgvError(path + " 요청 실패: " + repr(last))


def _jitter():
    """CGV 서버를 배려한 요청 간 지연."""
    time.sleep(random.uniform(0.3, 0.8))


# ---------------------------------------------------------------- 조회 API

def get_regions():
    """지역별 지점 목록.

    [{regnGrpNm: '서울', siteList: [{siteNo: '0013', siteNm: '용산아이파크몰'}, ...]}, ...]
    """
    return _get("booking/searchRegnList") or []


def get_special_screens(site_no):
    """지점의 특별관 현황.

    [{comCdvalNm: '아이맥스', schdCnt: '1'}, ...]
    schdCnt 는 회차 수가 아니라 그 특별관에서 상영 중인 '영화 편수'다.
    """
    return _get("booking/searchSscnsSchdExistList", siteNo=site_no) or []


def get_open_dates(site_no):
    """예매가 열려 있는 날짜 목록. ['20260819', '20260820', ...]"""
    rows = _get("booking/searchSiteScnscYmdListBySite", siteNo=site_no) or []
    return [r["scnYmd"] for r in rows]


def get_schedules(site_no, scn_ymd):
    """지점 + 날짜의 전체 시간표."""
    return _get(
        "booking/searchMovScnInfo",
        rtctlScopCd=RTCTL_SCOP_CD,
        siteNo=site_no,
        scnYmd=scn_ymd,
    ) or []


def get_now_playing():
    """현재 상영작(예매율 순). GUI 영화 드롭다운용."""
    return _get("booking/searchAtktTopPostrList") or []


# ---------------------------------------------------------------- 헬퍼

def has_imax(site_no):
    """(아이맥스 보유 여부, 상영 편수) 반환. GUI에서 지점 선택 검증용."""
    for row in get_special_screens(site_no):
        if row.get("comCdvalNm") == IMAX_GRADE:
            return True, row.get("schdCnt", "0")
    return False, "0"


def imax_rows(site_no, scn_ymd):
    """해당 날짜의 아이맥스 회차만."""
    return [
        r for r in get_schedules(site_no, scn_ymd)
        if r.get("tcscnsGradNm") == IMAX_GRADE
    ]
