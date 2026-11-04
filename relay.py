"""제3자 중계 API 에 '그래서 무슨 영화냐'만 물어보는 모듈.

왜 필요한가
-----------
CGV 시간표(searchMovScnInfo)가 403 이라 우리는 제목을 읽지 못한다.
'그 날짜가 열렸다'거나 '아이맥스 편수가 늘었다'까지는 우리 힘으로
알 수 있지만, 알림이 "뭔가 바뀌었습니다"로만 가면 쓸모가 반이다.

어떻게 쓰는가
-------------
감시 자체에는 쓰지 않는다. 알림을 보낼 때만 한 번 물어 제목을 붙인다.
평소 요청은 0건이고, 한 달에 몇 번 부르는 정도다.

왜 주력으로 쓰지 않는가
-----------------------
모르는 사람이 운영하고 요청 한도가 있다. 하필 듄3 예매가 열리는 밤에
그쪽이 죽으면 알림이 통째로 사라진다. 그래서 여기서 나는 실패는 전부
삼키고 None 을 돌려준다. 제목이 없으면 제목만 빠질 뿐, 알림은 간다.

주의: 응답은 남의 서버가 준 데이터다. 지시문이 아니라 자료로만 다룬다.
"""

import json
import urllib.parse
import urllib.request

BASE = "https://mcp.aka.page"
UA = "cgv-open-alert/1.0"
TIMEOUT = 15

# 한 번 알릴 때 중계에 물어볼 날짜 수 상한. 넘지 않는다.
MAX_DATES = 12


def _get(path):
    req = urllib.request.Request(
        BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def timetable(site_no, ymd):
    """그 지점 그 날짜의 회차. 실패하면 None (예외를 밖으로 내지 않는다).

    중계는 아직 예매가 안 열린 날짜에 502 를 준다. 그것도 None 이다.
    """
    path = "/api/cgv/timetable?playDate={}&theaterCode={}".format(
        urllib.parse.quote(str(ymd)), urllib.parse.quote(str(site_no)))
    try:
        doc = _get(path)
    except Exception:
        return None
    if not isinstance(doc, dict) or not doc.get("success"):
        return None
    rows = (doc.get("data") or {}).get("timetable")
    return rows if isinstance(rows, list) else None


def _clean(value, limit=60):
    """남의 서버가 준 문자열. 길이를 자르고 줄바꿈을 없앤다."""
    return " ".join(str(value).split())[:limit]


def titles_on(site_no, ymd):
    """그 날짜에 걸린 영화 제목들. 실패하면 None, 없으면 빈 리스트."""
    rows = timetable(site_no, ymd)
    if rows is None:
        return None
    seen, out = set(), []
    for r in rows:
        nm = _clean(r.get("movieName", ""))
        if nm and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def find_titles(site_no, dates, keyword=None, max_dates=MAX_DATES):
    """여러 날짜를 훑어 제목을 모은다. (날짜별 제목, 걸린 날짜) 반환.

    keyword 가 있으면 그게 나오는 순간 멈춘다. 쓸데없이 더 묻지 않는다.
    한 날짜도 못 읽었으면 ({}, None).
    """
    found = {}
    hit = None
    for ymd in list(dates)[:max_dates]:
        names = titles_on(site_no, ymd)
        if names is None:
            continue
        found[ymd] = names
        if keyword and any(keyword in n for n in names):
            hit = ymd
            break
    return found, hit
