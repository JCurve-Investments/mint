"""
MINT Market lenses: trading volume and buy vs sell pressure
===========================================================
MINT's first lens is the NEWS (events, factors and what happened after similar past events). This file adds two
more lenses, so a prediction is not based on the headline alone:

  VOLUME      how much is being traded in the last 24 hours compared with a normal day (the last 20 days).
              Volume on its own has no direction. A volume spike while the price FALLS means real selling,
              a spike while it RISES means real buying, low volume means the market is quiet.
  BUY / SELL  who is more eager: buyers or sellers.
              Bitcoin: Kraken lists every trade with the side that started it (a buyer taking a seller's price,
                       or the other way), so MINT adds up "taker" buys and sells for the last 24 hours. Kraken's
                       order book shows the waiting buy orders against the waiting sell orders near the price.
              Gold:    there is no free, real-time list of gold trades. MINT uses two stand-ins:
                       - hourly gold futures bars (Yahoo Finance, GC=F): volume in hours when the price rose
                         against volume in hours when it fell ("up-volume vs down-volume"),
                       - the CFTC Commitments of Traders report (weekly, free): are big speculators adding to or
                         cutting their bets that gold will rise?

Each lens VOTES: up, down or neutral (no clear signal / no data). Votes change how sure MINT is about a
prediction (more lenses agreeing = more confident), and they make the private "signal check".

Free data only, no keys, no accounts. If a website fails, that lens says "no data" and MINT carries on.
Results are saved in data/market.json (shown on the Dashboard and in the 9 pm summary).

Run on its own:  py market.py
"""

import json
import math
import statistics
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

from common import fetch_page
from supervisor import data_folder, parse_time, hours_between

