"""텔레그램 전송. 표준 라이브러리만 사용한다."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.telegram.org"
ENV_FILE = Path(__file__).with_name(".env")


def _load_dotenv():
    """로컬 실행용 .env 로드. GitHub Actions에서는 Secrets가 환경변수로 들어온다."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        # 환경변수가 이미 있으면 그쪽이 우선 (Actions Secrets 우선)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def credentials():
    _load_dotenv()
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    )


def save_credentials(token, chat_id):
    """GUI에서 호출. .env 에 저장한다 (.gitignore 대상)."""
    ENV_FILE.write_text(
        "TELEGRAM_BOT_TOKEN={}\nTELEGRAM_CHAT_ID={}\n".format(token, chat_id),
        encoding="utf-8",
    )
    os.environ["TELEGRAM_BOT_TOKEN"] = token
    os.environ["TELEGRAM_CHAT_ID"] = chat_id


def _call(token, method, payload, timeout=15):
    """timeout 은 소켓 대기 시간. getUpdates long polling 때는 길게 준다."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "{}/bot{}/{}".format(API, token, method),
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "imax-alert"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError("텔레그램 API 오류 {}: {}".format(exc.code, body)) from exc


def send(text, token=None, chat_id=None):
    env_token, env_chat = credentials()
    token = token or env_token
    chat_id = chat_id or env_chat
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다. "
            "설정 프로그램에서 입력하거나 환경변수로 넣으세요."
        )
    return _call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def discover_chat_id(token):
    """봇에게 아무 메시지나 보낸 뒤 호출하면 chat_id 후보를 찾아준다.

    [(chat_id, 표시이름), ...] 최신 대화 우선.
    """
    res = _call(token, "getUpdates", {})
    if not res.get("ok"):
        raise RuntimeError("getUpdates 실패: {}".format(res))

    found = []
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            continue
        name = (
            chat.get("title")
            or chat.get("username")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("type", "")
        )
        found.append((str(chat["id"]), name))

    seen, out = set(), []
    for cid, name in reversed(found):      # 최신 것부터
        if cid not in seen:
            seen.add(cid)
            out.append((cid, name))
    return out
