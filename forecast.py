"""
MINT Forecasts and results
==========================
Every prediction says four things, all worked out from real data (never typed in, never from headline wording):

  DIRECTION   up or down (from similar past events + the factor pattern; see library.py)
  SIZE        how much the price is expected to move, e.g. -0.8% to -1.5%
              = the typical move after the similar past events that went the predicted way
  TIME        how long the effect should take to show, e.g. "within about 1.5 days"
              = how long the similar past moves took to peak (MINT's own events), or, without that, the time it
                normally takes the market to move that much (bigger moves take longer):
                    window = 24 h x (expected move / a normal day's move)^2, kept between 4 hours and 5 days
  LIKELY      a percentage, e.g. 70% likely
              = the share of similar past events that went the predicted way, counted fairly for small numbers
                (4 of 5 = 71%, 40 of 50 = 79%: (agreeing + 1) / (all + 2)), then
                +5 points for each market lens (volume, buy/sell) that agrees, -5 for each that disagrees

HOW A RESULT IS JUDGED (from the moment MINT made the prediction, so a move that happened before MINT saw the
news never counts):
  threshold   the move that counts = half the expected move, but never less than about two hours of normal
              price wiggle (so luck does not count). Gold today: about 0.5%. Bitcoin: about 1.2%.
  HIT         the price moved past the threshold the predicted way and STAYED there for at least 2 hours,
              inside the time window
  MISS        the price moved past the threshold the OTHER way and stayed there for 2 hours first
  PARTIAL     the right way but too small or too short (it reached the threshold but did not hold for 2 hours,
              or it held a smaller move)
  NO CLEAR MOVE  nothing happened either way inside the window
A HIT or MISS is known as soon as it happens; PARTIAL / NO CLEAR MOVE only when the window ends.

Fair accuracy (shown on the History page):
  - events of the same asset within 12 hours that predict the same way are ONE price move, counted once
  - MINT is compared with the best "lazy" guess: always saying up, or always saying down, judged the same way.
    If MINT does not beat that, the news added nothing.
  - calibration: of the predictions MINT called "70% likely", how many really happened?
"""

import math
import statistics
from datetime import datetime, timedelta, timezone

from supervisor import parse_time, hours_between

HOLD_HOURS = 2                       # a move must last this long to count
MIN_THRESHOLD = {"Bitcoin": 0.6, "Gold": 0.25}
DEFAULT_SIGMA = {"Bitcoin": 2.8, "Gold": 1.0}    # a normal day's move (%), if the price history is too short
MIN_WINDOW, MAX_WINDOW = 4, 120
LENS_POINTS = 0.05
GROUP_HOURS = 12
VERSION = 2                          # forecasts made with this version of the rules


def daily_sigma(points, asset):
    """A normal day's move (%), from the hourly prices: standard deviation of hourly changes x sqrt(24)."""
    pts = [p for p in points if p[1]][-24 * 30:]
    if len(pts) < 48:
        return DEFAULT_SIGMA[asset]
    rets = []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        hours = (t1 - t0) / 3600e3
        if 0.5 <= hours <= 2:
            rets.append(math.log(p1 / p0) / math.sqrt(hours))
    if len(rets) < 24:
        return DEFAULT_SIGMA[asset]
    return round(statistics.pstdev(rets) * math.sqrt(24) * 100, 2)


def threshold(asset, sigma, mid):
    floor = max(MIN_THRESHOLD[asset], 2 * sigma / math.sqrt(24))
    return round(max(floor, 0.5 * abs(mid)), 2), round(floor, 2)


def pct_range(values):
    v = sorted(values)
    if len(v) >= 4:
        q = statistics.quantiles(v, n=4)
        return q[0], statistics.median(v), q[2]
    m = statistics.median(v)
    return m * 0.6, m, m * 1.4


def lens_votes(market_state, asset):
    L = ((market_state or {}).get("lenses") or {}).get(asset) or {}
    return {k: (L.get(k) or {}).get("vote", 0) for k in ("volume", "buy_sell")}, L


