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
# Price checks after the first report (hours). Keys stay short because they are saved in memory.
CHECKPOINTS = {"h1": 1, "h5": 5, "h10": 10, "d1": 24, "h36": 36, "d2": 48, "h84": 84, "d5": 120, "d7": 168}
CHECKPOINT_NAMES = {"h1": "1 hour", "h5": "5 hours", "h10": "10 hours", "d1": "1 day", "h36": "36 hours",
                    "d2": "2 days", "h84": "3.5 days", "d5": "5 days", "d7": "7 days"}
MIN_CLEAR_MOVE = {"Bitcoin": 0.3, "Gold": 0.1}     # never count a move smaller than this, even after 1 hour
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
def clear_move(asset, hours):
    """How big a move must be to count as a CLEAR move after this many hours.
    Prices wiggle more the longer you wait (roughly with the square root of time), so the bar grows:
        Bitcoin: 0.3% after 1 h, 0.7% after 5 h, 1.5% after 1 day, 2.1% after 2 days, 4.0% after 7 days
        Gold:    0.1% after 1 h, 0.2% after 5 h, 0.5% after 1 day, 0.7% after 2 days, 1.3% after 7 days
    A smaller move is just normal wiggle: it is shown, but not counted as right or wrong."""
    return round(max(MIN_CLEAR_MOVE[asset], BIG_MOVE_PERCENT[asset] * math.sqrt(hours / 24)), 2)


def cell_verdict(move, direction, asset=None, hours=None):
    """For one price check: right (clear move the predicted way) / wrong (clear move the other way) /
    flat (too small to count) / none (no prediction)."""
    if move is None or direction not in ("up", "down"):
        return "none"
    bar = clear_move(asset, hours) if asset and hours else 0.1
    if abs(move) < bar:
        return "flat"
    return "right" if (move > 0) == (direction == "up") else "wrong"


def judge(outcome, asset, direction):
    """Was the PREDICTION right? MINT checks the price 9 times after the first report (1 h, 5 h, 10 h, 1 day,
    36 h, 2 days, 3.5 days, 5 days, 7 days). Each check is right, wrong or too small to count (see clear_move).
       right          more checks moved clearly the predicted way than the other way
       wrong          more checks moved clearly the other way
       no_clear_move  no clear move yet, or as many right as wrong (NOT counted in accuracy)
       tracking       the first day is not over yet (shown with the checks so far)
       no_prediction  MINT made no prediction for this event
    The result can still change until the 7-day check is in; outcome["final"] says when it is final."""
    if direction not in ("up", "down"):
        return "no_prediction", "no prediction was made for this event"
    done = [k for k in CHECKPOINTS if outcome.get(k)]
    right = [k for k in done if outcome[k].get("verdict") == "right"]
    wrong = [k for k in done if outcome[k].get("verdict") == "wrong"]
    score = f"{len(right)} of {len(right) + len(wrong)} clear price checks went the predicted way" \
        if right or wrong else "no clear move in any price check yet"
    outcome["checks"] = {"done": len(done), "right": len(right), "wrong": len(wrong), "total": len(CHECKPOINTS)}
    outcome["final"] = bool(outcome.get("d7"))
    if not outcome.get("d1"):
        return "tracking", f"first day not over yet - {score} ({len(done)} of {len(CHECKPOINTS)} checks done)"
    tail = "" if outcome["final"] else f" (so far: {len(done)} of {len(CHECKPOINTS)} checks done)"
    if len(right) > len(wrong):
        return "right", f"predicted {direction}: {score}{tail}"
    if len(wrong) > len(right):
        return "wrong", f"predicted {direction}, but {score}{tail}"
    return "no_clear_move", f"predicted {direction}: {score}{tail}"


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
            p, _ = price_at(points, when, tolerance_hours=1)       # within an hour of the checkpoint
            if p is not None:
                o[key] = {"price": p, "move": pct(p, o["start_price"]), "time": when.isoformat()}
                added += 1
        if not o.get("d7") and points:              # the move SO FAR, until the 7-day price is in
            t_last, p_last = points[-1]
            o["now"] = {"move": pct(p_last, o["start_price"]),
                        "time": datetime.fromtimestamp(t_last / 1000, timezone.utc).isoformat(),
                        "hours": round(hours_between(datetime.fromtimestamp(t_last / 1000, timezone.utc), start), 1)}
        elif o.get("d7"):
            o.pop("now", None)
        if not ev.get("prediction"):
            from predict import predict
            ev["prediction"] = {**predict(ev["factor"], ev["title"], ev["asset"]), "by": "factor pattern"}
        direction = ev["prediction"]["direction"]
        for k, hours in CHECKPOINTS.items():
            if o.get(k):
                o[k]["verdict"] = cell_verdict(o[k]["move"], direction, ev["asset"], hours)
        if o.get("now"):
            o["now"]["verdict"] = cell_verdict(o["now"]["move"], direction, ev["asset"], max(o["now"]["hours"], 1))
        o["effect"], o["effect_reason"] = judge(o, ev["asset"], direction)
        o["impact"] = describe_impact(points, start, o["start_price"], direction, ev["asset"], now)
    return added


