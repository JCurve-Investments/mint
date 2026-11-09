"""
MINT weekly summary
===================
Once a week (Sunday), MINT writes a short summary:
  - what happened to the Bitcoin and gold price this week
  - what MINT found (alerts, watches, info-only events, updates to events it already knew)
  - HOW ACCURATE ITS DECISIONS WERE, using the price checks from the Price Tracker:
        an alert is a "good call" if the price clearly moved afterwards,
        a "no alert" decision is a "good call" if the price did NOT clearly move.
    A move after an event does not prove the event caused it, so this is a guide, not proof.
  - what is coming up next week (from the Gold Scout's calendar)

It is saved in data/weekly_summaries.json (shown on the History page) and sent on Telegram.
"""

import json
from datetime import datetime, timedelta, timezone

from common import FOLDER
from supervisor import data_folder, load_memory, parse_time

NOT_ALERTED = ("watch", "expired", "rejected", "context", "summary", "old")


def price_points(asset, test_mode):
    if test_mode:
        import prices
        return prices.fake_history(asset)
    path = data_folder(False) / f"price_history_{asset.lower()}.json"
    return json.loads(path.read_text(encoding="utf-8"))["points"] if path.exists() else []


def week_move(asset, start, end, test_mode):
    import prices
    pts = price_points(asset, test_mode)
    a, _ = prices.price_at(pts, start, tolerance_hours=6)
    b, _ = prices.price_at(pts, end, tolerance_hours=6)
    return b, prices.pct(b, a)


def newest_calendar(test_mode):
    pattern = "gold_scout_TEST_*.json" if test_mode else "gold_scout_2*.json"
    files = sorted((FOLDER / "results").glob(pattern))
    return json.loads(files[-1].read_text(encoding="utf-8")).get("calendar", {}) if files else {}


def build(test_mode=False, now=None):
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    memory = load_memory(test_mode)
    events = [e for e in memory["events"] if e["state"] != "baseline"]
    week = [e for e in events if parse_time(e["first_seen"]) >= start]
    count = lambda states: sum(1 for e in week if e["state"] in states)
    updates = sum(1 for e in events for h in e.get("history", [])
                  if "no new alert" in h["note"] and parse_time(h["time"]) >= start)

    lines = [f"MINT weekly summary · {start:%d %b} to {now:%d %b %Y}", ""]
    lines.append("Prices this week:")
    for asset in ("Bitcoin", "Gold"):
        price, move = week_move(asset, start, now, test_mode)
        lines.append(f"- {asset}: " + (f"${price:,.0f} ({move:+.1f}% this week)" if price else "no price data yet"))

    lines += ["", f"What MINT found: {len(week)} event(s)",
              f"- {count(('alerted',))} alert(s) sent",
              f"- {count(('watch', 'expired'))} watch(es) that did not get enough proof",
              f"- {count(('context', 'summary'))} info-only event(s) (slow factors or older news)",
              f"- {updates} new article(s) about events MINT already knew (no new alert)"]
    alerts = [e for e in week if e["state"] == "alerted"]
    if alerts:
        lines += ["", "Alerts:"]
        for e in alerts:
            o = e.get("outcome") or {}
            res = {"right": "prediction right", "wrong": "prediction wrong", "no_clear_move": "no clear move",
                   "tracking": "still tracking", "no_prediction": "no prediction"}.get(o.get("effect"), "no price yet")
            d1 = f", 1 day: {o['d1']['move']:+.1f}%" if o.get("d1") else ""
            lines.append(f"- {e['title'][:90]} ({e['asset']}) - {res}{d1}")

    # Accuracy over the last 30 days: only events whose price check is finished
    recent = [e for e in events if parse_time(e["first_seen"]) >= now - timedelta(days=30)
              and (e.get("outcome") or {}).get("effect") in ("right", "wrong")]
    right = [e for e in recent if e["outcome"]["effect"] == "right"]
    wrong = [e for e in recent if e["outcome"]["effect"] == "wrong"]
    a_all = [e for e in recent if e["state"] == "alerted"]
    a_good = [e for e in a_all if e["outcome"]["effect"] == "right"]
    n_all = [e for e in recent if e["state"] in NOT_ALERTED]
    n_good = [e for e in n_all if e["outcome"]["effect"] != "right"]     # no alert, and the prediction did not come true
    lines += ["", "How good were MINT's predictions? (last 30 days)"]
    if not recent:
        lines.append("- Not enough finished price checks yet (each takes up to 7 days).")
    else:
        lines.append(f"- Prediction accuracy: {len(right)} of {len(recent)} right ({len(right) / len(recent):.0%}) "
                     "- counting only events where the price clearly moved within 1 day")
        if a_all:
            lines.append(f"- Alerts where the prediction came true: {len(a_good)} of {len(a_all)}")
        missed = len(n_all) - len(n_good)
        if missed:
            lines.append(f"- Events with no alert where the prediction still came true: {missed} "
                         "(MINT may have been too careful)")
        lines.append("  (A price move does not prove the event caused it.)")
    delays = [e["outcome"]["delay_hours"] for e in week if (e.get("outcome") or {}).get("delay_hours") is not None]
    if delays:
        lines.append(f"- Average time from first report to MINT noticing: {sum(delays) / len(delays):.1f} hours")

    upcoming = newest_calendar(test_mode).get("upcoming", [])
    if upcoming:
        lines += ["", "Coming up:"] + [f"- {d}: {name}" for d, name, _ in upcoming[:5]]
    lines += ["", "Risk information only, not financial advice."]
    text = "\n".join(lines)
    stats = {"events": len(week), "finished": len(recent), "right": len(right), "wrong": len(wrong),
             "alerts": len(a_all), "alerts_good": len(a_good)}
    return {"time": now.isoformat(), "week": f"{now:%G-W%V}", "text": text[:3900], "stats": stats}


def save(summary, test_mode):
    path = data_folder(test_mode) / "weekly_summaries.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    saved = [s for s in saved if s["week"] != summary["week"]] + [summary]
    path.write_text(json.dumps(saved[-52:], indent=2), encoding="utf-8")


def load_all(test_mode):
    path = data_folder(test_mode) / "weekly_summaries.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


if __name__ == "__main__":
    import sys
    print(build(test_mode="--test" in sys.argv)["text"])
