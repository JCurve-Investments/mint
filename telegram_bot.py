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
from datetime import datetime, timedelta, timezone

from supervisor import data_folder, load_memory, save_memory, asset_states, parse_time, hours_between
from common import load_json

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


KEY_TEXT = ("📖 How to read MINT messages\n\n"
            "Line 1 says everything:\n"
            "🥇 Gold ↓ -0.8% to -1.5% within ~1.5 days · 70% likely\n"
            "= MINT expects gold to FALL by 0.8% to 1.5% within about 1.5 days, and it is 70% likely.\n\n"
            "Where the numbers come from:\n"
            "• size = the typical move after similar past events\n"
            "• time = how long those moves took (bigger moves take longer)\n"
            "• likely = the 3 lenses counted equally: the news (how many similar past events went that way), "
            "trading volume and buyers vs sellers (each agrees, disagrees or shows no signal)\n\n"
            "First icon: 🚨 alert · 👀 watch (not confirmed yet) · ℹ️ info only (slow factor) · ✔️ checked, no alert\n\n"
            "Results (one message, as soon as it is known):\n"
            "✅ HIT - moved at least half the predicted amount the predicted way and held it 2+ hours\n"
            "🟨 PARTIAL - the right way, but too small or too short (counts as right)\n"
            "❌ MISS - moved clearly the other way\n"
            "➖ NO CLEAR MOVE - nothing happened inside the time window\n\n"
            "Every evening at 9 pm (India time): the detailed daily summary.\n\n" + "Risk information only, not financial advice.")
KEY_VERSION = 3


WELCOME = ("Welcome to MINT - Market Intelligence and News Tracker.\n\n"
           "I send a short alert when something important happens that could affect Gold or Bitcoin, "
           "and only when trusted sources confirm it. Most days there will be no alert - that is normal.\n\n"
           "1. Choose the asset: Gold, Bitcoin or Both.\n"
           "2. Choose how much: 'Everything' (the default: every event MINT finds with its view and prediction, "
           "every update, the result 1 day later, heads-ups and a Sunday summary) or 'Alerts only' "
           "(confirmed alerts + the Sunday summary).\n\n"
           "At a glance: 🚨 alert · 👀 watch · 🔴 high / 🟠 medium / 🟡 low impact · 📈 up / 📉 down · "
           "▰▰▰ confidence.\n\n"
           "Send /status any time, or /stop to leave.\n\n"
           "MINT is a student project. It shares risk information, not financial advice.")


def handle_message(token, msg, subs):
    """Reply to one message someone sent the bot. Returns a short note for the log."""
    import os
    chat = str(msg["chat"]["id"])
    if os.environ.get("MINT_CLOUD") == "1":
        # In the cloud the data is in a PUBLIC repository, so MINT stores no subscriber list:
        # everything goes to the channel instead.
        channel = os.environ.get("TELEGRAM_CHANNEL_LINK") or os.environ.get("TELEGRAM_CHANNEL", "")
        if channel.lstrip("-").isdigit():
            channel = ""                       # a number ID is no use to people; only show a real link
        text = (msg.get("text") or "").strip().lower()
        if text in ("/id", "id"):
            call(token, "sendMessage", {"chat_id": int(chat), "text": f"Your chat ID is {chat}. To get the private "
                 "signal check, add it as the GitHub secret TELEGRAM_OWNER."})
            return None
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
HEADLINE = {"watch": "WATCH (not confirmed yet)", "confirmed": "BEING CHECKED", "alerted": "ALERT",
            "context": "INFO ONLY (slow factor)", "summary": "OLDER NEWS (weekly summary)", "old": "TOO OLD TO BE NEWS",
            "expired": "NOT CONFIRMED", "rejected": "CHECKED - NO ALERT"}


# ---------------------------------------------------------------------------
# "At a glance" line: impact, direction and confidence, so you know what to read first
# ---------------------------------------------------------------------------
STATE_ICON = {"watch": "👀", "confirmed": "🔎", "alerted": "🚨", "context": "ℹ️", "summary": "🗂", "old": "🗂",
              "expired": "⌛", "rejected": "✔️"}
IMPACT_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
ASSET_ICON = {"Bitcoin": "₿", "Gold": "🥇"}
DIRECTION_ICON = {"up": "📈 UP", "down": "📉 DOWN"}
CONFIDENCE_BARS = {"high": "▰▰▰", "medium": "▰▰▱", "low": "▰▱▱"}
RESULT_ICON = {"right": "✅", "wrong": "❌", "no_clear_move": "➖"}
GLANCE_KEY = "🔴 high · 🟠 medium · 🟡 low impact   ▰▰▰ = confidence"


