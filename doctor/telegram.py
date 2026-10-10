from __future__ import annotations

import json
import os
import urllib.request

from .redact import redact

LIMIT = 3900


def _api(method: str, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
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


MENU = [("status", "Problems now and every channel's state"), ("digest", "The daily digest, now"),
        ("review", "Run the channel review now"), ("scout", "Run the tool scout now"), ("help", "What I can do")]


def set_menu() -> bool:
    try:
        return bool(_api("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU]))
    except Exception as exc:
        print(f"telegram setMyCommands failed: {type(exc).__name__}")
        return False


def commands(offset: int) -> tuple[list[str], int]:
    """Owner's new bot commands since `offset`; returns (commands, next offset)."""
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    try:
        res = _api("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
    except Exception as exc:
        print(f"telegram getUpdates failed: {type(exc).__name__}")
        return [], offset
    out = []
    for u in (res or {}).get("result", []):
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == chat and (msg.get("text") or "").startswith("/"):
            out.append(msg["text"].split()[0].split("@")[0].lower())
    return out, offset
