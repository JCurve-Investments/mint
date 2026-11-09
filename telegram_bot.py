"""
MINT Telegram bot
=================
Sends MINT's alerts to people's phones, through Telegram. Free.

Setting it up (once, about 2 minutes):
  1. In Telegram, open @BotFather and send /newbot. Choose a name (e.g. "MINT Alerts") and a username
     that ends in "bot" (e.g. "mint_alerts_aryan_bot").
  2. BotFather replies with a TOKEN (a long code). Paste it into the MINT app: Settings > Telegram.
     The token is like a password for the bot: never share it or put it in your report.

How people sign up:
  They open the bot's link (shown in the app), press START, and choose Gold, Bitcoin or Both.
  MINT stores only their Telegram chat number and their choice - no name, no phone number.
  Commands:  /start  /gold  /bitcoin  /both  /status  /stop

What gets sent (each message only once, only for the asset the person chose):
  Everyone ("Alerts only", the default):
     ALERT            an event the Decision agent stamped (says if it was on Watch before)
     WEEKLY SUMMARY   every Sunday: prices, what MINT found, and how good its decisions were
  People who chose "Everything" also get:
     WATCH            something may be happening, but there is not enough proof yet
     WATCH UPDATE     what happened to a Watch (confirmed and alerted, dropped, or info only)
     HEADS-UP         a scheduled gold event tomorrow (US jobs report, inflation, Fed decision)
  Nothing older than 6 hours is sent (so turning the bot on later does not send old news).
  Test runs never send anything.

The bot only works while the MINT app is open (it checks for new messages every few seconds).
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from supervisor import data_folder, load_memory, save_memory, asset_states, parse_time, hours_between

API = "https://api.telegram.org"          # can be pointed at a pretend server for testing
SEND_ALERTS_NEWER_THAN_HOURS = 6
WATCH_NOTICE_NEWER_THAN_HOURS = 6
KEYBOARD = {"keyboard": [[{"text": "Gold"}, {"text": "Bitcoin"}, {"text": "Both"}],
                         [{"text": "Alerts only"}, {"text": "Everything"}],
                         [{"text": "Status"}, {"text": "Stop alerts"}]], "resize_keyboard": True}


# ---------------------------------------------------------------------------
# Talking to Telegram
# ---------------------------------------------------------------------------
def call(token, method, params=None, timeout=25):
    data = json.dumps(params or {}).encode()
    request = urllib.request.Request(f"{API}/bot{token}/{method}", data=data, method="POST",
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            reply = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read().decode())
        except Exception:
            raise RuntimeError(f"Telegram error {e.code}")
    except Exception as e:                      # could not reach Telegram at all
        from common import friendly_error
        raise RuntimeError(friendly_error(e, "Telegram (api.telegram.org)"))
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram: {reply.get('description', 'unknown error')}")
    return reply["result"]


def check_token(token):
    """Returns the bot's username if the token works."""
    return call(token, "getMe")["username"]


# ---------------------------------------------------------------------------
# Subscribers (stored on this computer: data/subscribers.json)
# ---------------------------------------------------------------------------
def load_subscribers():
    path = data_folder(False) / "subscribers.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_subscribers(subs):
    (data_folder(False) / "subscribers.json").write_text(json.dumps(subs, indent=2), encoding="utf-8")


def load_state():
    path = data_folder(False) / "telegram_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"offset": 0}


