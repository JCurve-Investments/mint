"""
MINT past events library
========================
MINT predicts the direction of a new event mainly from WHAT HAPPENED AFTER SIMILAR PAST EVENTS.

The library has two kinds of past events:
  1. MINT's own events - every event MINT has tracked, once its 1-day price is known (added automatically).
  2. Researched events - real past events YOU add in the app (History > Past events library): a date, the asset,
     the factor and a headline. MINT then downloads the REAL prices for that date (Yahoo Finance daily prices)
     and works out the move - no numbers are typed in by hand.
     How the moves are measured for researched events (daily prices only):
        start  = closing price on the last trading day BEFORE the event date
        1 day  = closing price on the event date (or the next trading day)
        7 days = closing price 7 days after the event date

How a prediction is made for a new event:
  a. Find similar past events: same asset, similar headline words, the same factor counts extra, and the same KIND
     of event counts extra (a past "Fed cuts rates" is never used for a new "Fed will NOT cut").
     Only events from BEFORE the new event are used - MINT never peeks into the future.
  b. Each similar past event "votes" for the direction the price actually moved after 1 day.
     More similar events count more.
  c. The factor's known pattern (e.g. "dollar up, gold down") also votes, when the headline says which way.
  d. Whichever direction gets more votes is the prediction. The reason lists the evidence.
  If there are no similar past events and the factor pattern cannot tell, the prediction is "unclear"
  (with AI switched on, the AI is asked as a last resort).
"""

import json
from datetime import date, datetime, timedelta, timezone

from common import FOLDER, fetch_page
from predict import predict
from supervisor import data_folder, key_words, similarity

YAHOO = {"Bitcoin": "BTC-USD", "Gold": "GC=F"}
MIN_SCORE = 0.45            # how similar a past event must be to count
SAME_FACTOR_BONUS = 0.25
RULE_WEIGHT = 1.5           # the factor pattern counts like one or two very similar past events
TOP = 6

# Ideas for real past events to research and add (dates to double-check yourself before adding).
SUGGESTIONS = [
    {"date": "2022-11-08", "asset": "Bitcoin", "factor": "B2", "title": "FTX halts customer withdrawals as crisis deepens"},
    {"date": "2025-02-21", "asset": "Bitcoin", "factor": "B2", "title": "Crypto exchange Bybit hacked for $1.5bn"},
    {"date": "2023-06-05", "asset": "Bitcoin", "factor": "B1", "title": "SEC sues Binance and founder Changpeng Zhao"},
    {"date": "2023-06-06", "asset": "Bitcoin", "factor": "B1", "title": "SEC sues Coinbase for operating unregistered exchange"},
    {"date": "2021-09-24", "asset": "Bitcoin", "factor": "B1", "title": "China's central bank bans all crypto transactions"},
    {"date": "2024-01-10", "asset": "Bitcoin", "factor": "B1", "title": "SEC approves first spot Bitcoin ETFs"},
    {"date": "2022-06-15", "asset": "Gold", "factor": "G2", "title": "Fed raises rates by 0.75 point, biggest hike since 1994"},
    {"date": "2024-09-18", "asset": "Gold", "factor": "G2", "title": "Fed cuts rates by half a point"},
    {"date": "2024-12-18", "asset": "Gold", "factor": "G2", "title": "Fed cuts rates but signals fewer cuts next year"},
    {"date": "2022-09-13", "asset": "Gold", "factor": "G3", "title": "US inflation hotter than expected"},
    {"date": "2024-11-06", "asset": "Gold", "factor": "G1", "title": "Dollar jumps after US election result"},
]


# ---------------------------------------------------------------------------
# Reading and saving the library
# ---------------------------------------------------------------------------
def researched_path(test_mode):
    return data_folder(test_mode) / "past_events.json"


def load_researched(test_mode):
    entries = []
    if test_mode:
        sample = FOLDER / "test_data" / "sample_past_events.json"
        if sample.exists():
            entries += json.loads(sample.read_text(encoding="utf-8"))
    path = researched_path(test_mode)
    if path.exists():
        entries += json.loads(path.read_text(encoding="utf-8"))
    return entries


def own_entries(memory):
    """MINT's own events whose 1-day price is known."""
    out = []
    for ev in memory.get("events", []):
        o = ev.get("outcome") or {}
        if (o.get("d1") or {}).get("move") is None:
            continue
        out.append({"id": ev["id"], "date": ev["earliest_report"][:10], "asset": ev["asset"], "factor": ev["factor"],
                    "title": ev["title"], "d1": o["d1"]["move"], "d7": (o.get("d7") or {}).get("move"),
                    "source": "MINT"})
    return out


def all_entries(test_mode, memory):
    return load_researched(test_mode) + own_entries(memory)


# ---------------------------------------------------------------------------
# Adding a researched past event (real prices from Yahoo Finance)
# ---------------------------------------------------------------------------
def daily_closes(asset, day):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(days=12)
    end = start + timedelta(days=12 + 14)
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO[asset]}?interval=1d"
           f"&period1={int(start.timestamp())}&period2={int(end.timestamp())}")
    result = json.loads(fetch_page(url))["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, tz=timezone.utc).date(), c)
            for t, c in zip(result["timestamp"], closes) if c is not None]


def measure(asset, day):
    """The real price moves around a past date: (start price, 1-day move %, 7-day move %)."""
    closes = daily_closes(asset, day)
    before = [c for d, c in closes if d < day]
    on_or_after = [c for d, c in closes if d >= day]
    week = [c for d, c in closes if d >= day + timedelta(days=7)]
    if not before or not on_or_after:
        raise RuntimeError(f"no {asset} prices found around {day}")
    start = before[-1]
    d1 = round((on_or_after[0] - start) / start * 100, 2)
    d7 = round((week[0] - start) / start * 100, 2) if week else None
    return round(start, 2), d1, d7