def make_forecast(ev, sigma, market_state=None, library_entries=None):
    """The size, time window and likelihood for an event's prediction (see the top of this file)."""
    p = ev.get("prediction") or {}
    d = p.get("direction")
    if d not in ("up", "down"):
        return None
    sign = 1 if d == "up" else -1
    sims = [s for s in (p.get("similar") or []) if s.get("d1") is not None]
    agree = [s for s in sims if sign * s["d1"] >= 0.1]
    if ev.get("market_triggered"):
        mid = sigma * 0.6
        lo, hi = mid * 0.6, mid * 1.5
        basis = "market-triggered: busy trading with the same side in control; size = 0.6 of a normal day's move"
    elif agree:
        lo, mid, hi = pct_range([abs(s["d1"]) for s in agree])
        basis = f"{len(agree)} of {len(sims)} similar past events moved this way (typical move {mid:.1f}%)"
    else:
        same = [abs(e["d1"]) for e in (library_entries or []) if e.get("asset") == ev["asset"]
                and e.get("factor") == ev.get("factor") and e.get("d1") is not None and abs(e["d1"]) >= 0.1]
        if len(same) >= 3:
            lo, mid, hi = pct_range(same)
            basis = f"no similar past event moved this way; size from {len(same)} past {ev.get('factor')} events"
        else:
            mid = sigma * 0.5
            lo, hi = mid * 0.6, mid * 1.4
            basis = "no similar past events moved this way; size = half a normal day's move"
    # one event rarely moves the price more than 1.5 normal days on its own: bigger past moves had other causes too
    mid = min(max(mid, 0.1), 1.5 * sigma)
    hi = min(max(hi, mid * 1.2), 2 * sigma)
    lo = min(max(lo, 0.05), mid * 0.8)
    peaks = [s["peak_hours"] for s in agree if s.get("peak_hours")]
    if len(peaks) >= 3:
        window = statistics.median(peaks)
        window_basis = f"similar past moves peaked after about {fmt_hours(window)}"
    else:
        window = 24 * (mid / sigma) ** 2
        window_basis = "time the market normally needs to move this much"
    window = round(min(max(window, MIN_WINDOW), MAX_WINDOW), 1)
    n, k = len(sims), len(agree)
    prob = (k + 1) / (n + 2) if n else 0.5
    rule = (p.get("votes") or {})
    votes, L = lens_votes(market_state, ev["asset"])
    lens_notes = []
    for name, v in votes.items():
        if v:
            prob += LENS_POINTS if v == sign else -LENS_POINTS
            lens_notes.append(f"{'agrees' if v == sign else 'disagrees'}: {name.replace('_', '/')}")
    prob = round(min(max(prob, 0.2), 0.9), 2)
    thr, floor = threshold(ev["asset"], sigma, mid)
    return {"version": VERSION, "made": datetime.now(timezone.utc).isoformat(),
            "low": round(sign * lo, 2), "mid": round(sign * mid, 2), "high": round(sign * hi, 2),
            "window_hours": window, "probability": prob, "agree": k, "similar": n,
            "basis": basis, "window_basis": window_basis, "sigma": sigma, "threshold": thr, "floor": floor,
            "lenses": {name: v for name, v in votes.items()},
            "lens_text": {name: (L.get(name) or {}).get("text") for name in votes if (L.get(name) or {}).get("ok")},
            "lens_notes": lens_notes, "rule_votes": rule}


def fmt_hours(h):
    if h is None:
        return "?"
    if h < 1.5:
        return f"{h * 60:.0f} min" if h < 1 else "1 hour"
    if h < 36:
        return f"{h:.0f} hours"
    return f"{h / 24:.1f} days".replace(".0 days", " days")


