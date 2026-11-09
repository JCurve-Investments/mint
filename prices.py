"""
MINT Price Tracker
==================
Two jobs:

  1. LIVE PRICE - every run, saves the current Bitcoin and gold price (for the app's cards and chart).
  2. OUTCOMES   - for every event MINT kept, it looks up the price:
                     at the first report       (the starting point)
                     when MINT noticed it      (how much the price had ALREADY moved - shows lateness)
                     1 hour, 1 day and 7 days after the first report
                  and then says whether the event "moved the price" or had "no clear effect".

Where the prices come from:
  CoinGecko's free public API (no account, no key; about 5 requests a minute is allowed).
    - Bitcoin: "bitcoin"
    - Gold:    "pax-gold" (PAXG). Each PAXG token is backed by one ounce of real gold, so its price
               follows the gold price very closely. CoinGecko has no direct gold price, so this is
               the free stand-in. Say so in your report.
  If CoinGecko fails, Yahoo Finance is tried as a backup (BTC-USD and GC=F gold futures).
  CoinGecko asks for credit when its data is shown: the app shows "Prices: CoinGecko".

Important: a price move after an event does NOT prove the event caused it (other news happens too).
MINT reports it as "moved the price" or "no clear effect" - a pattern to check, not proof.

Run on its own:  py prices.py
"""

import json
import math
import sys
from datetime import datetime, timezone, timedelta

from common import fetch_page, load_json
from supervisor import data_folder, load_memory, save_memory, parse_time, hours_between

COINGECKO_IDS = {"Bitcoin": "bitcoin", "Gold": "pax-gold"}
YAHOO_SYMBOLS = {"Bitcoin": "BTC-USD", "Gold": "GC=F"}
HISTORY_REFRESH_MINUTES = 50            # download the hourly history at most about once an hour
CHECKPOINTS = {"h1": 1, "d1": 24, "d7": 168}   # hours after the first report
BIG_MOVE_PERCENT = {"Bitcoin": 1.5, "Gold": 0.5}   # a clear move within 1 day (about half a normal day's move
                                                   # for Bitcoin, about a normal day's move for gold)
TEST_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Getting prices
# ---------------------------------------------------------------------------
def fake_history(asset):
    """Made-up hourly prices for TEST mode (no internet): 30 days ending 7 days after the test time,
    with a sharp Bitcoin drop after the test 'exchange hack' at 06:00 on 1 Nov."""
    start = TEST_NOW - timedelta(days=30)
    points, price = [], 68000.0 if asset == "Bitcoin" else 2650.0
    for h in range(37 * 24):
        t = start + timedelta(hours=h)
        wiggle = math.sin(h / 7) * 0.004 + math.sin(h / 31) * 0.01
        p = price * (1 + wiggle * (1 if asset == "Bitcoin" else 0.4))
        if asset == "Bitcoin" and t >= datetime(2026, 11, 1, 7, tzinfo=timezone.utc):
            hours_after = (t - datetime(2026, 11, 1, 7, tzinfo=timezone.utc)).total_seconds() / 3600
            p *= 1 - min(0.07, 0.012 * hours_after)
        if asset == "Gold" and t >= datetime(2026, 10, 30, 15, tzinfo=timezone.utc):
            p *= 0.985
        points.append([int(t.timestamp() * 1000), round(p, 2)])
    return points


