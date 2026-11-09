"""
Tests for the Supervisor: 24 situations it must handle correctly.
Every test uses made-up stories and a separate memory folder (data/selftest), so real data is never touched.

Run:  py test_supervisor.py      (or press "Test the Supervisor" in the MINT app)
"""

import contextlib
import io
import shutil
from datetime import datetime, timezone, timedelta

import supervisor
from common import FOLDER

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
results = []


def fresh_memory():
    folder = FOLDER / "data" / "selftest"
    shutil.rmtree(folder, ignore_errors=True)
    supervisor.data_folder = lambda test_mode: (folder.mkdir(parents=True, exist_ok=True), folder)[1]


def story(title, org, site, hours_ago, now=T0, factor="B2", link=None, summary=""):
    return {"title": title, "organisation": org, "source_site": site, "source_name": org,
            "link": link or f"https://{site}/{abs(hash(title))}", "summary": summary,
            "published": (now - timedelta(hours=hours_ago)).isoformat(), "age_hours": hours_ago}


def btc_report(stories_by_factor):
    return {"scout": "bitcoin", "news": [{"id": f, "kept": s} for f, s in stories_by_factor.items()],
            "data_checks": {}}


def events():
    return supervisor.load_memory(True)["events"]


def check(name, condition, detail=""):
    results.append((name, condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"   ({detail})" if detail and not condition else ""))


def quiet_run(reports, now):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return supervisor.run(reports, test_mode=True, now=now)


# 1. Two organisations, same hack, different words -> ONE event, confirmed
fresh_memory()
quiet_run([btc_report({"B2": [
    story("Bybit hacked for $1.5bn in largest crypto heist - Reuters", "Reuters", "reuters.com", 3),
    story("Hackers steal $1.5 billion from crypto exchange Bybit", "CNBC", "cnbc.com", 2)]})], T0)
e = events()
check("1. Two organisations, same hack -> one event", len(e) == 1, f"{len(e)} events")
check("2. Two organisations -> confirmed (passed to Decision agent)", e and e[0]["state"] == "confirmed",
      e and e[0]["state"])
check("3. ' - Reuters' removed from the headline", e and not e[0]["title"].endswith("Reuters"), e and e[0]["title"])

# 4. A different event from the same factor must NOT be joined to it
quiet_run([btc_report({"B2": [story("Exchange Kraken suffers outage, users cannot trade", "Bloomberg", "bloomberg.com", 1)]})], T0)
check("4. Different event, same factor -> kept separate", len(events()) == 2, f"{len(events())} events")

# 5. Old event, new article: once alerted, a new article the next day is an UPDATE, not a new alert
mem = supervisor.load_memory(True)
mem["events"][0]["state"] = "alerted"
mem["events"][0]["decision"] = {"time": T0.isoformat(), "send_alert": True}
supervisor.save_memory(mem, True)
later = T0 + timedelta(hours=30)
summary = quiet_run([btc_report({"B2": [story("Bybit hack: how $1.5bn vanished in minutes", "BBC", "bbc.com", 2,
                                              now=later)]})], later)
check("5. New article about an alerted event -> update, no new alert",
      summary["updates_ignored"] == 1 and summary["new_events"] == 0, str(summary))

# 6. ONE reliable (approved) news organisation is enough - no second source needed
fresh_memory()
quiet_run([btc_report({"B1": [
    story("Country Z bans bitcoin mining", "Reuters", "reuters.com", 3, factor="B1"),
    story("Country Z bans bitcoin mining, citing power shortages", "Reuters", "reuters.com", 1, factor="B1")]})], T0)
e = events()
check("6. One approved news organisation alone -> confirmed (no second source needed)",
      len(e) == 1 and e[0]["state"] == "confirmed", e and e[0]["state"])

# 7. A post from an approved social media account is confirmed on its own (labelled as social media)
fresh_memory()
post = {**story("Country Q says it will ban bitcoin trading next month", "Some Account (Telegram)", "someaccount", 1,
                factor="B1"), "kind": "social", "asset": "Bitcoin"}
quiet_run([{"scout": "social", "news": [{"id": "B1", "kept": [post]}]}], T0)
e = events()
check("7. Approved social media account alone -> confirmed, and marked as social media",
      e and e[0]["state"] == "confirmed" and e[0]["proof"] == "social" and "social media" in e[0]["proof_reason"],
      e and f"{e[0]['state']} / {e[0]['proof']}")

# 8. An official source counts on its own
fresh_memory()
quiet_run([btc_report({"B1": [story("SEC sues crypto exchange Acme for operating unregistered platform",
                                     "US Securities and Exchange Commission", "sec.gov", 1, factor="B1")]})], T0)
e = events()
check("8. Official source (SEC) alone -> confirmed", e and e[0]["state"] == "confirmed", e and e[0]["state"])