def headline(ev):
    """'🥇 Gold ↓ -0.8% to -1.5% within ~1.5 days · 70% likely' (or a shorter line without a forecast)."""
    p = ev.get("prediction") or {}
    f = p.get("forecast")
    icon = {"Bitcoin": "₿", "Gold": "🥇"}.get(ev["asset"], "")
    if p.get("direction") not in ("up", "down"):
        return f"{icon} {ev['asset']} · direction unclear (not enough evidence)"
    arrow = "↑" if p["direction"] == "up" else "↓"
    if not f:
        return f"{icon} {ev['asset']} {arrow} {p['direction'].upper()}"
    lo, hi = sorted([abs(f["low"]), abs(f["high"])])
    s = "+" if p["direction"] == "up" else "-"
    likely = f"{f['probability'] * 100:.0f}% likely" if f["probability"] >= 0.5 else \
        f"only {f['probability'] * 100:.0f}% likely (past events disagree)"
    return f"{icon} {ev['asset']} {arrow} {s}{lo:.1f}% to {s}{hi:.1f}% within ~{fmt_hours(f['window_hours'])} · {likely}"


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------
def path_from(points, start, start_price, end):
    t0 = start.timestamp() * 1000
    t1 = end.timestamp() * 1000
    return [((t - t0) / 3600e3, (p - start_price) / start_price * 100) for t, p in points if t0 <= t <= t1]


def first_hold(path, sign, thr):
    """When did the price first stay past the threshold (in that direction) for HOLD_HOURS? Returns
    (hour it became a hold, hour it started, longest hold) - or (None, None, longest hold)."""
    run_start, longest, best = None, 0.0, (None, None)
    for h, m in path:
        if sign * m >= thr:
            run_start = h if run_start is None else run_start
            length = h - run_start
            longest = max(longest, length)
            if length >= HOLD_HOURS and best[0] is None:
                best = (h, run_start)
        else:
            run_start = None
    return best[0], best[1], round(longest, 1)


def judge(ev, points, now):
    """HIT / MISS / PARTIAL / NO CLEAR MOVE / tracking for one event (see the top of this file)."""
    p = ev.get("prediction") or {}
    f = p.get("forecast")
    o = ev.setdefault("outcome", {})
    if p.get("direction") not in ("up", "down") or not f:
        return {"verdict": "no_prediction", "final": True, "text": "no prediction was made"}
    start = parse_time(ev["first_seen"])
    from prices import price_at
    sp, st = price_at(points, start)
    if sp is None:
        return {"verdict": "no_price", "final": False, "text": "no price found for the time MINT made the prediction"}
    end = start + timedelta(hours=f["window_hours"])
    path = path_from(points, start, sp, min(end, now))
    sign = 1 if p["direction"] == "up" else -1
    thr = f["threshold"]
    hit_at, hit_from, held = first_hold(path, sign, thr)
    miss_at, miss_from, held_against = first_hold(path, -sign, thr)
    window_over = now >= end + timedelta(minutes=30)
    fav = max((sign * m for h, m in path), default=0.0)
    peak_h = next((h for h, m in path if sign * m == fav), None) if path else None
    out = {"start_price": sp, "start_time": start.isoformat(), "threshold": thr, "window_hours": f["window_hours"],
           "peak": round(sign * fav, 2), "peak_hours": round(peak_h, 1) if peak_h is not None else None,
           "held_hours": held, "checked_until": round(path[-1][0], 1) if path else 0}
    if hit_at is not None and (miss_at is None or hit_at <= miss_at):
        verdict = "hit"
        text = (f"moved {sign * thr:+.1f}% or more and held it for {HOLD_HOURS}+ hours, after {fmt_hours(hit_from)}"
                f" (biggest move {sign * fav:+.1f}%)")
    elif miss_at is not None:
        verdict = "miss"
        text = f"moved {-sign * thr:+.1f}% or more the OTHER way for {HOLD_HOURS}+ hours, after {fmt_hours(miss_from)}"
    elif not window_over:
        verdict = "tracking"
        text = f"still inside the {fmt_hours(f['window_hours'])} window (biggest move so far {sign * fav:+.1f}%)"
    elif fav >= thr or (fav >= f["floor"] and held >= HOLD_HOURS):
        verdict = "partial"
        text = (f"the right way (up to {sign * fav:+.1f}%), but it did not stay past {sign * thr:+.1f}% for "
                f"{HOLD_HOURS} hours")
    else:
        verdict = "no_clear_move"
        text = f"no clear move either way within {fmt_hours(f['window_hours'])} (biggest {sign * fav:+.1f}%)"
    out.update({"verdict": verdict, "final": verdict in ("hit", "miss") or window_over, "text": text})
    size_word = None
    if verdict in ("hit", "partial"):
        lo, hi = sorted([abs(f["low"]), abs(f["high"])])
        size_word = "inside the predicted range" if lo <= fav <= hi * 1.05 else \
            "bigger than predicted" if fav > hi else "smaller than predicted"
    out["size"] = size_word
    # the "lazy guess" (always the other direction), judged the same way - for the fair-accuracy comparison
    if verdict in ("hit", "miss", "partial", "no_clear_move"):
        out["opposite"] = "hit" if verdict == "miss" else "miss" if verdict == "hit" else \
            ("partial" if any(-sign * m >= thr for h, m in path) else "no_clear_move")
    return out