def save_state(state):
    (data_folder(False) / "telegram_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def status_text():
    memory = load_memory(False)
    states = asset_states(memory, [], datetime.now(timezone.utc))
    lines = ["MINT right now:"]
    for asset, st in states.items():
        lines.append(f"\n{asset}: {st['state'].upper()}")
        for r in st["reasons"][:3]:
            lines.append(f"- {r}")
    lines.append("\nRisk information only, not financial advice.")
    return "\n".join(lines)


WELCOME = ("Welcome to MINT - Market Intelligence and News Tracker.\n\n"
           "I send a short alert when something important happens that could affect Gold or Bitcoin, "
           "and only when trusted sources confirm it. Most days there will be no alert - that is normal.\n\n"
           "1. Choose the asset: Gold, Bitcoin or Both.\n"
           "2. Choose how much: 'Everything' (the default: every event MINT finds with its view and prediction, "
           "every update, the result 1 day later, heads-ups and a Sunday summary) or 'Alerts only' "
           "(confirmed alerts + the Sunday summary).\n\n"
           "Send /status any time, or /stop to leave.\n\n"
           "MINT is a student project. It shares risk information, not financial advice.")


def handle_message(token, msg, subs):
    """Reply to one message someone sent the bot. Returns a short note for the log."""
    import os
    chat = str(msg["chat"]["id"])
    if os.environ.get("MINT_CLOUD") == "1":
        # In the cloud the data is in a PUBLIC repository, so MINT stores no subscriber list:
        # everything goes to the channel instead.
        channel = os.environ.get("TELEGRAM_CHANNEL", "")
        text = (msg.get("text") or "").strip().lower()
        reply = status_text() if text in ("/status", "status") else (
            "Hi! MINT posts every event, update and result in its channel" +
            (f": {channel}\nTap the link and press Join." if channel else ".") +
            "\nSend /status any time to see Gold and Bitcoin right now.\n\nRisk information only, not financial advice.")
        call(token, "sendMessage", {"chat_id": int(chat), "text": reply})
        return None
    text = (msg.get("text") or "").strip().lower()
    reply, note = None, None
    choice = {"/gold": ["Gold"], "gold": ["Gold"], "/bitcoin": ["Bitcoin"], "bitcoin": ["Bitcoin"],
              "/both": ["Gold", "Bitcoin"], "both": ["Gold", "Bitcoin"]}
    level = {"/alerts": "alerts", "alerts only": "alerts", "/everything": "everything", "everything": "everything"}
    new = lambda: {"assets": ["Gold", "Bitcoin"], "level": "everything", "joined": datetime.now(timezone.utc).isoformat()}
    if text.startswith("/start"):
        subs.setdefault(chat, new())
        reply, note = WELCOME + "\n\nFor now: BOTH assets, EVERYTHING.", "new subscriber"
    elif text in choice:
        subs.setdefault(chat, new())["assets"] = choice[text]
        reply = f"Done. Asset: {' and '.join(choice[text])}."
        note = f"subscriber chose {' and '.join(choice[text])}"
    elif text in level:
        subs.setdefault(chat, new())["level"] = level[text]
        reply = ("Done. You get alerts and the Sunday summary." if level[text] == "alerts" else
                 "Done. You get everything: alerts, watches, watch updates, heads-ups and the Sunday summary.")
        note = f"subscriber chose {level[text]}"
    elif text in ("/stop", "stop alerts", "stop"):
        subs.pop(chat, None)
        reply, note = "You will not get any more messages. Send /start to sign up again.", "subscriber left"
    elif text in ("/status", "status"):
        reply = status_text()
    else:
        reply = "Send /gold, /bitcoin, /both, /alerts, /everything, /status or /stop."
    call(token, "sendMessage", {"chat_id": int(chat), "text": reply, "reply_markup": KEYBOARD})
    return note


def check_messages(token):
    """Read new messages sent to the bot and answer them. Returns notes for the activity log."""
    state, subs, notes = load_state(), load_subscribers(), []
    updates = call(token, "getUpdates", {"offset": state.get("offset", 0), "timeout": 0,
                                         "allowed_updates": ["message"]}, timeout=15)
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            continue
        try:
            note = handle_message(token, msg, subs)
            if note:
                notes.append(note)
        except Exception as e:
            notes.append(f"could not reply: {e}")
    save_subscribers(subs)
    save_state(state)
    return notes


# ---------------------------------------------------------------------------
# Writing the messages
# ---------------------------------------------------------------------------
FOOTER = "Risk information only, not financial advice."
HEADLINE = {"watch": "WATCH (not proven yet)", "confirmed": "BEING CHECKED", "alerted": "ALERT",
            "context": "INFO ONLY (slow factor)", "summary": "OLDER NEWS (weekly summary)", "old": "TOO OLD TO BE NEWS",
            "expired": "NOT CONFIRMED", "rejected": "CHECKED - NO ALERT"}


def view_lines(ev):
    """MINT's view: prediction, evidence, confidence, what to watch."""
    p, a = ev.get("prediction") or {}, ev.get("analysis") or {}
    lines = []
    if a.get("summary") and a.get("by", "").startswith("AI"):
        lines.append(f"MINT's view: {a['summary']}")
    if p.get("direction") in ("up", "down"):
        lines.append(f"Prediction (next day): {ev['asset']} {p['direction'].upper()}"
                     + (f" - {a['confidence']} confidence ({a.get('confidence_reason', '')})" if a.get("confidence") else ""))
    else:
        lines.append("Prediction: unclear - not enough evidence")
    sims = [x for x in (p.get("similar") or []) if x.get("d1") is not None]
    if sims:
        lines.append("Similar past events:")
        lines += [f"- {x['date']}: {str(x['title']).replace('TEST: ', '')[:70]} -> {x['d1']:+.1f}% next day" for x in sims[:3]]
    elif p.get("reason"):
        lines.append(f"Why: {p['reason'][:200]}")
    if a.get("watch_next"):
        lines.append(f"Watch next: {a['watch_next']}")
    return lines


def event_text(ev, update=False):
    orgs = sorted({s["organisation"] for s in ev["sources"]})
    head = f"MINT {'update' if update else 'event'} · {ev['asset']} · {HEADLINE.get(ev['state'], ev['state'].upper())}"
    lines = [head, ev["title"], "",
             f"Sources: {', '.join(orgs)}", f"Status: {CLOSED_WORDS.get(ev['state']) or ev.get('state_reason', '')}"]
    o = ev.get("outcome") or {}
    if o.get("moved_before_mint") is not None:
        lines.append(f"{ev['asset']} had moved {o['moved_before_mint']:+.1f}% between the first report and MINT noticing.")
    lines += [""] + view_lines(ev) + ["", FOOTER]
    return "\n".join(lines)


CLOSED_WORDS = {
    "watch": "only one source so far - waiting for a second before any alert",
    "confirmed": "enough proof - the Decision agent is checking it",
    "expired": "No second source confirmed it in time, so there is no alert (still tracked).",
    "context": "Confirmed, but it is a slow factor, so it is info only - no alert.",
    "summary": "Older news (2 to 7 days), so it only goes in the weekly summary - no alert.",
    "old": "Too old to be news - no alert.",
}


def result_text(ev):
    o, p = ev.get("outcome") or {}, ev.get("prediction") or {}
    verdict = {"right": "RIGHT", "wrong": "WRONG", "no_clear_move": "NO CLEAR MOVE"}[o["effect"]]
    return (f"MINT result · {ev['asset']} · prediction {verdict}\n{ev['title']}\n\n"
            f"Predicted: {ev['asset']} {str(p.get('direction', '?')).upper()}\n"
            f"After 1 day: {o['d1']['move']:+.1f}%\n{o.get('effect_reason', '')}\n\n"
            "Every result is added to MINT's past events library, so future predictions learn from it.\n" + FOOTER)


def was_on_watch(ev):
    for h in ev.get("history", []):
        if "-> watch" in h["note"]:
            return parse_time(h["time"])
    return None


def alert_text(ev):
    text = ev["decision"]["telegram_message"]
    since = was_on_watch(ev)
    if since:
        text = text.replace("\n", f"\n(Update: this was on Watch since {since:%d %b %H:%M} UTC.)\n", 1)
    extra = view_lines(ev)
    return text.replace(FOOTER, "\n".join(extra) + "\n\n" + FOOTER) if FOOTER in text else text + "\n" + "\n".join(extra)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def log_message(kind, title, people):
    path = data_folder(False) / "telegram_log.json"
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    log.append({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "title": title[:120], "to": people})
    path.write_text(json.dumps(log[-300:], indent=2), encoding="utf-8")


def deliver(token, subs, text, asset=None, level="alerts", kind="", title="", channel=""):
    """Send one message to every subscriber who wants it, and to the channel (if set). Returns how many got it."""
    people = [c for c, s in subs.items()
              if (asset is None or asset in s.get("assets", []))
              and (level == "alerts" or s.get("level", "everything") == "everything")]
    ok = 0
    for chat in people:
        try:
            call(token, "sendMessage", {"chat_id": int(chat), "text": text[:4000], "disable_web_page_preview": True})
            ok += 1
            time.sleep(0.05)
        except Exception as e:
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                subs.pop(chat, None)          # they blocked the bot: remove them
    if channel:
        try:
            call(token, "sendMessage", {"chat_id": channel, "text": text[:4000], "disable_web_page_preview": True})
            ok += 1
        except Exception as e:
            print(f"   CHANNEL problem: {e} (is the bot an admin of {channel}?)")
    log_message(kind, title, ok)
    return ok


def send_alerts(token, test_mode=False, now=None, calendar_warnings=None, channel=""):
    """Send everything that is due. Prints what it did (shown in the app). Returns how many messages were delivered.
       'Everything' subscribers and the channel get: every new event with MINT's view, every status change,
       the 1-day result, heads-ups and the weekly summary. 'Alerts only' subscribers get alerts + weekly summary."""
    import weekly
    now = now or datetime.now(timezone.utc)
    memory = load_memory(test_mode)
    subs, state = load_subscribers(), load_state()
    total, waiting = 0, 0
    fresh = lambda t, hours: t and hours_between(now, parse_time(t)) <= hours

    for ev in memory["events"]:
        if ev["state"] is None:
            continue
        tg = ev.setdefault("telegram", {})
        d = ev.get("decision") or {}
        # 1. ALERTS (everyone who chose this asset)
        if ev["state"] == "alerted" and d.get("telegram_message") and not d.get("telegram_sent_at"):
            if test_mode:
                d["telegram_status"] = "test run - not sent"
            elif not token:
                d["telegram_status"] = "not sent - Telegram bot not set up (Settings)"
                waiting += 1
            elif not fresh(d.get("time"), SEND_ALERTS_NEWER_THAN_HOURS):
                d["telegram_status"] = "not sent - too old by the time Telegram was set up"
                d["telegram_sent_at"] = now.isoformat()
            else:
                n = deliver(token, subs, alert_text(ev), ev["asset"], "alerts", "alert", ev["title"], channel)
                d["telegram_sent_at"] = now.isoformat()
                d["telegram_status"] = f"sent to {n} at {now:%d %b %H:%M} UTC"
                tg["sent_state"] = "alerted"
                total += n
                print(f"   ALERT    -> {n}: {ev['title'][:80]}")
        if test_mode or not token:
            continue
        # 2. EVERY NEW EVENT, with MINT's view (only once, only if it is recent)
        if not tg.get("sent_state") and ev["state"] != "alerted":
            if fresh(ev["first_seen"], WATCH_NOTICE_NEWER_THAN_HOURS):
                n = deliver(token, subs, event_text(ev), ev["asset"], "everything", "event", ev["title"], channel)
                total += n
                print(f"   EVENT    -> {n}: {ev['title'][:80]} ({ev['state']})")
            tg["sent_state"] = ev["state"]
        # 3. STATUS CHANGED since we last told people (e.g. watch -> not confirmed)
        elif tg.get("sent_state") and tg["sent_state"] != ev["state"] and ev["state"] not in ("alerted", "confirmed"):
            n = deliver(token, subs, event_text(ev, update=True), ev["asset"], "everything", "update", ev["title"], channel)
            tg["sent_state"] = ev["state"]
            total += n
            print(f"   UPDATE   -> {n}: {ev['title'][:80]} (now {ev['state']})")
        # 4. RESULT: was the prediction right after 1 day?
        o = ev.get("outcome") or {}
        if o.get("effect") in ("right", "wrong", "no_clear_move") and o.get("d1") and not tg.get("result_sent"):
            if fresh(o["d1"].get("time"), 48):
                n = deliver(token, subs, result_text(ev), ev["asset"], "everything", "result", ev["title"], channel)
                total += n
                print(f"   RESULT   -> {n}: {ev['title'][:70]} ({o['effect']})")
            tg["result_sent"] = now.isoformat()

    if not test_mode and token:
        # 5. HEADS-UP before scheduled gold events, once each
        sent_before = state.setdefault("headsup_sent", [])
        for w in calendar_warnings or []:
            key = f"{now:%Y-%m-%d} {w}"
            if key not in sent_before:
                n = deliver(token, subs, f"MINT heads-up · Gold\n{w}\n\n{FOOTER}", "Gold", "everything", "heads-up", w, channel)
                sent_before.append(key)
                total += n
                print(f"   HEADS-UP -> {n}: {w}")
        state["headsup_sent"] = sent_before[-50:]

    # 6. WEEKLY SUMMARY: every Sunday after 12:00 UTC (17:30 in India), saved even without Telegram
    week = f"{now:%G-W%V}"
    if now.weekday() == 6 and now.hour >= 12 and state.get("weekly_done") != week and not test_mode:
        summary = weekly.build(False, now)
        weekly.save(summary, False)
        if token:
            n = deliver(token, subs, summary["text"], None, "alerts", "weekly summary", summary["week"], channel)
            total += n
            print(f"   WEEKLY   -> {n}: summary for {week}")
        state["weekly_done"] = week

    save_memory(memory, test_mode)
    save_subscribers(subs)
    if not test_mode:
        save_state(state)
    print(f"\nBOTTOM LINE: {total} message(s) delivered, {len(subs)} subscriber(s)"
          + (f", posting to channel {channel}" if channel else "")
          + (f", {waiting} alert(s) waiting for the bot to be set up" if waiting else "")
          + (" (test run: nothing is sent)" if test_mode else "") + ".")
    return total


def send_test(token):
    subs = load_subscribers()
    for chat in subs:
        call(token, "sendMessage", {"chat_id": int(chat), "text": "MINT test message: the bot is working. "
                                    "Real alerts will look like this, but with the event."})
    return len(subs)
