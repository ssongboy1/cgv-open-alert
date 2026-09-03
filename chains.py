"""극장사(CGV / 메가박스)에 따라 달라지는 부분을 여기서만 다룬다.

지점 값의 접두어로 어느 극장사인지 가른다.

    "0345"    -> CGV 대구         (지금까지 쓰던 형식 그대로)
    "MB1351"  -> 메가박스 코엑스

CGV 지점에 접두어를 붙이지 않는 것이 중요하다. state.json 의 회차 키가
지점 값으로 시작하는데(`지점|영화|날짜|시간|상영관`), 형식을 바꾸면 쌓아둔
기록이 전부 무효가 되어 이미 알린 회차를 다시 알리게 된다. prune_seen 도
'세 번째 조각이 날짜'라는 위치에 기대고 있어서 칸을 늘리면 같이 깨진다.
그래서 칸을 늘리는 대신 메가박스 쪽에만 접두어를 붙였다.
"""

from __future__ import annotations

import urllib.parse

import cgv_api
import megabox_api

MEGABOX_PREFIX = "MB"

# 어느 극장사든 '조회가 막혔다'는 이 둘 중 하나로 올라온다.
BLOCKED_ERRORS = (cgv_api.CloudflareBlocked, megabox_api.Blocked)


def is_megabox(site):
    return str(site).upper().startswith(MEGABOX_PREFIX)


def code(site):
    """극장사 접두어를 뗀 실제 지점번호."""
    return str(site)[len(MEGABOX_PREFIX):] if is_megabox(site) else str(site)


def label(site):
    """알림 메시지에 붙는 극장사 이름."""
    return "메가박스" if is_megabox(site) else "CGV"


def gate(site):
    """지점당 최소 요청으로 '변화 있었나'를 볼 값.

    값이 달라졌을 때만 상세 시간표를 조회한다. 극장사마다 모양이 다르지만
    watcher 는 같은지 다른지만 보므로 상관없다.
    """
    if is_megabox(site):
        return megabox_api.get_gate(code(site))
    screens = cgv_api.get_special_screens(site)
    imax = next((s for s in screens
                 if s.get("comCdvalNm") == cgv_api.IMAX_GRADE), None)
    return {
        "imax_cnt": (imax or {}).get("schdCnt", "0"),
        "dates": cgv_api.get_open_dates(site),
    }


def schedules(site, ymd):
    """그 지점 그 날짜의 회차 목록. 두 극장사 모두 CGV 필드명으로 돌려준다."""
    if is_megabox(site):
        return megabox_api.get_schedules(code(site), ymd)
    return cgv_api.get_schedules(site, ymd)


def jitter(site):
    if is_megabox(site):
        megabox_api._jitter()
    else:
        cgv_api._jitter()


CGV_BOOK_URL = "https://cgv.co.kr/cnm/movieBook"


def booking_url(site, site_nm):
    """예매 화면 웹링크. CGV 주소는 지금까지 쓰던 것과 같아야 한다."""
    if is_megabox(site):
        return megabox_api.booking_url(code(site))
    return "{}/cinema?siteNo={}&siteNm={}".format(
        CGV_BOOK_URL, site, urllib.parse.quote(site_nm))


# 사용자가 적은 상영관 이름을 각 극장사의 표기로 맞춘다.
# 앞쪽은 CGV 로 쓰던 것 그대로. 뒤에 메가박스 특별관을 더했다.
SCREEN_ALIASES = {
    "IMAX": "아이맥스", "imax": "아이맥스", "Imax": "아이맥스",
    "아이맥스": "아이맥스",
    "4DX": "4DX", "4dx": "4DX",
    "SCREENX": "SCREENX", "screenx": "SCREENX", "ScreenX": "SCREENX",
    "일반": "일반", "일반관": "일반",

    # 메가박스. megabox_api.SCREEN_KINDS 가 회차에 넣는 이름과 맞아야 한다.
    "돌비": "돌비시네마", "돌비시네마": "돌비시네마",
    "DOLBY": "돌비시네마", "dolby": "돌비시네마", "DBC": "돌비시네마",
    "돌비비전": "돌비비전", "DVA": "돌비비전",
    "돌비애트모스": "돌비애트모스", "애트모스": "돌비애트모스",
    "MX": "돌비애트모스",
    "MX4D": "MX4D", "mx4d": "MX4D",
    "LED": "LED", "led": "LED", "LUMINEON": "LED",
    "리클라이너": "리클라이너", "RCL": "리클라이너",
    "부티크": "부티크", "TBQ": "부티크",
    "컴포트": "컴포트", "CFT": "컴포트",
}