# 9. A headline about an older event is flagged for the date check
fresh_memory()
quiet_run([btc_report({"B2": [
    story("A year after FTX collapse, exchange Gamma halts withdrawals", "Reuters", "reuters.com", 2),
    story("Exchange Gamma halts withdrawals as FTX memories linger", "BBC", "bbc.com", 1)]})], T0)
e = events()
check("9. 'A year after...' -> flagged for the Decision agent's date check", e and bool(e[0]["old_clues"]),
      e and str(e[0]["old_clues"]))

# 10. First report too old: the Scout found an article, but the event was first reported 30h ago
fresh_memory()
quiet_run([btc_report({"B2": [story("Exchange Delta hacked for $200m", "Reuters", "reuters.com", 30)]})], T0)
check("10. Event first reported 30h ago (Bitcoin limit 24h) -> old, no alert",
      events() and events()[0]["state"] in ("old", "summary"), events() and events()[0]["state"])

# 11. A headline that is not about its factor is dropped (Google returned it, but it is about politics)
fresh_memory()
summary = quiet_run([btc_report({"B2": [story("Bitcoin slips as Democratic sweep bets threaten crypto agenda",
                                              "Bloomberg", "bloomberg.com", 1)]})], T0)
check("11. Headline not about its factor -> dropped as off-topic", summary["off_topic"] == 1 and not events(),
      str(summary.get("off_topic")))

# 12. A headline that fits another factor better is moved there
fresh_memory()
gold = {"scout": "gold", "calendar": {}, "weekly_news": [{"id": "G2", "kept": [
    story("Gold gains 3% as dollar slides after midterm results", "Reuters", "reuters.com", 2, factor="G2")]}]}
quiet_run([gold], T0)
check("12. 'Gold gains as dollar slides' filed under G2 -> moved to G1 US dollar",
      events() and events()[0]["factor"] == "G1", events() and events()[0]["factor"])

# 13. Follow-up wording ("battles to save its brand after record hack") is flagged for the date check
fresh_memory()
quiet_run([btc_report({"B2": [
    story("Binance battles to save its brand after record $6.3bn crypto hack", "BBC", "bbc.com", 2),
    story("Binance fights to win back users after $6.3bn hack", "Reuters", "reuters.com", 1)]})], T0)
check("13. 'battles to ... after record hack' -> flagged as a probably older event",
      events() and len(events()[0]["old_clues"]) >= 1, events() and str(events()[0]["old_clues"]))

# 14. Gold news that is 3 days old goes into the weekly summary, never an alert
fresh_memory()
gold = {"scout": "gold", "calendar": {}, "weekly_news": [{"id": "G2", "kept": [
    story("Gold slides as Fed signals higher rates for longer", "Reuters", "reuters.com", 72, factor="G2"),
    story("Gold falls as Fed signals rates stay high", "CNBC", "cnbc.com", 70, factor="G2")]}]}
quiet_run([gold], T0)
check("14. Proven gold event first reported 72h ago -> weekly summary, not an alert",
      events() and events()[0]["state"] == "summary", events() and events()[0]["state"])

# 15. The very FIRST run reports everything it finds normally (nothing is hidden as "already in the news")
fresh_memory()
with contextlib.redirect_stdout(io.StringIO()):
    supervisor.run([btc_report({"B2": [story("Exchange Omega hacked for $90m", "Reuters", "reuters.com", 3),
                                       story("Hackers drain $90m from exchange Omega", "BBC", "bbc.com", 2)]})],
                   test_mode=True, now=T0)
check("15. First run: a proven new event is reported normally (confirmed), not hidden",
      events() and events()[0]["state"] == "confirmed", events() and events()[0]["state"])

# 16. A social media post from an approved account goes to the Decision agent without a second source
fresh_memory()
post = story("Binance has suspended withdrawals while we investigate a security incident",
             "Binance announcements (Telegram)", "binance_announcements", 1)
post.update(kind="social", asset="Bitcoin")
quiet_run([{"scout": "social", "news": [{"id": "B2", "kept": [post]}]}], T0)
check("16. Approved social media post alone -> confirmed (passed to the Decision agent)",
      events() and events()[0]["state"] == "confirmed", events() and events()[0]["state"])

# 17-20. Predictions come from what happened after similar PAST events (+ the factor pattern)
import library
past = [
    {"id": "P1", "date": "2026-03-18", "asset": "Gold", "factor": "G2", "title": "Fed signals no rate cut this year", "d1": -0.8},
    {"id": "P2", "date": "2026-06-17", "asset": "Gold", "factor": "G2", "title": "Fed holds rates, says no cut until inflation falls", "d1": -0.4},
    {"id": "P3", "date": "2026-09-16", "asset": "Gold", "factor": "G2", "title": "Fed cuts rates by a quarter point", "d1": 0.9},
    {"id": "P4", "date": "2026-12-01", "asset": "Gold", "factor": "G2", "title": "Fed holds rates as traders look to next decision", "d1": 5.0},
]
p = library.make_prediction("G2", "Gold holds above $4,000 as traders look to Fed rate decision", "Gold", past,
                            before="2026-11-01")
