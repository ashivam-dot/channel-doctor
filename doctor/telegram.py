from __future__ import annotations

import json
import os
import urllib.request

from .redact import redact

LIMIT = 3900


def _api(method: str, wait: float = 40, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=wait) as r:
        return json.load(r)


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


def send(text: str) -> bool:
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("telegram not configured; message:\n" + text)
        return False
    ok = True
    for part in chunks(redact(text)):
        try:
            _api("sendMessage", chat_id=chat, text=part, disable_web_page_preview=True)
        except Exception as exc:
            print(f"telegram send failed: {type(exc).__name__}")
            ok = False
    return ok


MENU = [("status", "Problems now and every channel's state"), ("channel", "One channel in detail: /channel atlas"),
        ("top", "Best uploads of the last 7 days across channels"), ("ask", "Ask anything about the channels"),
        ("digest", "The daily digest, now"), ("ypp", "Road to YPP: every channel's progress to earning"),
        ("review", "Run the channel review now"), ("scout", "Run the tool scout now"), ("id", "This chat's id"),
        ("help", "What I can do")]


def set_menu() -> bool:
    try:
        return bool(_api("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU]))
    except Exception as exc:
        print(f"telegram setMyCommands failed: {type(exc).__name__}")
        return False


def parse(text: str) -> tuple[str, str]:
    """(command, rest) for '/cmd@Bot rest'; a plain message is ('/ask', the message)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return "/ask", text
    head, _, rest = text.partition(" ")
    return head.split("@")[0].lower(), rest.strip()


def messages(offset: int, wait: int = 0) -> tuple[list[tuple[str, str]], int]:
    """The owner's new messages since `offset` as (command, rest), waiting up to `wait` s for one (long poll);
    returns (messages, next offset). Messages from any other chat are dropped."""
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    try:
        res = _api("getUpdates", wait=wait + 15, offset=offset, timeout=wait, allowed_updates=["message"])
    except Exception as exc:
        print(f"telegram getUpdates failed: {type(exc).__name__}")
        return [], offset
    out = []
    for u in (res or {}).get("result", []):
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == chat and (msg.get("text") or "").strip():
            out.append(parse(msg["text"]))
    return out, offset