EFFECT = {"hit": "right", "miss": "wrong", "partial": "partial", "no_clear_move": "no_clear_move",
          "tracking": "tracking", "no_prediction": "no_prediction", "no_price": "no_price"}


def apply(memory, histories, market_state, now, library_entries=None):
    """Make missing forecasts and judge every event. Forecasts are FROZEN once made (after the run they were made
    in), so a prediction can never be changed after the price moves."""
    sig = {a: daily_sigma(h.get("points") or [], a) for a, h in histories.items()}
    for ev in memory.get("events", []):
        if ev.get("state") in (None, "baseline"):
            continue
        p = ev.get("prediction") or {}
        if not p:
            continue
        f = p.get("forecast")
        fresh = hours_between(now, parse_time(ev["first_seen"])) <= 0.75
        if p.get("direction") not in ("up", "down"):
            p.pop("forecast", None)
        elif not f or f.get("version") != VERSION or (fresh and (f["mid"] > 0) != (p["direction"] == "up")):
            new = make_forecast(ev, sig.get(ev["asset"], DEFAULT_SIGMA[ev["asset"]]),
                                market_state if fresh else None, library_entries)
            if new:
                new["lenses_used"] = fresh
                if not fresh:
                    new["note"] = "made after the event (re-judged with the new rules); no market lenses"
                p["forecast"] = new
        points = (histories.get(ev["asset"]) or {}).get("points") or []
        o = ev.setdefault("outcome", {})
        r = judge(ev, points, now)
        o["result"] = r
        if o.get("effect") in ("right", "wrong", "no_clear_move"):
            o["old_effect"] = o["effect"]            # the old 9-check rule (from the first report), for comparison
        o["effect"] = EFFECT.get(r["verdict"], r["verdict"])
        o["effect_reason"] = r["text"]
    return sig


# ---------------------------------------------------------------------------
# Fair accuracy (History page, 9 pm summary, weekly summary)
# ---------------------------------------------------------------------------
def market_accuracy(events):
    """HIT rate of the market-triggered events (volume + buyers/sellers, no news) - kept apart from the news events."""
    judged = [e for e in events if e.get("market_triggered") and
              ((e.get("outcome") or {}).get("result") or {}).get("verdict") in ("hit", "miss", "partial", "no_clear_move")
              and e["outcome"]["result"].get("final")]
    count = lambda v: sum(1 for e in judged if e["outcome"]["result"]["verdict"] == v)
    hit, miss, partial, flat = count("hit"), count("miss"), count("partial"), count("no_clear_move")
    decided = hit + miss + partial
    open_ = sum(1 for e in events if e.get("market_triggered") and e not in judged)
    return {"judged": len(judged), "hit": hit, "miss": miss, "partial": partial, "no_clear_move": flat,
            "hit_rate": round(hit / decided, 3) if decided else None, "open": open_,
            "total": sum(1 for e in events if e.get("market_triggered"))}


