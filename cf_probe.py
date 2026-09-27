#!/usr/bin/env python3
"""개봉예정작 감시가 실제로 되는지 확인. 확인 후 지운다."""
from __future__ import annotations

import cgv_api
import telegram_bot

movies = cgv_api.get_now_playing()
up = [m for m in movies if not m.get("atktRate") or m.get("atktRate") == "0"]
now = [m for m in movies if m not in up]

print("CGV 영화 목록 {}편 = 예매중 {}편 + 예매율없음(개봉예정) {}편".format(
    len(movies), len(now), len(up)))
print("\n예매중 예시:")
for m in now[:3]:
    print("  {} (예매율 {})".format(m.get("movNm"), m.get("atktRate")))
print("\n개봉예정 예시:")
for m in up[:12]:
    print("  {} (예매율 {!r}, 개봉 {})".format(
        m.get("movNm"), m.get("atktRate"), m.get("scnsrtYmd") or m.get("rlseYmd") or "?"))

print("\n--- verify_movie 가 개봉예정작을 제대로 잡는지 ---")
for title in ([up[0]["movNm"]] if up else []) + ["아바타", "존재하지않는영화제목zzz"]:
    hits, sugg = telegram_bot.verify_movie(title)
    print("  {!r:28} -> hits={} upcoming={} 추천={}".format(
        title, len(hits),
        [h["upcoming"] for h in hits[:3]], sugg[:3]))

print("\n--- 원본 필드 (개봉예정작 1편) ---")
if up:
    import json
    print(json.dumps(up[0], ensure_ascii=False, indent=2)[:900])