def impact_of(ev):
    """high / medium / low. The Decision agent's answer if it decided; otherwise from the factor's strength."""
    d = ev.get("decision") or {}
    if d.get("impact") in IMPACT_ICON:
        return d["impact"]
    if ev["state"] in ("context", "summary", "old"):
        return "low"
    try:
        strength = {f["id"]: f.get("strength") for f in load_json("factors.json")["factors"]}.get(ev["factor"])
    except Exception:
        strength = None
    return "high" if strength == "strong" else "medium"


def glance_lines(ev, kind):
    """Two short lines at the top of every message, e.g.
         🚨 ALERT · ₿ Bitcoin
         🔴 HIGH impact · 📉 DOWN · Confidence ▰▰▰ high"""
    p, a = ev.get("prediction") or {}, ev.get("analysis") or {}
    impact = impact_of(ev)
    conf = a.get("confidence")
    second = f"{IMPACT_ICON[impact]} {impact.upper()} impact · {DIRECTION_ICON.get(p.get('direction'), '❔ unclear')}"
    if conf in CONFIDENCE_BARS:
        second += f" · Confidence {CONFIDENCE_BARS[conf]} {conf}"
    return [f"{STATE_ICON.get(ev['state'], '•')} MINT {kind} · {ASSET_ICON.get(ev['asset'], '')} {ev['asset']}", second]


