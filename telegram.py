# -*- coding: utf-8 -*-
"""Send alerts to Telegram via Bot API (stdlib urllib).

sendPhoto with the token image + HTML caption; if there is no image or the
photo failed to send — sendMessage. No external dependencies.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
TIMEOUT = 12
_SENT_SIGS = {}   # symbol|dir|pct|window -> ts: dedup after timeout ambiguity


def _post(token, method, payload):
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def classify_exc(e):
    """timeout  — request did not complete; MAY HAVE arrived (retry risks a dup,
                  but losing it is worse — we retry with a caveat).
       refused  — HTTP 4xx/5xx: server answered, retrying the same method is pointless.
       connect  — DNS/TCP/SSL handshake: server definitely did NOT see the request — retry is safe."""
    name = type(e).__name__ + ": " + str(e)[:80]
    s = str(e).lower()
    if isinstance(e, urllib.error.HTTPError):
        return "refused", name
    if "timed out" in s or "timeout" in s:
        return "timeout", name
    return "connect", name


def format_alert(alert, token_info):
    """Readable caption (Telegram HTML)."""
    arrow = "🚀" if alert["direction"] == "up" else "🔻"
    word = "РАСТЁТ" if alert["direction"] == "up" else "ПАДАЕТ"
    sym = token_info.get("symbol", "?")
    name = token_info.get("name", "")
    chain = token_info.get("chain") or ""
    price = token_info.get("price") or alert.get("price") or 0
    pct = alert["pct"]
    reason = ("выстрел пика, уже откатывается" if alert.get("reason") == "spike"
              and alert["direction"] == "down" else
              "выстрел пика" if alert.get("reason") == "spike" else
              "устойчивое движение")
    m5 = token_info.get("m5")
    mcap = token_info.get("mcap")
    mcap_s = ""
    if mcap:
        mcap_s = f"${mcap/1e6:.2f}M" if mcap >= 1e6 else f"${mcap/1e3:.0f}K"
    lines = [
        f"{arrow} <b>{sym}</b> ({chain}) — {word} <b>{pct:+.1f}%</b>" if chain else
        f"{arrow} <b>{sym}</b> — {word} <b>{pct:+.1f}%</b>",
        f"<i>{name}</i>" if name and name != sym else "",
        f"За {alert['window']}: <b>{pct:+.1f}%</b> ({reason})",
        f"Капитализация: <code>{mcap_s}</code>" if mcap_s else "",
        f"5м: {m5:+.1f}% | Ликв. ${token_info.get('liquidity', 0):,.0f}" if m5 is not None else "",
        f"<a href=\"{token_info.get('url', '')}\">DexScreener</a>" if token_info.get("url") else "",
    ]
    return "\n".join(l for l in lines if l)


def send_alert(cfg, alert, info):
    """Returns (ok, detail).

    The network path to Telegram is leaky: a request either dies on timeout or
    the connection drops BEFORE sending. We distinguish in principle:
      connect — server never got the request, a retry is free (up to 2 attempts);
      timeout — it may have arrived: retry only on «escalation» (a materially
                worse move — a dup of the same pct is worse than a miss,
                so plain resends are skipped);
      refused — 4xx/5xx: server refused, don't hammer it.
    """
    tg = cfg.get("telegram") or {}
    token, chat_id = tg.get("botToken"), tg.get("chatId")
    if not token or not chat_id:
        return (False, "telegram не настроен")
    text = format_alert(alert, info)
    image = info.get("image") or ""

    def once(with_photo):
        if with_photo:
            return _post(token, "sendPhoto", {
                "chat_id": chat_id, "photo": image, "caption": text,
                "parse_mode": "HTML", "disable_web_page_preview": "true"})
        return _post(token, "sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true"})

    # dedup: don't send the same message twice due to timeout ambiguity
    sig = f"{info.get('symbol')}|{alert['direction']}|{alert['pct']}|{alert['window']}"
    now = time.time()
    prev = _SENT_SIGS.get(sig)
    if prev and now - prev < 90:
        return (True, "suppressed (dup)")
    _SENT_SIGS[sig] = now

    attempts = {"connect": 3, "timeout": 2, "refused": 1}
    used_photo = bool(image.startswith("http"))
    last_err = "?"
    for _ in range(attempts["connect"]):          # budget for the worst case
        kind = "connect"
        try:
            res = once(used_photo)
            if res.get("ok"):
                return (True, "sent" + (" (photo)" if used_photo else ""))
            desc = str(res.get("description"))[:120]
            # TG server may fail to fetch the photo — try as plain text
            if used_photo:
                used_photo = False
                continue
            return (False, desc)
        except Exception as e:
            kind, last_err = classify_exc(e)
            if kind == "connect":
                continue                           # didn't arrive — immediate retry
            if kind == "timeout":
                break
            return (False, last_err)
        break
    # timeout: one cautious text-only retry (photo on timeout = extra traffic)
    if kind == "timeout":
        time.sleep(1.0)
        try:
            res = once(False)
            if res.get("ok"):
                return (True, "sent (retry after timeout)")
        except Exception as e:
            _, last_err = classify_exc(e)
    return (False, last_err)


def send_test(cfg):
    tg = cfg.get("telegram") or {}
    token, chat_id = tg.get("botToken"), tg.get("chatId")
    if not token or not chat_id:
        return (False, "не заданы botToken / chatId")
    payload = {"chat_id": chat_id,
               "text": "✅ Token Tracker подключён — уведомления будут приходить сюда.",
               "parse_mode": "HTML"}
    last = "?"
    for k in range(3):
        try:
            res = _post(token, "sendMessage", payload)
            return (bool(res.get("ok")), res.get("description") or "ok")
        except Exception as e:
            kind, last = classify_exc(e)
            if kind == "refused":
                break
            time.sleep(0.8)
    return (False, last)