def download_history(asset):
    """Hourly prices for the last 30 days, as a list of [time in milliseconds, price]."""
    errors = []
    try:
        url = (f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[asset]}/market_chart"
               "?vs_currency=usd&days=30")
        points = json.loads(fetch_page(url))["prices"]
        return [[int(t), round(p, 2)] for t, p in points], "CoinGecko"
    except Exception as e:
        errors.append(f"CoinGecko: {e}")
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO_SYMBOLS[asset]}?range=1mo&interval=1h"
        result = json.loads(fetch_page(url))["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        points = [[t * 1000, round(c, 2)] for t, c in zip(result["timestamp"], closes) if c is not None]
        return points, "Yahoo Finance (backup)"
    except Exception as e:
        errors.append(f"Yahoo: {e}")
    raise RuntimeError("; ".join(errors))


def get_history(asset, test_mode, now, force=False):
    """The hourly price history, downloaded again only if the saved copy is older than about an hour."""
    if test_mode:
        return {"points": [p for p in fake_history(asset)], "source": "made-up test prices"}
    path = data_folder(False) / f"price_history_{asset.lower()}.json"
    if path.exists() and not force:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if hours_between(now, parse_time(saved["downloaded"])) * 60 < HISTORY_REFRESH_MINUTES:
            return saved
    points, source = download_history(asset)
    saved = {"downloaded": now.isoformat(), "source": source, "points": points}
    path.write_text(json.dumps(saved), encoding="utf-8")
    return saved


def live_prices(test_mode, now):
    """The price right now for both assets (one request for both)."""
    if test_mode:
        out = {}
        for asset in COINGECKO_IDS:
            out[asset] = price_at(fake_history(asset), now)[0]
        return out, "made-up test prices"
    try:
        url = ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,pax-gold&vs_currencies=usd"
               "&include_24hr_change=true")
        data = json.loads(fetch_page(url))
        return {"Bitcoin": data["bitcoin"]["usd"], "Gold": data["pax-gold"]["usd"]}, "CoinGecko"
    except Exception as e:
        out = {}
        for asset in COINGECKO_IDS:                 # backup: the newest point of the hourly history
            out[asset] = get_history(asset, False, now)["points"][-1][1]
        return out, f"saved hourly history (live price failed: {e})"


def price_at(points, when, tolerance_hours=2):
    """The price closest to a moment in time, if there is one within the tolerance.
    Returns (price, time it was recorded) or (None, None)."""
    if not points:
        return None, None
    target = when.timestamp() * 1000
    best = min(points, key=lambda p: abs(p[0] - target))
    if abs(best[0] - target) > tolerance_hours * 3600 * 1000:
        return None, None
    return best[1], datetime.fromtimestamp(best[0] / 1000, tz=timezone.utc)


def pct(new, old):
    return round((new - old) / old * 100, 2) if new is not None and old else None


# ---------------------------------------------------------------------------
# Outcomes for events
# ---------------------------------------------------------------------------
def cell_verdict(move, direction):
    """For one price check: did it go the predicted way? right / wrong / flat / none (no prediction)."""
    if move is None or direction not in ("up", "down"):
        return "none"
    if abs(move) < 0.1:
        return "flat"
    return "right" if (move > 0) == (direction == "up") else "wrong"


def judge(outcome, asset, direction):
    """Was the PREDICTION right? Judged on the 1-DAY move after the first report, because that is when news
    moves prices most; after 7 days other news has mixed in (the 7-day move is still shown, for context).
       right          after 1 day the price had moved clearly (1.5% Bitcoin, 0.5% gold) the predicted way
       wrong          it had moved clearly the other way
       no_clear_move  it had not moved clearly either way (NOT counted in accuracy - nothing to judge)
       tracking       still waiting for the 1-day price
       no_prediction  MINT made no prediction for this event"""
    big = BIG_MOVE_PERCENT[asset]
    d1 = (outcome.get("d1") or {}).get("move")
    if direction not in ("up", "down"):
        return "no_prediction", "no prediction was made for this event"
    if d1 is None:
        h1 = (outcome.get("h1") or {}).get("move")
        early = f" (after 1 hour: {0.0 if abs(h1) < 0.05 else h1:+.1f}%)" if h1 is not None else ""
        return "tracking", "waiting for the 1-day price" + early
    if (direction == "up" and d1 >= big) or (direction == "down" and d1 <= -big):
        return "right", f"predicted {direction}: {d1:+.1f}% after 1 day (a clear move is {big}% or more)"
    if (direction == "up" and d1 <= -big) or (direction == "down" and d1 >= big):
        return "wrong", f"predicted {direction}, but {d1:+.1f}% after 1 day"
    return "no_clear_move", f"predicted {direction}, but only {d1:+.1f}% after 1 day (less than {big}%)"


