"""CGV 신규 사이트(BFF) 조회 클라이언트.

CGV는 2025년 사이트를 Next.js로 재구축하면서 구 예매 시스템
ticket.cgv.co.kr 을 내렸다. 현재 프런트는 api.cgv.co.kr 을 직접 부르지 않고
BFF(https://cgv.co.kr/api/v1/*)를 경유한다. 이 BFF는 토큰/쿠키가 필요 없고
coCd=A420 만 있으면 누구나 GET 으로 조회할 수 있다.

--------------------------------------------------------------------------
중요: cgv.co.kr 앞단 Cloudflare 를 통과하려면 두 가지가 필요하다.

1) User-Agent 를 브라우저 문자열로 (전 엔드포인트 공통)

    curl/8.4.0              -> 403
    python-requests/2.32.0  -> 403
    Mozilla/5.0 ... Chrome  -> 200
    Mozilla/5.0 ... (구형)   -> 200

2) Referer 를 cgv.co.kr 페이지로 (searchMovScnInfo 에 필수)

   실측: 같은 UA 로 호출해도
       searchSscnsSchdExistList      UA만 200
       searchSiteScnscYmdListBySite  UA만 200
       searchRegnList                UA만 200
       searchMovScnInfo              UA만 403 / UA+Referer 200

   가장 무거운 시간표 엔드포인트만 Referer 를 요구한다. 하필 우리가
   제일 많이 쓰는 엔드포인트다. Accept, Accept-Language 는 영향 없다.

아래 헤더 중 User-Agent 나 Referer 를 빼면 감시가 통째로 멈춘다.
selftest.py 가 이 둘을 회귀 테스트로 잡고 있다.
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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TIMEOUT = 10
RETRIES = 3


class CgvError(RuntimeError):
    """CGV 조회 실패."""


class CloudflareBlocked(CgvError):
    """403. UA 규칙에 걸렸거나 Cloudflare 정책이 강화됐다."""


def _get(path, **params):
    params.setdefault("coCd", CO_CD)
    url = BASE + "/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": "https://cgv.co.kr/cnm/movieBook",
        },
    )

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
                    "Cloudflare가 User-Agent를 봇으로 판정했습니다. "
                    "cgv_api.UA 를 확인하세요."
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