def view_lines(ev):
    """MINT's view: prediction, evidence, confidence, what to watch."""
    p, a = ev.get("prediction") or {}, ev.get("analysis") or {}
    lines = []
    if a.get("summary") and a.get("by", "").startswith("AI"):
        lines.append(f"MINT's view: {a['summary']}")
    if p.get("direction") in ("up", "down"):
        lines.append(f"Predicted effect: {ev['asset']} {p['direction'].upper()}"
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


def social_line(ev):
    """'📱 From social media: ...' when an event comes only from social media posts, else ''."""
    if ev["sources"] and all(s.get("kind") == "social" for s in ev["sources"]):
        accounts = sorted({s["organisation"] for s in ev["sources"]})
        return f"📱 From social media: {', '.join(accounts)} (approved account) - no news report yet"
    return ""


SHORT_FOOTER = "Not financial advice."
STATUS_SHORT = {"watch": "Watch: not confirmed by an approved source yet", "confirmed": "Being checked",
                "alerted": "Alert", "context": "Info only (slow factor)", "summary": "Older news",
                "old": "Too old to be news", "expired": "Not confirmed in time", "rejected": "Checked - no alert needed"}


def one_liner(ev, limit=160):
    """What happened, in one line: the AI's one-sentence summary if there is one, otherwise the headline."""
    a = ev.get("analysis") or {}
    text = a.get("summary") if str(a.get("by", "")).startswith("AI") and a.get("summary") else ev["title"]
    text = " ".join(str(text).replace("TEST: ", "").split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


def evidence_line(ev):
    """'Why: 4 of 5 similar past events fell (typical -1.4%) · market: volume 1.6x, real selling'"""
    p = ev.get("prediction") or {}
    f = p.get("forecast") or {}
    if p.get("direction") not in ("up", "down"):
        return "Why: not enough evidence for a prediction yet"
    word = "rose" if p["direction"] == "up" else "fell"
    if ev.get("market_triggered"):
        lt = f.get("lens_text") or {}
        return "Why: " + "; ".join(x for x in (lt.get("volume"), lt.get("buy_sell")) if x)[:230]
    if f.get("similar"):
        why = f"Why: {f['agree']} of {f['similar']} similar past events {word}"
        if f.get("agree"):
            why += f" (typical {abs(f['mid']):.1f}%)"
    elif f:
        why = "Why: no similar past events yet - " + ("factor pattern" if p.get("by") != "AI" else "AI's reading")
    else:
        why = f"Why: {p.get('reason', '')[:120]}"
    agree = [n.split(": ")[1] for n in f.get("lens_notes", []) if n.startswith("agrees")]
    against = [n.split(": ")[1] for n in f.get("lens_notes", []) if n.startswith("disagrees")]
    if agree:
        why += " · market agrees (" + ", ".join(agree) + ")"
    if against:
        why += " · market disagrees (" + ", ".join(against) + ")"
    return why


def source_line(ev):
    if ev.get("market_triggered"):
        return "📊 Market signal (volume + buyers vs sellers) · no news report found yet"
    social = social_line(ev)
    status = STATUS_SHORT.get(ev["state"], ev["state"])
    if social:
        return f"{status} · {social}"
    orgs = sorted({s["organisation"] for s in ev["sources"]})
    return f"{status} · Source: {', '.join(orgs[:3])}"


def already_moved(ev):
    o, f = ev.get("outcome") or {}, ((ev.get("prediction") or {}).get("forecast") or {})
    m = o.get("moved_before_mint")
    if m is None or abs(m) < max(0.2, f.get("floor", 0.3) / 2):
        return ""
    return f"Already moved {m:+.1f}% before MINT saw it."


def event_text(ev, update=False):
    """The short instant message for a new event (4 or 5 lines)."""
    import forecast
    icon = "🚨" if ev["state"] == "alerted" else STATE_ICON.get(ev["state"], "•")
    if update:
        return (f"🔄 Update · {ASSET_ICON.get(ev['asset'], '')} {ev['asset']}: {one_liner(ev, 110)}\n"
                f"Now: {CLOSED_WORDS.get(ev['state']) or STATUS_SHORT.get(ev['state'], ev['state'])}\n{SHORT_FOOTER}")
    lines = [f"{icon} {forecast.headline(ev)}", one_liner(ev), evidence_line(ev), source_line(ev)]
    moved = already_moved(ev)
    if moved:
        lines.append(moved)
    return "\n".join(lines) + f"\n{SHORT_FOOTER}"


CLOSED_WORDS = {
    "watch": "reported, but not by an approved source yet - MINT tracks its impact",
    "confirmed": "reported by an approved source - the Decision agent is checking it",
    "expired": "No approved source reported it in time, so there is no alert (still tracked).",
    "context": "Confirmed, but it is a slow factor, so it is info only - no alert.",
    "summary": "Older news (2 to 7 days), so it only goes in the weekly summary - no alert.",
    "old": "Too old to be news - no alert.",
}


def checks_lines(ev):
    """All price checks so far, e.g. '✅ 5 hours: -1.2%'."""
    from prices import CHECKPOINTS, CHECKPOINT_NAMES
    o, out = ev.get("outcome") or {}, []
    for k in CHECKPOINTS:
        if o.get(k):
            mark = {"right": "✅", "wrong": "❌"}.get(o[k].get("verdict"), "➖")
            out.append(f"{mark} {CHECKPOINT_NAMES[k]}: {o[k]['move']:+.1f}%")
    return out


RESULT_WORD = {"hit": ("✅", "HIT"), "miss": ("❌", "MISS"), "partial": ("🟨", "PARTIAL"),
               "no_clear_move": ("➖", "NO CLEAR MOVE")}


def result_line(ev, title=True):
    """'✅ HIT · 🥇 Gold ↓ predicted -0.8% to -1.5% within ~1.5 days → reached -1.6% (inside the predicted range)'"""
    import forecast
    o, p = ev.get("outcome") or {}, ev.get("prediction") or {}
    r, f = o.get("result") or {}, p.get("forecast") or {}
    icon, word = RESULT_WORD.get(r.get("verdict"), ("•", str(r.get("verdict", "")).upper()))
    arrow = "↑" if p.get("direction") == "up" else "↓"
    s_ = "+" if p.get("direction") == "up" else "-"
    lo, hi = sorted([abs(f.get("low", 0)), abs(f.get("high", 0))])
    pred = f"predicted {s_}{lo:.1f}% to {s_}{hi:.1f}% within ~{forecast.fmt_hours(f.get('window_hours'))}"
    v = r.get("verdict")
    if v == "hit":
        got = f"reached {r['peak']:+.1f}%" + (f" ({r['size']})" if r.get("size") else "")
    elif v == "miss":
        got = f"went the other way ({-r['threshold'] if p.get('direction') == 'up' else r['threshold']:+.1f}% or more for 2+ hours)"
    elif v == "partial":
        got = f"only {r['peak']:+.1f}% or did not hold 2 hours"
    else:
        got = f"no clear move (biggest {r.get('peak', 0):+.1f}%)"
    line = f"{icon} {word} · {ASSET_ICON.get(ev['asset'], '')} {ev['asset']} {arrow} {pred} → {got}"
    return line + (f"\n{one_liner(ev, 120)}" if title else "")


def result_text(ev, final=True):
    return result_line(ev) + f"\n{SHORT_FOOTER}"


def was_on_watch(ev):
    for h in ev.get("history", []):
        if "-> watch" in h["note"]:
            return parse_time(h["time"])
    return None


def alert_text(ev):
    text = event_text(ev)
    since = was_on_watch(ev)
    if since:
        text = text.replace(f"\n{SHORT_FOOTER}", f"\n(Was on Watch since {since:%d %b %H:%M} UTC.)\n{SHORT_FOOTER}")
    return text


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def log_message(kind, title, people):
    path = data_folder(False) / "telegram_log.json"
    log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    log.append({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "title": title[:120], "to": people})
    path.write_text(json.dumps(log[-300:], indent=2), encoding="utf-8")


def deliver(token, subs, text, asset=None, level="alerts", kind="", title="", channel="", silent=False):
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
            call(token, "sendMessage", {"chat_id": channel, "text": text[:4000], "disable_web_page_preview": True,
                                        "disable_notification": silent})
            ok += 1
        except Exception as e:
            print(f"   CHANNEL problem: {e} (is the bot an admin of {channel}?)")
    log_message(kind, title, ok)
    return ok


DAILY_SUMMARY_UTC = (15, 30)          # 21:00 in India


def run_health(now):
    try:
        log = json.loads((data_folder(False) / "run_log.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    day = [r for r in log if hours_between(now, parse_time(r["start"])) <= 24]
    return (len(day), sum(1 for r in day if not r.get("ok"))) if day else None


def market_lines(memory, now, private=False):
    """One line per asset: the three lenses (news, volume, buyers vs sellers)."""
    import market
    mstate = market.load(False)
    out = []
    for asset in ("Bitcoin", "Gold"):
        sc = market.signal_check(memory, mstate, asset, now)
        arrow = lambda v: "↑" if v > 0 else "↓" if v < 0 else "·"
        out.append(f"{ASSET_ICON[asset]} {asset}" + (f": {sc['verdict']}" if private else ""))
        out.append(f"   {arrow(sc['votes']['news'])} News: {sc['news']['text']}")
        out.append(f"   {arrow(sc['votes']['volume'])} Volume: {sc['volume'].get('text', 'no data')}")
        out.append(f"   {arrow(sc['votes']['buy_sell'])} Buyers vs sellers: {sc['buy_sell'].get('text', 'no data')}")
    return out


def daily_text(memory, now):
    """The detailed once-a-day summary: everything found in the last 24 hours with its forecast, the results that came
    in, MINT's fair accuracy so far, the market lenses, and run health."""
    import forecast
    ist = now + timedelta(hours=5, minutes=30)
    found = [e for e in memory["events"] if e.get("state") not in (None, "baseline")
             and hours_between(now, parse_time(e["first_seen"])) <= 24]
    results = [e for e in memory["events"] if (e.get("telegram") or {}).get("verdict_sent")
               and str(e["telegram"]["verdict_sent"])[:2] == "20"
               and hours_between(now, parse_time(e["telegram"]["verdict_sent"])) <= 24]
    lines = [f"🌙 MINT daily summary · {ist:%a %d %b}"]
    if found:
        lines.append(f"\nFound today ({len(found)}):")
        for e in found[:10]:
            icon = "🚨" if e["state"] == "alerted" else STATE_ICON.get(e["state"], "•")
            lines.append(f"{icon} {forecast.headline(e)}\n   {one_liner(e, 110)}")
        if len(found) > 10:
            lines.append(f"…and {len(found) - 10} more (see the website)")
    else:
        lines.append("\nNothing new found today.")
    if results:
        lines.append(f"\nResults today ({len(results)}):")
        lines += [result_line(e, title=False) + f"\n   {one_liner(e, 90)}" for e in results[:8]]
    try:
        acc = forecast.accuracy(memory["events"])
        if acc["hit"] + acc["miss"] + acc["partial"]:
            lz = acc["lazy"]
            lines.append("\nAccuracy so far:")
            lines.append(f"   {acc['hit']} HIT · {acc['partial']} partial · {acc['miss']} miss"
                         + (f" → {acc['score'] * 100:.0f}% right (HIT + partial)" if acc.get("score") is not None else ""))
            if acc["moves"]["total"]:
                lines.append(f"   Counting each price move once: {acc['moves']['hit']} of {acc['moves']['total']} right")
            if lz.get("best") is not None:
                lines.append(f"   A lazy guess (always {lz['best_word']}) would score {lz['best'] * 100:.0f}%")
        mk = acc.get("market") or {}
        if mk.get("total"):
            lines.append(f"   Market-triggered (no news): {mk['total']} so far, {mk['hit']} HIT · {mk['partial']} partial · "
                         f"{mk['miss']} miss" + (f" → {mk['score'] * 100:.0f}% score" if mk.get("score") is not None else
                                                 " (still being judged)"))
    except Exception as e:
        lines.append(f"\n(accuracy not available: {e})")
    try:
        lines.append("\nMarkets now (3 lenses):")
        lines += market_lines(memory, now, private=True)
    except Exception:
        pass
    try:
        states = asset_states(memory, [], now)
        lines.append("\n" + " · ".join(f"{ {'CALM': '🟢', 'WATCH': '👀', 'ALERT': '🚨'}.get(str(states[a]['state']).upper(), '•') } "
                                       f"{a}: {str(states[a]['state']).capitalize()}" for a in ("Bitcoin", "Gold")))
    except Exception:
        pass
    h = run_health(now)
    if h:
        lines.append(f"Runs in the last 24 h: {h[0]}, {h[1]} with errors.")
    return "\n".join(lines) + "\n\n" + FOOTER


def signal_text(memory, now, strong_only=None):
    """The signal check (sent to the channel): the 3 lenses for each asset and what they add up to."""
    import forecast
    ist = now + timedelta(hours=5, minutes=30)
    lines = [f"📡 MINT signal check · {ist:%d %b %H:%M}"] + market_lines(memory, now, private=True)
    try:
        acc = forecast.accuracy(memory["events"])
        if acc.get("score") is not None:
            lines.append(f"\nMINT's record so far: {acc['score'] * 100:.0f}% right ({acc['hit']} HIT + {acc['partial']} partial of "
                         f"{acc['hit'] + acc['miss'] + acc['partial']}). The lenses describe market conditions from data. Not financial advice.")
    except Exception:
        pass
    return "\n".join(lines)


def send_alerts(token, test_mode=False, now=None, calendar_warnings=None, channel=""):
    """Send everything that is due. Prints what it did (shown in the app). Returns how many messages were delivered.
       'Everything' subscribers and the channel get: every new event with MINT's view, every status change,
       the 1-day result, heads-ups and the weekly summary. 'Alerts only' subscribers get alerts + weekly summary."""
    import weekly
    now = now or datetime.now(timezone.utc)
    memory = load_memory(test_mode)
    owner = str(owner_chat() or "")
    subs, state = load_subscribers(), load_state()
    total, waiting = 0, 0
    fresh = lambda t, hours: t and hours_between(now, parse_time(t)) <= hours

    # 0. Once: post "How to read MINT messages" to the channel (pin it in Telegram so new members see it)
    if channel and token and not test_mode and state.get("key_version") != KEY_VERSION:
        if deliver(token, {}, KEY_TEXT, None, "everything", "key", "How to read MINT messages", channel):
            state["key_sent"] = now.isoformat()
            state["key_version"] = KEY_VERSION
            total += 1
            print("   KEY      -> channel: How to read MINT messages (pin it in Telegram)")

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
                tg.setdefault("first_sent_at", datetime.now(timezone.utc).isoformat())   # for the speed check
                total += n
                print(f"   ALERT    -> {n}: {ev['title'][:80]}")
        if test_mode or not token:
            continue
        # 2. EVERY NEW EVENT, with MINT's view (only once, only if it is recent)
        if not tg.get("sent_state") and ev["state"] != "alerted":
            if fresh(ev["first_seen"], WATCH_NOTICE_NEWER_THAN_HOURS):
                n = deliver(token, subs, event_text(ev), ev["asset"], "everything", "event", ev["title"], channel)
                if n:
                    tg.setdefault("first_sent_at", datetime.now(timezone.utc).isoformat())   # for the speed check
                total += n
                print(f"   EVENT    -> {n}: {ev['title'][:80]} ({ev['state']})")
            tg["sent_state"] = ev["state"]
        # 3. STATUS CHANGED since we last told people (e.g. watch -> not confirmed)
        elif tg.get("sent_state") and tg["sent_state"] != ev["state"] and ev["state"] not in ("alerted", "confirmed"):
            n = deliver(token, subs, event_text(ev, update=True), ev["asset"], "everything", "update", ev["title"], channel)
            tg["sent_state"] = ev["state"]
            total += n
            print(f"   UPDATE   -> {n}: {ev['title'][:80]} (now {ev['state']})")
        # 4. RESULT: one short message as soon as the verdict is known (HIT / MISS straight away,
        #    PARTIAL / NO CLEAR MOVE when the time window ends)
        r = (ev.get("outcome") or {}).get("result") or {}
        if r.get("final") and r.get("verdict") in RESULT_WORD and not tg.get("verdict_sent"):
            if not state.get("results_v2") or not fresh(ev["first_seen"], 24 * 8):
                tg["verdict_sent"] = "judged before the new rules - not re-sent"
            else:
                n = deliver(token, subs, result_text(ev), ev["asset"], "everything", "result", ev["title"], channel)
                tg["verdict_sent"] = now.isoformat()
                total += n
                print(f"   RESULT   -> {n}: {ev['title'][:70]} ({r['verdict'].upper()})")

    if token and not test_mode:
        state["results_v2"] = state.get("results_v2") or now.isoformat()   # from now on, results are sent

    if not test_mode and token:
        # 5. HEADS-UP before scheduled gold events, once each
        sent_before = state.setdefault("headsup_sent", [])
        for w in calendar_warnings or []:
            key = f"{now:%Y-%m-%d} {w}"
            if key not in sent_before:
                n = deliver(token, subs, f"📅 MINT heads-up · 🥇 Gold\n{w}\n\n{FOOTER}", "Gold", "everything", "heads-up", w, channel)
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

    # 7. DAILY SUMMARY: ONE message a day at 21:00 India time (15:30 UTC). Nothing is posted just to say "run complete".
    #    Anything MINT finds is still sent straight away (steps 1-6); this only wraps up the day.
    today = f"{now:%Y-%m-%d}"
    if channel and token and not test_mode and state.get("daily_done") != today \
            and (now.hour, now.minute) >= DAILY_SUMMARY_UTC:
        text = daily_text(memory, now)
        if deliver(token, {}, text, None, "everything", "daily summary", today, channel):
            total += 1
            print("   DAILY    -> channel: summary for " + today)
        state["daily_done"] = today

    # 8. SIGNAL CHECK to the channel straight away when 2 or 3 lenses agree and nothing disagrees (at most every 6 hours
    #    per asset, and only when the picture changes). The 9 pm summary already carries the verdict lines.
    if channel and token and not test_mode:
        try:
            import market
            mstate = market.load(False)
            for asset in ("Bitcoin", "Gold"):
                sc = market.signal_check(memory, mstate, asset, now)
                strong = "up" if sc["ups"] >= 2 and not sc["downs"] else "down" if sc["downs"] >= 2 and not sc["ups"] else None
                key = f"signal_{asset}"
                last = state.get(key) or {}
                if strong and (last.get("dir") != strong or not fresh(last.get("time"), 6)):
                    if deliver(token, {}, signal_text(memory, now), None, "everything", "signal check", asset, channel):
                        total += 1
                        print(f"   SIGNAL   -> channel: {asset} {strong} ({sc['verdict']})")
                    state[key] = {"dir": strong, "time": now.isoformat()}
                elif not strong and last.get("dir"):
                    state[key] = {"dir": None, "time": now.isoformat()}
        except Exception as e:
            print(f"   SIGNAL problem: {e}")

    save_memory(memory, test_mode)
    save_subscribers(subs)
    if not test_mode:
        save_state(state)
    print(f"\nBOTTOM LINE: {total} message(s) delivered, {len(subs)} subscriber(s)"
          + (", posting to the channel" if channel else "")
          + (f", {waiting} alert(s) waiting for the bot to be set up" if waiting else "")
          + (" (test run: nothing is sent)" if test_mode else "") + ".")
    return total


def owner_chat():
    """Your own Telegram chat ID (GitHub secret TELEGRAM_OWNER, or "telegram_owner" in settings). Send /id to the bot
    to find it."""
    import decision
    return decision.load_settings().get("telegram_owner") or ""


def send_private(token, chat, text):
    try:
        call(token, "sendMessage", {"chat_id": int(chat) if str(chat).lstrip("-").isdigit() else chat,
                                    "text": text[:4000], "disable_web_page_preview": True})
        log_message("private signal check", "signal check", 1)
        return True
    except Exception as e:
        print(f"   PRIVATE message problem: {e}")
        return False


def send_test(token):
    subs = load_subscribers()
    for chat in subs:
        call(token, "sendMessage", {"chat_id": int(chat), "text": "MINT test message: the bot is working. "
                                    "Real alerts will look like this, but with the event."})
    return len(subs)