def describe_impact(points, start, start_price, direction, asset, now):
    """How big was the event's effect on the price, and how long did it last?
    Uses every saved hourly price in the 7 days after the first report.
      peak       the biggest move the PREDICTED way (and when)
      faded      when the price went back past the starting price after the peak (the effect was over)
      against    the biggest move the other way (and when)
    Returns a dict with a plain-English 'text', or None if there are not enough prices yet."""
    if direction not in ("up", "down") or not start_price:
        return None
    t0 = start.timestamp() * 1000
    end = min(t0 + 168 * 3600e3, now.timestamp() * 1000)
    path = [(round((t - t0) / 3600e3, 1), (p - start_price) / start_price * 100) for t, p in points if t0 <= t <= end]
    if len(path) < 2:
        return None
    sign = 1 if direction == "up" else -1
    peak_h, peak = max(path, key=lambda x: sign * x[1])
    against_h, against = min(path, key=lambda x: sign * x[1])
    peak_fav, against_fav = sign * peak, sign * against
    faded = next((h for h, m in path if h > peak_h and sign * m <= 0), None)
    last_h, last = path[-1]
    hrs = lambda h: f"{h:.0f} h" if h < 48 else f"{h / 24:.1f} days"
    word = "rose" if direction == "up" else "fell"
    other = "fell" if direction == "up" else "rose"
    size = lambda m, h: "small" if abs(m) < clear_move(asset, max(h, 1)) else "clear"
    if peak_fav <= 0:
        text = f"Never moved the predicted way. It {other} up to {abs(against):.1f}% (after {hrs(against_h)})."
    else:
        text = f"{size(peak, peak_h).capitalize()} impact: {asset} {word} up to {abs(peak):.1f}% (after {hrs(peak_h)})."
        if faded is not None:
            text += f" The effect lasted about {hrs(faded)}, then faded"
            text += (f" - later it {other} up to {abs(against):.1f}% (after {hrs(against_h)})." if against_fav < 0 and against_h > faded
                     else ".")
        else:
            text += f" Still in the predicted direction after {hrs(last_h)} ({last:+.1f}%)."
    return {"peak": round(peak, 2), "peak_hours": peak_h, "faded_hours": faded, "against": round(against, 2),
            "against_hours": against_h, "until_hours": last_h, "text": text}


def rejudge(memory):
    """Judge every event again with its CURRENT prediction. The Decision agent runs after the Price Tracker and
    can add a prediction, so without this an event could show a prediction but still say "No prediction"."""
    for ev in memory["events"]:
        o, direction = ev.get("outcome") or {}, (ev.get("prediction") or {}).get("direction")
        if ev["state"] is None or o.get("start_price") is None:
            continue
        for k, hours in CHECKPOINTS.items():
            if o.get(k):
                o[k]["verdict"] = cell_verdict(o[k]["move"], direction, ev["asset"], hours)
        if o.get("now"):
            o["now"]["verdict"] = cell_verdict(o["now"]["move"], direction, ev["asset"], max(o["now"]["hours"], 1))
        o["effect"], o["effect_reason"] = judge(o, ev["asset"], direction)


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
