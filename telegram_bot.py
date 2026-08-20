"""텔레그램 봇으로 감시 대상을 설정한다.

웹훅(공개 서버)이 없어도 되도록, watcher 루프가 돌 때마다 getUpdates 로
밀린 명령을 가져와 처리한다. 반응까지 최대 한 사이클(기본 60초) 걸린다.

명령
----
    /list    감시 목록 보기 (삭제 버튼 포함)
    /add     감시 대상 추가 (지역 -> 지점 -> 상영관 -> 영화)
    /check   지금 조회해서 현황 보기
    /status  실행 상태
    /help    도움말

보안
----
config 의 chat_id 와 같은 대화에서 온 명령만 처리한다.
공개 리포지토리라 봇을 찾아낸 제3자가 말을 걸어도 무시된다.
"""

from __future__ import annotations

import json

import cgv_api
import notifier

MAX_BUTTON_ROWS = 30


# ---------------------------------------------------------------- 저수준

def _api(method, payload):
    token, _ = notifier.credentials()
    return notifier._call(token, method, payload)


def send(text, keyboard=None, chat_id=None):
    _, chat = notifier.credentials()
    payload = {
        "chat_id": chat_id or chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _api("sendMessage", payload)


def edit(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return _api("editMessageText", payload)
    except Exception:
        return send(text, keyboard, chat_id)


def ack(callback_id, text=None):
    try:
        _api("answerCallbackQuery",
             {"callback_query_id": callback_id, "text": text or ""})
    except Exception:
        pass


def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


# ---------------------------------------------------------------- 화면

def help_text():
    return "\n".join([
        "<b>CGV 예매 오픈 알리미</b>",
        "",
        "/list — 감시 목록 보기 · 삭제",
        "/add — 감시 대상 추가",
        "/check — 지금 조회해서 현황 보기",
        "/status — 실행 상태",
        "",
        "명령은 다음 점검 때 처리되므로 최대 1분 걸립니다.",
    ])


def list_view(cfg, describe):
    targets = cfg.get("targets", [])
    if not targets:
        return "감시 중인 대상이 없습니다.\n/add 로 추가하세요.", None
    lines = ["<b>감시 목록</b>", ""]
    keyboard = []
    for i, target in enumerate(targets):
        lines.append("{}. {}".format(i + 1, describe(target)))
        keyboard.append([{
            "text": "❌ {}. {}".format(i + 1, target.get("site_nm", "")),
            "callback_data": "del|{}".format(i),
        }])
    return "\n".join(lines), keyboard


def region_view():
    regions = cgv_api.get_regions()
    buttons = [{"text": r["regnGrpNm"], "callback_data": "r|" + r["regnGrpNm"]}
               for r in regions]
    return "감시할 <b>지역</b>을 고르세요.", _rows(buttons, 3)


def site_view(region_nm):
    regions = cgv_api.get_regions()
    region = next((r for r in regions if r["regnGrpNm"] == region_nm), None)
    sites = (region or {}).get("siteList", [])[:MAX_BUTTON_ROWS * 2]
    buttons = [{"text": s["siteNm"], "callback_data": "s|{}|{}".format(
        s["siteNo"], s["siteNm"][:20])} for s in sites]
    return ("<b>{}</b> 의 지점을 고르세요.".format(region_nm), _rows(buttons, 2))


def screen_view(site_nm, site_no):
    try:
        has_imax, cnt = cgv_api.has_imax(site_no)
    except Exception:
        has_imax, cnt = False, "?"
    note = ("아이맥스 있음 (현재 {}편 상영 중)".format(cnt) if has_imax
            else "이 지점에는 아이맥스가 없습니다")
    buttons = [
        {"text": "아이맥스", "callback_data": "sc|아이맥스"},
        {"text": "4DX", "callback_data": "sc|4DX"},
        {"text": "SCREENX", "callback_data": "sc|SCREENX"},
        {"text": "전 상영관", "callback_data": "sc|ALL"},
    ]
    return ("<b>{}</b> — {}\n\n<b>상영관</b>을 고르세요.".format(site_nm, note),
            _rows(buttons, 2))


def movie_view():
    return ("<b>영화</b>를 정하세요.\n\n"
            "영화 제목을 <b>메시지로 보내주세요</b>. 일부만 적어도 되고 "
            "아직 개봉 안 한 영화도 됩니다.\n"
            "구분 없이 전부 받으려면 아래 버튼을 누르세요.",
            [[{"text": "전체 영화 (새 영화가 걸릴 때만 알림)",
               "callback_data": "mv|all"}]])


# ---------------------------------------------------------------- 처리

def handle(cfg, state, describe, check_fn, long_poll=0):
    """명령을 처리한다. (cfg 가 바뀌었는지, 응답을 보냈는지) 반환.

    long_poll 초를 주면 텔레그램에 그만큼 연결을 걸어두고 기다린다.
    감시 사이클 사이에 어차피 쉬는 시간이므로, 그 시간을 여기에 쓰면
    버튼을 눌렀을 때 1초 안에 반응한다. CGV 요청은 늘지 않는다.
    """
    token, my_chat = notifier.credentials()
    if not token or not my_chat:
        return False, False

    try:
        res = notifier._call(token, "getUpdates", {
            "offset": int(state.get("tg_offset", 0)),
            "timeout": int(long_poll),
            "allowed_updates": ["message", "callback_query"],
        }, timeout=int(long_poll) + 15)
    except Exception:
        return False, False
    if not res.get("ok"):
        return False, False

    updates = res.get("result", [])
    if not updates:
        return False, False

    changed = False
    responded = False
    draft = dict(state.get("tg_draft") or {})

    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        try:
            did_change, draft, ok = _one(upd, cfg, state, draft,
                                         describe, check_fn, str(my_chat))
            changed = changed or did_change
            responded = responded or ok
        except Exception as exc:                       # noqa: BLE001
            try:
                send("처리 중 오류가 났습니다.\n<code>{}</code>".format(
                    str(exc)[:300]))
            except Exception:
                pass

    state["tg_draft"] = draft
    return changed, responded


def _one(upd, cfg, state, draft, describe, check_fn, my_chat):
    """업데이트 하나 처리. (cfg 변경됨, draft, 응답함)"""
    import watcher                                     # 순환 임포트 회피

    # ---------------- 버튼 콜백
    if "callback_query" in upd:
        cb = upd["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        if chat_id != my_chat:
            return False, draft, False
        msg_id = cb["message"]["message_id"]
        data = cb.get("data", "")
        ack(cb["id"])

        if data.startswith("r|"):
            text, kb = site_view(data[2:])
            draft = {"region": data[2:]}
            edit(chat_id, msg_id, text, kb)

        elif data.startswith("s|"):
            _, site_no, site_nm = data.split("|", 2)
            draft.update({"site_no": site_no, "site_nm": site_nm})
            text, kb = screen_view(site_nm, site_no)
            edit(chat_id, msg_id, text, kb)

        elif data.startswith("sc|"):
            draft["screen"] = data[3:]
            text, kb = movie_view()
            draft["awaiting_movie"] = True
            edit(chat_id, msg_id, text, kb)

        elif data == "mv|all":
            draft["awaiting_movie"] = False
            target = _finish(draft, "")
            cfg.setdefault("targets", []).append(target)
            edit(chat_id, msg_id,
                 "추가했습니다.\n\n<b>{}</b>".format(describe(target)))
            return True, {}, True

        elif data.startswith("del|"):
            idx = int(data[4:])
            targets = cfg.get("targets", [])
            if 0 <= idx < len(targets):
                removed = targets.pop(idx)
                text, kb = list_view(cfg, describe)
                edit(chat_id, msg_id,
                     "삭제했습니다: {}\n\n{}".format(describe(removed), text), kb)
                return True, draft, True
            edit(chat_id, msg_id, "이미 삭제된 항목입니다.")

        return False, draft, True

    # ---------------- 일반 메시지
    msg = upd.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if chat_id != my_chat:
        return False, draft, False
    text = (msg.get("text") or "").strip()
    if not text:
        return False, draft, False

    if draft.get("awaiting_movie") and not text.startswith("/"):
        target = _finish(draft, text)
        cfg.setdefault("targets", []).append(target)
        send("추가했습니다.\n\n<b>{}</b>".format(describe(target)))
        return True, {}, True

    cmd = text.split()[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        send(help_text())
    elif cmd == "/list":
        body, kb = list_view(cfg, describe)
        send(body, kb)
    elif cmd == "/add":
        body, kb = region_view()
        send(body, kb)
        draft = {}
    elif cmd == "/check":
        send("조회 중입니다...")
        send(check_fn())
    elif cmd == "/status":
        send(_status(cfg, state, describe))
    else:
        send("모르는 명령입니다.\n\n" + help_text())
    return False, draft, True


def _finish(draft, keyword):
    """영화를 지정하지 않으면 알림이 쏟아지므로 '새 영화만'으로 잡는다."""
    return {
        "site_no": draft["site_no"],
        "site_nm": draft["site_nm"],
        "screen": draft.get("screen", "아이맥스"),
        "movie_keyword": keyword,
        "notify": "schedule" if keyword else "movie",
    }


def _status(cfg, state, describe):
    targets = cfg.get("targets", [])
    lines = ["<b>실행 상태</b>", ""]
    lines.append("마지막 점검  {}".format(state.get("last_run", "아직 없음")))
    lines.append("누적 점검     {}회".format(state.get("run_no", 0)))
    lines.append("기준선 회차   {}건".format(len(state.get("seen", []))))
    lines.append("")
    lines.append("<b>감시 대상 {}건</b>".format(len(targets)))
    for target in targets:
        lines.append("  · " + describe(target))
    return "\n".join(lines)