def add_researched(test_mode, day_text, asset, factor, title):
    day = date.fromisoformat(day_text)
    if day >= date.today():
        raise RuntimeError("The date must be in the past.")
    if asset not in YAHOO:
        raise RuntimeError("Asset must be Gold or Bitcoin.")
    title = title.strip()
    if len(title) < 8:
        raise RuntimeError("Please write a headline (what happened).")
    start, d1, d7 = measure(asset, day)
    path = researched_path(test_mode)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entry = {"id": f"PAST-{day:%Y%m%d}-{len(entries) + 1:02d}", "date": day.isoformat(), "asset": asset,
             "factor": factor, "title": title, "start_price": start, "d1": d1, "d7": d7,
             "source": "researched", "added": datetime.now(timezone.utc).isoformat()}
    entries.append(entry)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return entry


def starter_events():
    path = FOLDER / "starter_library.json"
    return json.loads(path.read_text(encoding="utf-8"))["events"] if path.exists() else []


def fill_starter(test_mode, max_new=12):
    """Load the starter library little by little: download real prices for up to max_new starter events that are
    not in the library yet (so one run never makes too many requests). Returns (added, still_to_load, errors)."""
    if test_mode:
        return 0, 0, []
    import time
    path = researched_path(False)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    have = {(e["date"], e["asset"], e["title"]) for e in entries}
    todo = [s for s in starter_events() if (s["date"], s["asset"], s["title"]) not in have]
    added, errors = 0, []
    for s in todo[:max_new]:
        try:
            start, d1, d7 = measure(s["asset"], date.fromisoformat(s["date"]))
            entries.append({"id": f"START-{s['date'].replace('-', '')}-{s['asset'][0]}", "date": s["date"],
                            "asset": s["asset"], "factor": s["factor"], "title": s["title"], "start_price": start,
                            "d1": d1, "d7": d7, "source": "starter library (check the date)",
                            "added": datetime.now(timezone.utc).isoformat()})
            added += 1
            time.sleep(0.5)
        except Exception as e:
            errors.append(f"{s['date']} {s['title'][:40]}: {e}")
            break                      # probably a network problem: try again next run
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return added, max(0, len(todo) - added), errors


def delete_researched(test_mode, entry_id):
    path = researched_path(test_mode)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.write_text(json.dumps([e for e in entries if e["id"] != entry_id], indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Predicting from similar past events
# ---------------------------------------------------------------------------
def find_similar(asset, factor, title, entries, exclude_id=None, before=None):
    words = key_words(title)
    cause = predict(factor, title, asset)["direction"]     # what KIND of event: e.g. rate cut (up) vs no cut (down)
    found = []
    for e in entries:
        if e["asset"] != asset or e.get("id") == exclude_id or e.get("d1") is None:
            continue
        if before and e["date"] >= before:          # only events from BEFORE this one
            continue
        score, shared = similarity(words, key_words(e["title"]))
        if e["factor"] == factor:
            score += SAME_FACTOR_BONUS
        past_cause = predict(e["factor"], e["title"], asset)["direction"]
        if cause != "unclear" and past_cause != "unclear":
            if past_cause != cause:
                continue            # opposite kind of event ("Fed cuts" is not like "Fed will NOT cut")
            score += 0.2            # same kind of event
        if score >= MIN_SCORE and (shared >= 1 or e["factor"] == factor):
            found.append({**e, "score": round(score, 2)})
    found.sort(key=lambda e: (-e["score"], e["date"]))
    return found[:TOP]


def make_prediction(factor, title, asset, entries, exclude_id=None, before=None):
    rule = predict(factor, title, asset)
    similar = find_similar(asset, factor, title, entries, exclude_id, before)
    ups = [e for e in similar if e["d1"] >= 0.1]
    downs = [e for e in similar if e["d1"] <= -0.1]
    vote_up = sum(e["score"] for e in ups) + (RULE_WEIGHT if rule["direction"] == "up" else 0)
    vote_down = sum(e["score"] for e in downs) + (RULE_WEIGHT if rule["direction"] == "down" else 0)
    word = asset.lower()

    parts = []
    if similar:
        avg = sum(e["d1"] for e in similar) / len(similar)
        parts.append(f"{len(similar)} similar past event(s): {word} rose after {len(ups)} and fell after {len(downs)} "
                     f"(average 1-day move {avg:+.1f}%)")
    else:
        parts.append("no similar past events in the library yet")
    if rule["direction"] in ("up", "down"):
        parts.append(f"factor pattern: {rule['reason']}")

    if vote_up == vote_down:
        direction = "unclear"
        if not similar and rule["direction"] == "unclear":
            parts = [f"{rule['reason']}, and there are no similar past events in the library yet"]
        else:
            parts.append("the evidence is evenly split")
    else:
        direction = "up" if vote_up > vote_down else "down"
        if rule["direction"] in ("up", "down") and rule["direction"] != direction:
            parts.append(f"the past events outweigh the factor pattern, so MINT predicts {direction}")
    by = ("past events + factor pattern" if similar and rule["direction"] != "unclear" else
          "past events" if similar else "factor pattern")
    return {"direction": direction, "by": by, "reason": "; ".join(parts)[:400],
            "similar": [{k: e.get(k) for k in ("id", "date", "title", "d1", "d7", "source", "score")} for e in similar[:5]],
            "votes": {"up": round(vote_up, 2), "down": round(vote_down, 2)}}