def update_outcomes(memory, histories, now, factors):
    """Fill in the prices for every event that is not rejected. Returns how many prices were added."""
    added = 0
    for ev in memory["events"]:
        if ev["state"] is None:
            continue
        points = histories[ev["asset"]]["points"]
        o = ev.setdefault("outcome", {})
        start = parse_time(ev["earliest_report"])
        if o.get("start_price") is None:
            o["start_price"], t = price_at(points, start)
            o["start_time"] = t.isoformat() if t else None
        if o.get("start_price") is None:
            o["effect"], o["effect_reason"] = "no_price", "no price found for the time of the first report"
            continue
        if o.get("moved_before_mint") is None:
            p, _ = price_at(points, parse_time(ev["first_seen"]))
            o["moved_before_mint"] = pct(p, o["start_price"])
            o["delay_hours"] = round(hours_between(parse_time(ev["first_seen"]), start), 1)
        for key, hours in CHECKPOINTS.items():
            when = start + timedelta(hours=hours)
            if o.get(key) or now < when + timedelta(minutes=10):
                continue
            p, _ = price_at(points, when)
            if p is not None:
                o[key] = {"price": p, "move": pct(p, o["start_price"]), "time": when.isoformat()}
                added += 1
        if not ev.get("prediction"):
            from predict import predict
            ev["prediction"] = {**predict(ev["factor"], ev["title"], ev["asset"]), "by": "factor pattern"}
        direction = ev["prediction"]["direction"]
        for k in CHECKPOINTS:
            if o.get(k):
                o[k]["verdict"] = cell_verdict(o[k]["move"], direction)
        o["effect"], o["effect_reason"] = judge(o, ev["asset"], direction)
    return added


def run(test_mode=False, now=None):
    now = now or (TEST_NOW if test_mode else datetime.now(timezone.utc))
    print(f"MINT PRICE TRACKER  {now:%Y-%m-%d %H:%M} UTC{'  (TEST MODE)' if test_mode else ''}")
    folder = data_folder(test_mode)
    factors = {f["id"]: f for f in load_json("factors.json")["factors"]}
    summary = {"run_time": now.isoformat(), "errors": []}

    histories = {}
    for asset in COINGECKO_IDS:
        try:
            histories[asset] = get_history(asset, test_mode, now)
            print(f"   {asset}: {len(histories[asset]['points'])} hourly prices ({histories[asset]['source']})")
        except Exception as e:
            summary["errors"].append(f"{asset} history: {e}")
            print(f"   ERROR {asset} history: {e}")
            histories[asset] = {"points": [], "source": "none"}

    try:
        live, source = live_prices(test_mode, now)
        snaps_path = folder / "prices.json"
        snaps = json.loads(snaps_path.read_text(encoding="utf-8")) if snaps_path.exists() else []
        snaps.append({"time": now.isoformat(), **live, "source": source})
        snaps_path.write_text(json.dumps(snaps[-5000:]), encoding="utf-8")
        summary["live"] = live
        summary["source"] = source
        print(f"   Now: Bitcoin ${live['Bitcoin']:,.0f}, gold ${live['Gold']:,.0f}/oz ({source})")
    except Exception as e:
        summary["errors"].append(f"live price: {e}")
        print(f"   ERROR live price: {e}")

    import library
    try:
        n, left, errs = library.fill_starter(test_mode)
        if n or left or errs:
            print(f"   Past events library: loaded real prices for {n} starter event(s), {left} still to load"
                  + (f" (problem: {errs[0]})" if errs else ""))
    except Exception as e:
        print(f"   Past events library: could not load starter events ({e})")
    memory = load_memory(test_mode)
    added = update_outcomes(memory, histories, now, factors)
    save_memory(memory, test_mode)
    summary["prices_added"] = added
    tracked = [e for e in memory["events"] if e.get("outcome", {}).get("start_price")]
    moved = sum(1 for e in tracked if e["outcome"].get("effect") == "right")
    print(f"   Outcomes: {added} new price check(s). {len(tracked)} event(s) tracked, {moved} prediction(s) right so far.")
    for e in tracked[-8:]:
        o = e["outcome"]
        cells = "  ".join(f"{k}: {o[k]['move']:+.1f}%" if o.get(k) else f"{k}: -" for k in CHECKPOINTS)
        print(f"   [{e['id']}] {cells}  -> {o.get('effect')}")
    saved = "prices saved" if "live" in summary else "NO live price"
    print(f"\nBOTTOM LINE: {saved}, {added} new outcome check(s), {len(summary['errors'])} error(s).")
    return summary


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv)