KRAKEN = "https://api.kraken.com/0/public"
COINBASE = "https://api.exchange.coinbase.com"
YAHOO_BARS = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1mo&interval=1h"
CFTC = ("https://publicreporting.cftc.gov/resource/6dca-aqww.json?"
        + urllib.parse.urlencode({"$where": "cftc_contract_market_code='088691'",
                                  "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "6"}))
BARS_REFRESH_MINUTES = 50          # hourly bars: download about once an hour
COT_REFRESH_HOURS = 12             # the CFTC report is weekly: check twice a day
KEEP_FLOW_HOURS = 24 * 30

# When does a lens count as a vote?
VOLUME_HIGH = 1.25                 # 24-hour volume at least 1.25x a normal day = "busy"
VOLUME_LOW = 0.8                   # under 0.8x = "quiet"
TAKER_EDGE = 0.53                  # buyers start 53% or more of the volume = buyers in control (47% or less: sellers)
BOOK_EDGE = 0.58                   # 58% or more of the waiting orders near the price are buy orders
BAR_EDGE = 0.56                    # up-hours volume share (gold futures / Bitcoin hourly bars)
MIN_FLOW_HOURS = 6                 # need at least 6 hours of trades before the taker share counts


def market_path(test_mode):
    return data_folder(test_mode) / "market.json"


def load(test_mode=False):
    p = market_path(test_mode)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save(state, test_mode=False):
    market_path(test_mode).write_text(json.dumps(state, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
def get_json(url):
    return json.loads(fetch_page(url))


def yahoo_bars(symbol):
    """Hourly bars for the last month: list of {t, open, close, volume} (t in seconds)."""
    r = get_json(YAHOO_BARS.format(urllib.parse.quote(symbol)))["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    bars = []
    for t, o, c, v in zip(r["timestamp"], q.get("open", []), q.get("close", []), q.get("volume", [])):
        if None not in (o, c, v):
            bars.append({"t": int(t), "open": o, "close": c, "volume": v})
    if len(bars) < 48:
        raise RuntimeError(f"only {len(bars)} hourly bars")
    return bars


def kraken_bars():
    """Kraken hourly Bitcoin bars (about 30 days): {t, open, close, volume}."""
    r = get_json(f"{KRAKEN}/OHLC?pair=XBTUSD&interval=60")
    if r.get("error"):
        raise RuntimeError("Kraken: " + "; ".join(r["error"]))
    rows = next(v for k, v in r["result"].items() if k != "last")
    return [{"t": int(x[0]), "open": float(x[1]), "close": float(x[4]), "volume": float(x[6])} for x in rows]


def kraken_trades(since=None):
    """Trades after 'since' (Kraken's cursor). Returns (list of (time, volume, 'b' or 's'), next cursor)."""
    url = f"{KRAKEN}/Trades?pair=XBTUSD&count=1000" + (f"&since={since}" if since else "")
    r = get_json(url)
    if r.get("error"):
        raise RuntimeError("Kraken: " + "; ".join(r["error"]))
    key = next(k for k in r["result"] if k != "last")
    trades = [(float(x[2]), float(x[1]), x[3]) for x in r["result"][key]]
    return trades, r["result"]["last"]


def coinbase_trades():
    """Backup: Coinbase's last 1000 trades. Its 'side' is the side of the WAITING order, so a 'sell' there means a
    buyer took the price (a taker buy)."""
    rows = get_json(f"{COINBASE}/products/BTC-USD/trades?limit=1000")
    out = []
    for x in rows:
        t = datetime.fromisoformat(x["time"].replace("Z", "+00:00")).timestamp()
        out.append((t, float(x["size"]), "b" if x["side"] == "sell" else "s"))
    return out


def kraken_book():
    """Share of waiting BUY orders within 1% of the middle price (0 to 1)."""
    r = get_json(f"{KRAKEN}/Depth?pair=XBTUSD&count=500")
    if r.get("error"):
        raise RuntimeError("Kraken: " + "; ".join(r["error"]))
    book = next(iter(r["result"].values()))
    bids = [(float(p), float(v)) for p, v, *_ in book["bids"]]
    asks = [(float(p), float(v)) for p, v, *_ in book["asks"]]
    mid = (bids[0][0] + asks[0][0]) / 2
    b = sum(v for p, v in bids if p >= mid * 0.99)
    a = sum(v for p, v in asks if p <= mid * 1.01)
    return b / (a + b) if a + b else None


def cftc_gold():
    """The last few weekly CFTC reports for gold futures: big speculators' net position (bets on a rise minus bets
    on a fall), newest first."""
    rows = get_json(CFTC)
    out = []
    for r in rows:
        try:
            out.append({"date": r["report_date_as_yyyy_mm_dd"][:10],
                        "net": int(float(r["noncomm_positions_long_all"])) - int(float(r["noncomm_positions_short_all"])),
                        "open_interest": int(float(r.get("open_interest_all", 0) or 0))})
        except (KeyError, ValueError):
            continue
    if len(out) < 2:
        raise RuntimeError("fewer than 2 weekly reports")
    return out


# ---------------------------------------------------------------------------
# Turning data into votes
# ---------------------------------------------------------------------------
def sign_vote(value, up_edge, down_edge):
    if value is None:
        return 0
    return 1 if value >= up_edge else -1 if value <= down_edge else 0


def volume_lens(bars, now_ts):
    """24-hour volume against a normal day, and the price change over those 24 hours."""
    day = [b for b in bars if b["t"] > now_ts - 86400]
    before = [b for b in bars if now_ts - 86400 * 21 < b["t"] <= now_ts - 86400]
    if len(day) < 12 or len(before) < 24 * 5:
        return {"vote": 0, "text": "not enough volume data yet", "ok": False}
    v_day = sum(b["volume"] for b in day) * 24 / len(day)        # scaled to a full day if hours are missing
    normal = sum(b["volume"] for b in before) * 24 / len(before)
    ratio = v_day / normal if normal else None
    change = (day[-1]["close"] - day[0]["open"]) / day[0]["open"] * 100
    vote = 0
    if ratio and ratio >= VOLUME_HIGH and abs(change) >= 0.2:
        vote = 1 if change > 0 else -1
    if ratio is None:
        word = "no volume data"
    elif ratio >= VOLUME_HIGH:
        word = f"busy: {ratio:.1f}x a normal day, price {change:+.1f}% - " + \
               ("real buying" if vote > 0 else "real selling" if vote < 0 else "no clear direction")
    elif ratio <= VOLUME_LOW:
        word = f"quiet: {ratio:.1f}x a normal day (moves are less reliable)"
    else:
        word = f"normal: {ratio:.1f}x a normal day, price {change:+.1f}%"
    return {"vote": vote, "ratio": round(ratio, 2) if ratio else None, "change_24h": round(change, 2), "text": word, "ok": True}


def bar_pressure(bars, now_ts, hours=24):
    """Volume in hours when the price rose / (rose + fell). Above 0.5 = more buying pressure."""
    day = [b for b in bars if b["t"] > now_ts - hours * 3600]
    up = sum(b["volume"] for b in day if b["close"] > b["open"])
    down = sum(b["volume"] for b in day if b["close"] < b["open"])
    return round(up / (up + down), 3) if up + down else None


def flow_share(flow, now_ts, hours=24):
    """Taker buy share from the saved hourly buckets. Returns (share, hours covered)."""
    keys = [k for k in flow if int(k) > now_ts - hours * 3600]
    buy = sum(flow[k]["b"] for k in keys)
    sell = sum(flow[k]["s"] for k in keys)
    return (round(buy / (buy + sell), 3) if buy + sell else None), len(keys)


def combine(parts):
    """Several small signals -> one vote. They must not point opposite ways."""
    votes = [v for v in parts if v]
    if not votes:
        return 0
    if all(v > 0 for v in votes):
        return 1
    if all(v < 0 for v in votes):
        return -1
    return 0


# ---------------------------------------------------------------------------
# One refresh (called by the Price Tracker every run)
# ---------------------------------------------------------------------------
def refresh(now=None, test_mode=False):
    now = now or datetime.now(timezone.utc)
    state = load(test_mode)
    if test_mode:
        state = fake_state(now)
        save(state, True)
        return state
    now_ts = now.timestamp()
    errors = []

    # ---- Bitcoin ----
    btc = state.setdefault("Bitcoin", {})
    if not btc.get("bars_time") or hours_between(now, parse_time(btc["bars_time"])) * 60 >= BARS_REFRESH_MINUTES:
        try:
            btc["bars"] = kraken_bars()[-24 * 22:]
            btc["bars_time"], btc["bars_source"] = now.isoformat(), "Kraken"
        except Exception as e:
            errors.append(f"Bitcoin bars (Kraken): {e}")
            try:
                btc["bars"] = yahoo_bars("BTC-USD")[-24 * 22:]
                btc["bars_time"], btc["bars_source"] = now.isoformat(), "Yahoo Finance (backup)"
            except Exception as e2:
                errors.append(f"Bitcoin bars (Yahoo): {e2}")
    flow = btc.setdefault("flow", {})
    try:
        trades, cursor = kraken_trades(btc.get("trades_cursor"))
        btc["trades_cursor"], btc["flow_source"] = cursor, "Kraken"
    except Exception as e:
        errors.append(f"Bitcoin trades (Kraken): {e}")
        try:
            trades = coinbase_trades()
            last = btc.get("coinbase_last", 0)
            trades = [x for x in trades if x[0] > last]
            if trades:
                btc["coinbase_last"] = max(x[0] for x in trades)
            btc["flow_source"] = "Coinbase (backup)"
        except Exception as e2:
            trades = []
            errors.append(f"Bitcoin trades (Coinbase): {e2}")
    for t, vol, side in trades:
        hour = str(int(t // 3600 * 3600))
        bucket = flow.setdefault(hour, {"b": 0.0, "s": 0.0})
        bucket["b" if side == "b" else "s"] += vol
    for k in [k for k in flow if int(k) < now_ts - KEEP_FLOW_HOURS * 3600]:
        flow.pop(k)
    for k in flow:
        flow[k] = {"b": round(flow[k]["b"], 4), "s": round(flow[k]["s"], 4)}
    try:
        btc["book_bid_share"] = round(kraken_book(), 3)
        btc["book_time"] = now.isoformat()
    except Exception as e:
        errors.append(f"Bitcoin order book (Kraken): {e}")

    # ---- Gold ----
    gold = state.setdefault("Gold", {})
    if not gold.get("bars_time") or hours_between(now, parse_time(gold["bars_time"])) * 60 >= BARS_REFRESH_MINUTES:
        try:
            gold["bars"] = yahoo_bars("GC=F")[-24 * 22:]
            gold["bars_time"], gold["bars_source"] = now.isoformat(), "Yahoo Finance (gold futures GC=F)"
        except Exception as e:
            errors.append(f"Gold bars (Yahoo): {e}")
    if not gold.get("cot_time") or hours_between(now, parse_time(gold["cot_time"])) >= COT_REFRESH_HOURS:
        try:
            gold["cot"] = cftc_gold()
            gold["cot_time"] = now.isoformat()
        except Exception as e:
            errors.append(f"Gold CFTC report: {e}")

    state["time"] = now.isoformat()
    state["errors"] = errors
    state["lenses"] = lenses(state, now)
    save(state, test_mode)
    return state


def lenses(state, now):
    """The volume and buy/sell votes for both assets, in plain words."""
    now_ts = now.timestamp()
    out = {}
    # Bitcoin
    btc = state.get("Bitcoin", {})
    bars = btc.get("bars") or []
    vol = volume_lens(bars, now_ts) if bars else {"vote": 0, "text": "no volume data", "ok": False}
    share, hours = flow_share(btc.get("flow", {}), now_ts)
    taker = share if hours >= MIN_FLOW_HOURS else None
    book = btc.get("book_bid_share")
    if book is not None and btc.get("book_time") and hours_between(now, parse_time(btc["book_time"])) > 1:
        book = None
    bars_p = bar_pressure(bars, now_ts) if bars else None
    parts = [sign_vote(taker, TAKER_EDGE, 1 - TAKER_EDGE), sign_vote(book, BOOK_EDGE, 1 - BOOK_EDGE)]
    if taker is None:
        parts.append(sign_vote(bars_p, BAR_EDGE, 1 - BAR_EDGE))
    words = []
    if taker is not None:
        words.append(f"buyers started {taker * 100:.0f}% of trades (24 h)")
    elif share is not None:
        words.append(f"trade data building up ({hours} of {MIN_FLOW_HOURS} hours)")
    if book is not None:
        words.append(f"{book * 100:.0f}% of waiting orders near the price are buy orders")
    if taker is None and bars_p is not None:
        words.append(f"{bars_p * 100:.0f}% of volume in rising hours")
    out["Bitcoin"] = {"volume": vol,
                      "buy_sell": {"vote": combine(parts), "taker_buy_share": taker, "book_bid_share": book,
                                   "up_volume_share": bars_p, "text": "; ".join(words) or "no buy/sell data",
                                   "ok": bool(words)}}
    # Gold
    gold = state.get("Gold", {})
    gbars = gold.get("bars") or []
    gvol = volume_lens(gbars, now_ts) if gbars else {"vote": 0, "text": "no volume data", "ok": False}
    gp = bar_pressure(gbars, now_ts) if gbars else None
    cot = gold.get("cot") or []
    cot_vote, cot_text = 0, None
    if len(cot) >= 2:
        change = cot[0]["net"] - cot[1]["net"]
        base = max(abs(cot[1]["net"]), 1)
        if abs(change) / base >= 0.05:
            cot_vote = 1 if change > 0 else -1
        cot_text = (f"big speculators {'added' if change > 0 else 'cut'} {abs(change):,} contracts of bets on a rise "
                    f"(week to {cot[0]['date']})")
    gparts = [sign_vote(gp, BAR_EDGE, 1 - BAR_EDGE), cot_vote]
    gwords = ([f"{gp * 100:.0f}% of futures volume in rising hours (24 h)"] if gp is not None else []) + \
             ([cot_text] if cot_text else [])
    out["Gold"] = {"volume": gvol,
                   "buy_sell": {"vote": combine(gparts), "up_volume_share": gp, "cot_vote": cot_vote,
                                "text": "; ".join(gwords) or "no buy/sell data", "ok": bool(gwords)}}
    return out


def fake_state(now):
    """TEST mode: fixed made-up readings (no internet)."""
    return {"time": now.isoformat(), "errors": [], "test": True,
            "lenses": {"Bitcoin": {"volume": {"vote": -1, "ratio": 1.6, "change_24h": -2.1, "ok": True,
                                              "text": "busy: 1.6x a normal day, price -2.1% - real selling"},
                                   "buy_sell": {"vote": -1, "taker_buy_share": 0.45, "book_bid_share": 0.4, "ok": True,
                                                "text": "buyers started 45% of trades (24 h); 40% of waiting orders near the price are buy orders"}},
                       "Gold": {"volume": {"vote": 0, "ratio": 1.0, "change_24h": 0.1, "ok": True,
                                           "text": "normal: 1.0x a normal day, price +0.1%"},
                                "buy_sell": {"vote": 0, "up_volume_share": 0.5, "cot_vote": 0, "ok": True,
                                             "text": "50% of futures volume in rising hours (24 h)"}}}}


def news_lens(memory, asset, now):
    """The third lens: the news. Each event from the last 24 hours with a prediction votes, weighted by how likely
    MINT thinks it is. Returns a vote like the other lenses."""
    score, n = 0.0, 0
    for ev in memory.get("events", []):
        if ev.get("asset") != asset or ev.get("state") in (None, "rejected", "baseline"):
            continue
        if hours_between(now, parse_time(ev["first_seen"])) > 24:
            continue
        p = ev.get("prediction") or {}
        f = p.get("forecast") or {}
        if p.get("direction") not in ("up", "down"):
            continue
        w = max(f.get("probability", 0.5) - 0.5, 0.02)
        score += w if p["direction"] == "up" else -w
        n += 1
    vote = 1 if score > 0.1 else -1 if score < -0.1 else 0
    text = f"{n} event(s) in 24 h" + ("" if not n else f", leaning {'up' if score > 0 else 'down' if score < 0 else 'nowhere'}")
    return {"vote": vote, "events": n, "text": text, "ok": True}


def signal_check(memory, state, asset, now):
    """The private signal check: the 3 lenses side by side. Only says how conditions look, from the data."""
    L = (state.get("lenses") or {}).get(asset) or {}
    news = news_lens(memory, asset, now)
    votes = {"news": news["vote"], "volume": (L.get("volume") or {}).get("vote", 0),
             "buy_sell": (L.get("buy_sell") or {}).get("vote", 0)}
    ups = sum(1 for v in votes.values() if v > 0)
    downs = sum(1 for v in votes.values() if v < 0)
    if ups >= 2 and not downs:
        verdict = f"{ups} of 3 lenses point UP - conditions look favourable for a rise"
    elif downs >= 2 and not ups:
        verdict = f"{downs} of 3 lenses point DOWN - conditions look unfavourable (risk of a fall)"
    elif ups and downs:
        verdict = "the lenses disagree - no clear picture"
    else:
        verdict = "no strong signal right now"
    return {"votes": votes, "ups": ups, "downs": downs, "verdict": verdict, "news": news,
            "volume": L.get("volume") or {}, "buy_sell": L.get("buy_sell") or {}}


# ---------------------------------------------------------------------------
# Market-triggered events: the lenses can start an event on their own (no news needed)
# ---------------------------------------------------------------------------
TRIGGER_VOLUME_RATIO = 1.5         # 24-hour volume at least 1.5x a normal day
TRIGGER_COOLDOWN_HOURS = 12        # at most one market-triggered event per asset every 12 hours
NEWS_COVERS_HOURS = 6              # skip it if a news event pointing the same way was found in the last 6 hours


def trigger_events(memory, mstate, now):
    """Start an event when the volume lens AND the buyers-vs-sellers lens both point the same way and trading is
    clearly busy (1.5x a normal day), unless the news already covers it or the news points the other way.
    The event has a forecast and is judged HIT / MISS like any other, but is counted on its own accuracy line.
    Returns the new events."""
    from supervisor import new_event_id
    made = []
    for asset in ("Bitcoin", "Gold"):
        L = (mstate.get("lenses") or {}).get(asset) or {}
        vol, bs = L.get("volume") or {}, L.get("buy_sell") or {}
        if not (vol.get("ok") and bs.get("ok")):
            continue
        direction = vol.get("vote", 0)
        if not direction or bs.get("vote", 0) != direction or (vol.get("ratio") or 0) < TRIGGER_VOLUME_RATIO:
            continue
        word = "up" if direction > 0 else "down"
        news = news_lens(memory, asset, now)
        if news["vote"] == -direction:
            continue                                       # the news points the other way: no clear picture
        recent = [e for e in memory.get("events", []) if e.get("asset") == asset and e.get("state") not in (None, "baseline")]
        if any(e.get("market_triggered") and hours_between(now, parse_time(e["first_seen"])) < TRIGGER_COOLDOWN_HOURS
               for e in recent):
            continue
        if any((e.get("prediction") or {}).get("direction") == word and not e.get("market_triggered")
               and hours_between(now, parse_time(e["first_seen"])) < NEWS_COVERS_HOURS for e in recent):
            continue                                       # a news event already covers this move
        side = "buyers" if direction > 0 else "sellers"
        ev = {
            "id": new_event_id(memory, asset, now), "asset": asset, "factor": "market",
            "factor_name": "Market move (volume + buyers vs sellers)", "market_triggered": True,
            "title": f"{asset} market move: busy trading ({vol.get('ratio'):.1f}x a normal day) with {side} in control",
            "state": "context", "state_reason": "started by the market lenses, no news report", "proof": "market",
            "proof_reason": "volume and buy/sell data, not a news report",
            "first_seen": now.isoformat(), "earliest_report": now.isoformat(), "last_updated": now.isoformat(),
            "words": [], "amounts": [], "old_clues": [], "sources": [], "decision": None, "outcome": {},
            "history": [{"time": now.isoformat(), "note": f"new market-triggered event ({word}): {vol.get('text')}; {bs.get('text')}"}],
            "prediction": {"direction": word, "by": "market lenses", "similar": [], "votes": {},
                           "reason": f"Volume: {vol.get('text')}. Buyers vs sellers: {bs.get('text')}."},
        }
        memory["events"].append(ev)
        made.append(ev)
    return made


if __name__ == "__main__":
    s = refresh(test_mode="--test" in sys.argv)
    for a, L in s.get("lenses", {}).items():
        print(f"{a}:\n   Volume:   {L['volume']['text']}\n   Buy/sell: {L['buy_sell']['text']}")
    for e in s.get("errors", []):
        print("   problem:", e)
