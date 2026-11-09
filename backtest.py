"""
MINT Past-event test (success criterion 1: detection accuracy)
==============================================================
You give MINT 5 real past events (date, asset, headline) whose market reaction YOU already know
(your answer key). MINT does NOT get the answer. It has to:

  1. decide which factor the event belongs to,
  2. decide if it would send an ALERT (strong factors) or only INFO (moderate factors),
  3. predict UP or DOWN,

using only what it would have known at the time: past events from BEFORE that date (the event itself
and anything after it are hidden). MINT predicts the same way it does live:
  - the RULES: similar past events + the factor's pattern (no AI)
  - the AI (Groq), given the same past events and told to pretend it is that date
  - MINT's final prediction = the AI's direction if the AI is on, otherwise the rules' (as in live use)
Careful: an AI model may still REMEMBER what happened after famous events, so the rules-only result is
the fairest test. Both are shown.

MINT does not download prices here: the real move comes from YOUR answer key.
Then MINT's answer is compared with the real move. Criterion A's 3 checks for a PASS:
  (1) an alert is sent, (2) the right asset and factor, (3) the right direction over the next 7 days.

Your events go in backtest_events.json (edit it on GitHub or in the MINT folder).
Results are saved to data/backtest_results.json and shown on the History page.

Run on its own:  py backtest.py
"""

import hashlib
import json
from datetime import datetime, timezone

import library
from common import FOLDER, load_json
from supervisor import data_folder, load_memory

EVENTS_FILE = FOLDER / "backtest_events.json"


def detect_factor(headline, asset, factors):
    """Which factor is this headline about? The same keyword check the Supervisor uses."""
    import re
    text = headline.lower()
    for f in factors:
        if f["asset"] == asset and f.get("keywords") and \
                any(re.search(r"\b" + re.escape(k) + r"(s|es|ed|d)?\b", text) for k in f["keywords"]):
            return f
    return None


def run(force=False):
    if not EVENTS_FILE.exists():
        return None
    config = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    events = [e for e in config.get("events", []) if e.get("date") and e.get("headline") and e.get("asset")]
    out_path = data_folder(False) / "backtest_results.json"
    fingerprint = hashlib.sha1(json.dumps(events, sort_keys=True).encode()).hexdigest()
    if out_path.exists() and not force:
        old = json.loads(out_path.read_text(encoding="utf-8"))
        if old.get("fingerprint") == fingerprint:
            return old                        # nothing changed since the last test
    factors = load_json("factors.json")["factors"]
    entries = library.all_entries(False, load_memory(False))
    import decision
    choice = decision.ai_choice(decision.load_settings())
    sign = lambda v: None if v is None else ("up" if v > 0 else "down" if v < 0 else "flat")
    results = []
    print("MINT PAST-EVENT TEST (success criterion 1)")
    for i, ev in enumerate(events, 1):
        f = detect_factor(ev["headline"], ev["asset"], factors)
        fid = f["id"] if f else None
        alert = bool(f and f.get("strength") == "strong")
        rules = library.make_prediction(fid or "", ev["headline"], ev["asset"], entries, before=ev["date"])
        ai_dir, ai_why, ai_by = None, "", "AI is off"
        if choice:
            fake = {"asset": ev["asset"], "factor": fid or "", "title": ev["headline"], "prediction": rules,
                    "sources": [{"organisation": "past-event test"}],
                    "state_reason": (f"TEST: pretend today is {ev['date']}. Use ONLY this headline and the past events "
                                     "listed - not anything you know about what happened after this date.")}
            try:
                a = decision.analyse(fake, f or {}, choice)
                ai_dir, ai_why, ai_by = a.get("direction"), " ".join(a.get("why") or [])[:300], a["by"]
            except Exception as e:
                ai_by = f"AI problem: {e}"
        final = ai_dir if ai_dir in ("up", "down") else rules["direction"]
        d7, d1 = ev.get("real_move_7_days_percent"), ev.get("real_move_1_day_percent")
        real_dir = sign(d7)
        checks = {
            "alert_sent": alert,
            "right_asset_and_factor": bool(f) and (not ev.get("expected_factor") or ev["expected_factor"] == fid),
            "right_direction_7_days": real_dir is not None and final == real_dir,
        }
        passed = all(checks.values())
        results.append({"n": i, "date": ev["date"], "asset": ev["asset"], "headline": ev["headline"],
                        "expected_factor": ev.get("expected_factor"), "factor": fid,
                        "factor_name": f["name"] if f else None, "alert": alert,
                        "rules": rules["direction"], "rules_reason": rules["reason"], "similar_used": len(rules["similar"]),
                        "ai": ai_dir, "ai_by": ai_by, "ai_why": ai_why, "prediction": final,
                        "rules_right": real_dir is not None and rules["direction"] == real_dir,
                        "ai_right": None if ai_dir not in ("up", "down") or real_dir is None else ai_dir == real_dir,
                        "real": {"d1": d1, "d7": d7}, "real_direction": real_dir,
                        "checks": checks, "passed": passed})
        print(f"   {i}. {ev['date']} {ev['asset']}: {ev['headline'][:60]}\n"
              f"      factor {fid or 'none'} | {'ALERT' if alert else 'info only'} | rules {rules['direction']} | "
              f"AI {ai_dir or '-'} | final {final} | real 7 days {d7 if d7 is not None else '?'}% -> "
              f"{'PASS' if passed else 'FAIL'}")
    summary = {"fingerprint": fingerprint, "time": datetime.now(timezone.utc).isoformat(), "results": results,
               "passed": sum(r["passed"] for r in results), "total": len(results),
               "direction_right": sum(r["checks"]["right_direction_7_days"] for r in results),
               "rules_right": sum(r["rules_right"] for r in results),
               "ai_right": sum(1 for r in results if r["ai_right"]), "ai_used": sum(1 for r in results if r["ai"])}
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nBOTTOM LINE: {summary['passed']} of {summary['total']} past events passed all 3 checks "
          f"({summary['direction_right']} of {summary['total']} directions right).")
    return summary


if __name__ == "__main__":
    import sys
    run(force="--force" in sys.argv)