check("17. A headline with no direction is predicted from similar past Fed events (gold fell after 2 of 3)",
      p["direction"] == "down" and len(p["similar"]) == 3, p["reason"])
check("18. A past event from AFTER the new event is never used (no peeking into the future)",
      all(x["id"] != "P4" for x in p["similar"]), str([x["id"] for x in p["similar"]]))
p2 = library.make_prediction("G2", "Fed must cut interest rates now", "Gold", past, before="2026-11-01")
check("19. An opposite kind of event is not counted as similar ('cut' is not like 'no cut')",
      [x["id"] for x in p2["similar"]] == ["P3"] and p2["direction"] == "up", str([x["id"] for x in p2["similar"]]))
p3 = library.make_prediction("G1", "Gold steady as traders watch the dollar", "Gold", [], before="2026-11-01")
check("20. No similar past events and no pattern -> 'unclear' (MINT does not guess)", p3["direction"] == "unclear",
      p3["direction"])

# 21. The same story from two organisations on the same day, in different words -> ONE event
fresh_memory()
quiet_run([{"scout": "gold", "news": [{"id": "G4", "kept": [
    story("Central Banks Accelerated Gold Buying in Third Quarter", "Wall Street Journal", "wsj.com", 3, factor="G4"),
    story("Central bank buying and ETF inflows support gold demand in Q3", "Reuters", "reuters.com", 2,
          factor="G4")]}]}], T0)
check("21. Same story, same day, different words (WSJ + Reuters on Q3 central bank buying) -> one event",
      len(events()) == 1, f"{len(events())} events")

# 22. Old memory cleaned up: a website section saved by an older version is removed, duplicates are joined
fresh_memory()
old_memory = supervisor.load_memory(True)
for i, title in enumerate(["Investment", "Gold Demand Trends: Q3 2026", "Gold demand trends show record Q3 demand"]):
    old_memory["events"].append({
        "id": f"GOLD-OLD-{i}", "asset": "Gold", "factor": "G4", "factor_name": "Central banks", "title": title,
        "state": "summary", "state_reason": "", "proof": "", "proof_reason": "", "first_seen": T0.isoformat(),
        "earliest_report": (T0 - timedelta(hours=30 - i)).isoformat(), "last_updated": T0.isoformat(),
        "words": sorted(supervisor.key_words(title)), "amounts": [], "old_clues": [], "history": [],
        "sources": [{"organisation": "World Gold Council", "kind": "data", "site": "gold.org", "title": title,
                     "link": f"https://gold.org/{i}", "published": None, "found_at": T0.isoformat(),
                     "summary": "", "factor": "G4"}], "decision": None, "outcome": {}})
supervisor.save_memory(old_memory, True)
quiet_run([], T0)
check("22. Old memory: website section removed, same-day duplicate joined (1 event left)",
      [e["id"] for e in events()] == ["GOLD-OLD-1"], str([e["id"] for e in events()]))

# 23. Judging over 9 price checks: clear moves only, the bar grows with time, majority decides
import prices
o = {}
for k, move in {"h1": -0.5, "h5": -0.2, "h10": -1.2, "d1": -2.0}.items():      # Bitcoin, predicted DOWN
    o[k] = {"move": move, "verdict": prices.cell_verdict(move, "down", "Bitcoin", prices.CHECKPOINTS[k])}
effect, why = prices.judge(o, "Bitcoin", "down")
check("23. Price checks: -0.5% after 1h counts (bar 0.3%), -0.2% after 5h is too small (bar 0.7%); 3 of 3 -> right, not final",
      [o[k]["verdict"] for k in ("h1", "h5", "h10", "d1")] == ["right", "flat", "right", "right"]
      and effect == "right" and o["checks"]["right"] == 3 and not o["final"], f"{effect}: {why}")
o["d2"] = {"move": 3.0, "verdict": prices.cell_verdict(3.0, "down", "Bitcoin", 48)}
o["d7"] = {"move": 5.0, "verdict": prices.cell_verdict(5.0, "down", "Bitcoin", 168)}
o["d5"] = {"move": 4.0, "verdict": prices.cell_verdict(4.0, "down", "Bitcoin", 120)}
o["h84"] = {"move": 3.5, "verdict": prices.cell_verdict(3.5, "down", "Bitcoin", 84)}
effect, why = prices.judge(o, "Bitcoin", "down")
check("24. Later checks go the other way (4 wrong vs 3 right) -> wrong, and final after 7 days",
      effect == "wrong" and o["final"], f"{effect}: {why}")

passed = sum(ok for _, ok in results)
print(f"\nRESULT: {passed} of {len(results)} tests passed.")
shutil.rmtree(FOLDER / "data" / "selftest", ignore_errors=True)