def accuracy(events, cloud_start=None):
    market_part = market_accuracy(events)
    events = [e for e in events if not e.get("market_triggered")]      # the news events: the main accuracy
    judged = [e for e in events if ((e.get("outcome") or {}).get("result") or {}).get("verdict") in
              ("hit", "miss", "partial", "no_clear_move") and (e["outcome"]["result"].get("final"))]
    count = lambda v: sum(1 for e in judged if e["outcome"]["result"]["verdict"] == v)
    hit, miss, partial, flat = count("hit"), count("miss"), count("partial"), count("no_clear_move")
    decided = hit + miss + partial
    out = {"judged": len(judged), "hit": hit, "miss": miss, "partial": partial, "no_clear_move": flat,
           "hit_rate": round(hit / decided, 3) if decided else None, "market": market_part}
    # same price move = one group
    groups = []
    for asset in ("Bitcoin", "Gold"):
        evs = sorted((e for e in judged if e["asset"] == asset), key=lambda e: e["first_seen"])
        for e in evs:
            t = parse_time(e["first_seen"])
            d = e["prediction"]["direction"]
            g = groups[-1] if groups and groups[-1]["asset"] == asset else None
            if g and g["dir"] == d and hours_between(t, g["last"]) <= GROUP_HOURS:
                g["events"].append(e)
                g["last"] = t
            else:
                groups.append({"asset": asset, "dir": d, "last": t, "events": [e]})
    gh = gm = gp = 0
    for g in groups:
        vs = [x["outcome"]["result"]["verdict"] for x in g["events"]]
        score = vs.count("hit") - vs.count("miss")
        if vs.count("hit") + vs.count("miss") + vs.count("partial") == 0:
            continue
        if score > 0:
            gh += 1
        elif score < 0:
            gm += 1
        else:
            gp += 1
    gd = gh + gm + gp
    out["moves"] = {"total": gd, "hit": gh, "miss": gm, "other": gp, "hit_rate": round(gh / gd, 3) if gd else None}
    # lazy guess: always up / always down
    lazy = {}
    for word in ("up", "down"):
        h = d_ = 0
        for e in judged:
            r = e["outcome"]["result"]
            v = r["verdict"] if e["prediction"]["direction"] == word else r.get("opposite")
            if v in ("hit", "miss", "partial"):
                d_ += 1
                h += v == "hit"
        lazy[word] = round(h / d_, 3) if d_ else None
    best = max((v, k) for k, v in lazy.items() if v is not None) if any(v is not None for v in lazy.values()) else (None, None)
    out["lazy"] = {"always_up": lazy["up"], "always_down": lazy["down"], "best": best[0], "best_word": best[1]}
    # calibration
    bins = [(0.0, 0.55, "under 55%"), (0.55, 0.65, "55-65%"), (0.65, 0.75, "65-75%"), (0.75, 1.01, "75%+")]
    cal = []
    for lo, hi, label in bins:
        es = [e for e in judged if lo <= ((e["prediction"].get("forecast") or {}).get("probability") or 0) < hi
              and e["outcome"]["result"]["verdict"] in ("hit", "miss", "partial")]
        if es:
            cal.append({"label": label, "n": len(es), "hit": sum(1 for e in es if e["outcome"]["result"]["verdict"] == "hit")})
    out["calibration"] = cal
    # old rule (9 checks, from the first report) for comparison
    old = [e for e in events if (e.get("outcome") or {}).get("old_effect") in ("right", "wrong")]
    oright = sum(1 for e in old if e["outcome"]["old_effect"] == "right")
    out["old_rule"] = {"right": oright, "total": len(old), "rate": round(oright / len(old), 3) if old else None}
    # how quickly MINT noticed the news (only while running on a schedule, only stories that were new)
    delays = {}
    for e in events:
        o = e.get("outcome") or {}
        if o.get("delay_hours") is None or e.get("state") in ("summary", "old", "baseline", None):
            continue
        if cloud_start and e["first_seen"] < cloud_start:
            continue
        delays.setdefault(e["asset"], []).append(o["delay_hours"])
    allv = sorted(v for vs in delays.values() for v in vs)
    out["delay"] = {"median_hours": round(statistics.median(allv), 1) if allv else None, "n": len(allv),
                    "by_asset": {a: round(statistics.median(v), 1) for a, v in delays.items() if v},
                    "since": cloud_start}
    return out
