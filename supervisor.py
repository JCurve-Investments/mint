"""
MINT Supervisor
===============
The Scouts find stories. The Supervisor decides what they MEAN.

For every story the Scouts kept, the Supervisor answers three questions:

  1. WHICH EVENT is this about?
     Ten websites writing about one hack is ONE event, not ten. Stories are grouped into
     events by comparing their headlines (shared names, amounts and key words).

  2. Is there ENOUGH PROOF?
       - an official source (SEC, Federal Reserve, World Gold Council...) counts on its own
       - news websites need 2 DIFFERENT organisations
       - a social media post (later) can only start a Watch

  3. Is it NEW?
       - an event MINT has already seen is never alerted twice (a new article = an update)
       - if the first report is too old, it is not news any more
       - headlines with "last week", "a year after", an old year etc. are flagged so the
         Decision agent checks the real date

Every event gets a state:
    watch      something might be happening, but there is not enough proof yet
    confirmed  enough proof - waiting for the Decision agent
    alerted    the Decision agent approved it ("stamped") and wrote the alert
    context    proven, but from a moderate factor - shown in the app, no alert
    old        the event is too old to be news
    expired    "not confirmed": a Watch that never got a second source (kept and tracked, no alert)
    rejected   the Decision agent decided it is not a real new event

Then each asset gets a state:  Calm, Watch or Alert.

Memory: the Supervisor remembers every event in data/seen_events.json
        (test runs use data/test/ so they never mix with real data).

Run on its own:  py supervisor.py          (uses the newest real Scout results)
                 py supervisor.py --test   (uses the newest TEST Scout results)
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from common import FOLDER, load_json

# ---------------------------------------------------------------------------
# Settings (all in one place so they are easy to explain and to test)
# ---------------------------------------------------------------------------
GROUP_WINDOW_HOURS = {"Bitcoin": 48, "Gold": 72}       # a story can join an event first reported up to this long before
MAX_EVENT_AGE_HOURS = {"Bitcoin": 24, "Gold": 7 * 24}   # first report older than this = not news any more
ALERT_WINDOW_HOURS = {"Bitcoin": 24, "Gold": 48}        # only events first reported within this can become an alert;
                                                        # older gold news (up to 7 days) goes into the weekly summary
WATCH_EXPIRES_HOURS = {"Bitcoin": 24, "Gold": 7 * 24}   # a Watch with no extra proof after this long is dropped
ALERT_STAYS_HOURS = {"Bitcoin": 24, "Gold": 72}         # how long an asset stays on "Alert" after an alert
SAME_EVENT_SCORE = 0.5        # how similar two headlines must be (0 = nothing shared, 1 = same words)
SAME_EVENT_SCORE_OTHER_FACTOR = 0.65   # stricter if the stories came from different factor searches
SAME_DAY_HOURS = 12           # same factor, first reported within 12 hours, 2+ shared key words = one event
                              # (e.g. the WGC's Q3 report and the WSJ story about it)

# Words that appear in almost every story, so sharing them proves nothing.
GENERIC = {
    "bitcoin", "btc", "crypto", "cryptocurrency", "cryptocurrenc", "digital", "asset", "token", "coin",
    "gold", "bullion", "precious", "metal", "price", "market", "investor", "trader", "trad",
    "fed", "federal", "reserve", "sec", "us", "u.s.", "say", "new", "report", "news", "week", "day",
    "today", "amid", "after", "over", "could", "may", "will", "first", "big", "top", "latest", "update",
    "test",
}
STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "as", "at", "by", "from", "is",
    "are", "was", "were", "be", "been", "its", "it", "this", "that", "these", "after", "into", "than",
    "but", "not", "no", "has", "have", "had", "his", "her", "their", "our", "more", "most", "what", "why",
    "how", "who", "when", "where", "which", "up", "down", "out", "about", "s", "vs", "via", "per",
}
# Different words for the same thing, so "hackers steal" and "exchange hacked" match.
SYNONYMS = {
    "hacked": "hack", "hackers": "hack", "hacker": "hack", "hacks": "hack", "exploit": "hack",
    "exploited": "hack", "breach": "hack", "heist": "hack", "stolen": "hack", "steal": "hack",
    "steals": "hack", "drained": "hack", "theft": "hack",
    "bans": "ban", "banned": "ban", "outlaw": "ban", "outlaws": "ban", "prohibit": "ban",
    "prohibits": "ban", "illegal": "ban",
    "sues": "sue", "sued": "sue", "lawsuit": "sue", "suing": "sue",
    "charges": "charge", "charged": "charge",
    "halts": "halt", "halted": "halt", "suspends": "halt", "suspended": "halt", "freezes": "halt",
    "frozen": "halt", "pauses": "halt", "paused": "halt",
    "withdrawals": "withdraw", "withdrawal": "withdraw",
    "bankruptcy": "bankrupt", "insolvent": "bankrupt", "collapses": "collapse", "collapsed": "collapse",
    "outflow": "outflows", "inflow": "inflows",
    "cuts": "cut", "hikes": "hike", "hiked": "hike", "rates": "rate",
    "buys": "buy", "buying": "buy", "bought": "buy", "purchases": "buy", "purchase": "buy",
    "slips": "fall", "falls": "fall", "drops": "fall", "slides": "fall", "tumbles": "fall", "sinks": "fall",
    "rises": "rise", "jumps": "rise", "climbs": "rise", "surges": "rise", "soars": "rise",
}
# Words in a headline that suggest it is about an OLDER event.
OLD_EVENT_PATTERNS = [
    r"\blast (week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\bearlier this (week|month|year)\b",
    r"\b(a|one|two|three) (year|years|month|months) (ago|after|on|later)\b",
    r"\banniversary\b", r"\blooking back\b", r"\brecap\b", r"\bweek in review\b", r"\bwhat happened\b",
    # follow-up wording: the story is about what happened AFTER an event, so the event itself is older
    r"\bafter (its|record|massive|huge|last|\$)", r"\bin the wake of\b", r"\baftermath\b", r"\bfallout\b",
    r"\bbattles? to\b", r"\brecover(s|ing)? from\b", r"\breimburs", r"\bcompensat", r"\bprobe into\b",
    r"\binvestigation into\b", r"\bweeks? after\b", r"\bdays? after\b", r"\bsince the\b",
]


# ---------------------------------------------------------------------------
# Where things are saved
# ---------------------------------------------------------------------------
def data_folder(test_mode):
    folder = FOLDER / "data" / ("test" if test_mode else "")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def load_memory(test_mode):
    path = data_folder(test_mode) / "seen_events.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"events": [], "counter": {}}


def save_memory(memory, test_mode):
    path = data_folder(test_mode) / "seen_events.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(memory, indent=2), encoding="utf-8")
    tmp.replace(path)          # write to a spare file first, so a crash can never leave half a file


def parse_time(text):
    return datetime.fromisoformat(text) if text else None


def hours_between(later, earlier):
    return (later - earlier).total_seconds() / 3600


# ---------------------------------------------------------------------------
# Reading headlines
# ---------------------------------------------------------------------------
def clean_title(title, story):
    """Remove 'TEST:' and the ' - Reuters' that Google News adds to the end of headlines."""
    t = re.sub(r"^\s*TEST:\s*", "", title or "").strip()
    if " | " in t and len(t.split(" | ")[0]) >= 15:      # "Headline | Post by someone | Insights" -> "Headline"
        t = t.split(" | ")[0].strip()
    m = re.search(r"\s+[-–|]\s+([^-–|]{2,45})$", t)
    if m:
        tail = m.group(1).lower()
        names = [story.get("organisation", ""), story.get("source_name", ""),
                 story.get("source_site", "").split(".")[0]]
        if any(n and (n.lower() in tail or tail in n.lower()) for n in names):
            t = t[:m.start()].strip()
    return t


def amounts(text):
    """Find money amounts like $6.3bn, 1.5 billion, 400 million. Returned in millions."""
    found = set()
    for num, unit in re.findall(r"\$?(\d+(?:[.,]\d+)?)\s*(bn|billion|b\b|m\b|mn|million|k\b|thousand)",
                                text.lower()):
        value = float(num.replace(",", "."))
        scale = {"bn": 1000, "billion": 1000, "b": 1000, "m": 1, "mn": 1, "million": 1,
                 "k": 0.001, "thousand": 0.001}[unit]
        found.add(round(value * scale, 1))
    return found


def key_words(text):
    """Turn a headline into a set of key words: lower case, no small words, same word for synonyms."""
    words = set()
    for w in re.findall(r"[a-z0-9][a-z0-9.'-]*", text.lower()):
        w = w.strip(".'-")
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        w = SYNONYMS.get(w, w)
        if w not in SYNONYMS.values():          # simple stemming: trading -> trad, payments -> payment
            for ending in ("ing", "ed", "es", "s"):
                if len(w) > 5 and w.endswith(ending):
                    w = w[: -len(ending)]
                    break
        words.add(w)
    return words


def old_event_clues(text, now):
    """Return the reasons a headline looks like it is about an older event (empty list = none)."""
    clues = [re.search(p, text.lower()).group(0) for p in OLD_EVENT_PATTERNS if re.search(p, text.lower())]
    for year in re.findall(r"\b(20\d\d)\b", text):
        if int(year) < now.year:
            clues.append(f"mentions {year}")
    if re.search(r"\bold story\b", text.lower()):         # used in the sample test data
        clues.append("old story")
    return clues


def factor_check(story, factors):
    """Is the headline really about its factor? Returns (factor id, note) - the same factor, a better-fitting
    factor of the same asset, or (None, reason) if it fits no factor. Official sources keep their factor."""
    def fits(fid):
        words = factors.get(fid, {}).get("keywords")
        if not words:
            return True                       # factors without keywords (data checks) are not checked
        text = story["clean_title"].lower()
        if story["kind"] == "social":            # a post's headline is cut short: check the whole post
            text = (story["clean_title"] + " " + story.get("summary", "")).lower()
        return any(re.search(r"\b" + re.escape(k) + r"(s|es|ed|d)?\b", text) for k in words)
    if story["kind"] in ("official", "data") or fits(story["factor"]):
        return story["factor"], ""
    for fid, f in factors.items():
        if f["asset"] == story["asset"] and f.get("keywords") and fits(fid):
            return fid, f"moved from {story['factor']} to {fid} (headline fits {f['name']} better)"
    return None, f"headline is not about any {story['asset']} factor"


def similarity(words_a, words_b):
    """How much two headlines share, from 0 to 1. Generic words like 'bitcoin' count only a little.
    Returns (score, number of shared non-generic words)."""
    weight = lambda w: 0.3 if w in GENERIC else 1.0
    shared = words_a & words_b
    smaller = min(sum(map(weight, words_a)), sum(map(weight, words_b))) or 1
    score = sum(map(weight, shared)) / smaller
    return round(score, 2), len([w for w in shared if w not in GENERIC])


# ---------------------------------------------------------------------------
# Collecting stories from the Scout reports
# ---------------------------------------------------------------------------
def source_kind(organisation, sources):
    """official / data / news / social, read from sources.json."""
    for s in sources:
        if s["organisation"] == organisation:
            t = s.get("type", "").lower()
            if "official" in t:
                return "official"
            if "data" in t:
                return "data"
            if "social" in t:
                return "social"
            return "news"
    return "news"


def collect_stories(reports, sources):
    """Take every KEPT story from the Scout reports and label it with its asset and factor."""
    stories = []
    for report in reports:
        asset = "Bitcoin" if report.get("scout") == "bitcoin" else "Gold"
        for group in report.get("news", []) + report.get("weekly_news", []):
            for s in group.get("kept", []):
                story = dict(s)
                story["asset"] = s.get("asset") or asset          # the Social Watcher sets the asset per post
                story["factor"] = group["id"]
                story["kind"] = s.get("kind") or source_kind(s.get("organisation", ""), sources)
                story["clean_title"] = clean_title(s.get("title", ""), s)
                stories.append(story)
    return stories


def newest_reports(test_mode):
    """For running the Supervisor on its own: the newest Bitcoin and Gold Scout results."""
    reports = []
    for prefix in ("btc_scout", "gold_scout", "social"):
        pattern = f"{prefix}_TEST_*.json" if test_mode else f"{prefix}_2*.json"
        files = sorted((FOLDER / "results").glob(pattern))
        if files:
            reports.append(json.loads(files[-1].read_text(encoding="utf-8")))
    return reports


# ---------------------------------------------------------------------------
# The main job
# ---------------------------------------------------------------------------
CLOSED_STATES = ("alerted", "context", "summary", "old", "expired", "rejected", "baseline")


def new_event_id(memory, asset, when):
    key = f"{'BTC' if asset == 'Bitcoin' else 'GOLD'}-{when:%Y%m%d}"
    memory["counter"][key] = memory["counter"].get(key, 0) + 1
    return f"{key}-{memory['counter'][key]:02d}"


def find_matching_event(story, events, now):
    """Find the event this story belongs to, or None if it is a new event."""
    words = set(story["words"])
    best, best_score, best_why = None, 0, ""
    published = parse_time(story.get("published")) or now
    for ev in events:
        if ev["asset"] != story["asset"]:
            continue
        if abs(hours_between(published, parse_time(ev["earliest_report"]))) > GROUP_WINDOW_HOURS[ev["asset"]]:
            continue
        same_amount = bool(set(story["amounts"]) & set(ev["amounts"]))
        score, shared = similarity(words, set(ev["words"]))
        needed = SAME_EVENT_SCORE if story["factor"] == ev["factor"] else SAME_EVENT_SCORE_OTHER_FACTOR
        same_day = abs(hours_between(published, parse_time(ev["earliest_report"]))) <= SAME_DAY_HOURS
        if same_amount and shared >= 1:
            score, why = max(score, 0.9), "same amount of money and shared key words"
        elif score >= needed and shared >= 2:
            why = f"headlines share key words ({score:.0%} match)"
        elif story["factor"] == ev["factor"] and same_day and shared >= 2:
            score, why = max(score, needed), "same factor, same day and shared key words"
        elif story["factor"] == ev["factor"] and same_day and shared >= 1 and \
                story.get("organisations", [story.get("organisation")]) and \
                set(story.get("organisations", [story.get("organisation")])) & {x["organisation"] for x in ev["sources"]}:
            score, why = max(score, needed), "same organisation, same factor and same day"
        else:
            continue
        if score > best_score:
            best, best_score, best_why = ev, score, why
    return best, best_why


def not_news_title(title):
    """True for website sections and guides ("Investment", "Gold Market Primer") - not news headlines."""
    return len(title.split()) < 4 or bool(re.search(r"\b(primer|glossary|about us|faq)\b", title.lower()))


def tidy_memory(memory, now, say):
    """Clean up events saved by older versions of MINT:
    1. remove 'events' that are really website sections (they were never news), and
    2. join events that are the same story (same factor, same day, shared key words) into the first one,
       so one story is never counted as several predictions."""
    removed = memory.setdefault("removed", [])
    for ev in list(memory["events"]):
        if all(src["kind"] != "social" for src in ev["sources"]) and not_news_title(ev["title"]):
            memory["events"].remove(ev)
            removed.append({"id": ev["id"], "title": ev["title"], "removed_at": now.isoformat(),
                            "reason": "a website section, not a news headline"})
            say(f"REMOVED     [{ev['id']}] {ev['title']} - a website section, not a news headline")
    ordered = sorted(memory["events"], key=lambda e: e["earliest_report"])
    for i, later in enumerate(ordered):
        if later not in memory["events"]:
            continue
        probe = {"asset": later["asset"], "factor": later["factor"], "words": later["words"],
                 "amounts": later["amounts"], "published": later["earliest_report"],
                 "organisations": [x["organisation"] for x in later["sources"]]}
        earlier = [e for e in ordered[:i] if e in memory["events"]]
        ev, why = find_matching_event(probe, earlier, now)
        if ev is None:
            continue
        for src in later["sources"]:
            add_story(ev, {**src, "clean_title": src["title"], "source_site": src.get("site", ""),
                           "words": sorted(key_words(src["title"])), "amounts": sorted(amounts(src["title"])),
                           "old_clues": []}, now)
        note(ev, now, f"joined with {later['id']} ({why}) - the same story")
        memory["events"].remove(later)
        removed.append({"id": later["id"], "title": later["title"], "removed_at": now.isoformat(),
                        "reason": f"the same story as {ev['id']} ({why})"})
        say(f"JOINED      [{later['id']}] into [{ev['id']}] - the same story ({why})")


def proof_level(ev):
    """official / two_news / one_news / social_only - and a plain-English explanation."""
    kinds = {s["kind"] for s in ev["sources"]}
    news_orgs = sorted({s["organisation"] for s in ev["sources"] if s["kind"] == "news"})
    if kinds & {"official", "data"}:
        org = next(s["organisation"] for s in ev["sources"] if s["kind"] in ("official", "data"))
        return "official", f"official source: {org}"
    if len(news_orgs) >= 2:
        return "two_news", f"{len(news_orgs)} different news organisations: {', '.join(news_orgs)}"
    if news_orgs:
        return "one_news", f"only 1 news organisation so far ({news_orgs[0]}) - waiting for a second"
    return "social_only", "only a social media post - waiting for a news or official source"


def note(ev, now, text):
    ev["history"].append({"time": now.isoformat(), "note": text})


def add_story(ev, story, now):
    """Add a story to an event. Returns True if it was really new (not the same link or the same
    organisation repeating itself)."""
    for s in ev["sources"]:
        if s["link"] and s["link"] == story.get("link"):
            return False
        if s["organisation"] == story.get("organisation") and s["title"] == story["clean_title"]:
            return False
    ev["sources"].append({
        "organisation": story.get("organisation", "?"), "kind": story["kind"], "site": story.get("source_site", ""),
        "title": story["clean_title"], "link": story.get("link", ""), "published": story.get("published"),
        "found_at": now.isoformat(), "summary": story.get("summary", ""), "factor": story["factor"],
    })
    ev["words"] = sorted(set(ev["words"]) | set(story["words"]))
    ev["amounts"] = sorted(set(ev["amounts"]) | set(story["amounts"]))
    published = story.get("published") or now.isoformat()
    if published < ev["earliest_report"]:
        ev["earliest_report"] = published
    ev["old_clues"] = sorted(set(ev["old_clues"]) | set(story["old_clues"]))
    ev["last_updated"] = now.isoformat()
    return True


def best_title(ev):
    """The headline shown for the event: the earliest official one, otherwise the earliest one."""
    ranked = sorted(ev["sources"], key=lambda s: (s["kind"] not in ("official", "data"), s["published"] or ""))
    return ranked[0]["title"]


def update_state(ev, factors, now):
    """Decide the state of an event that is still open (watch or confirmed)."""
    factor = factors.get(ev["factor"], {})
    proof, why = proof_level(ev)
    ev["proof"], ev["proof_reason"] = proof, why
    age = hours_between(now, parse_time(ev["earliest_report"]))
    old_state = ev["state"]

    proven = proof in ("official", "two_news")
    window = ALERT_WINDOW_HOURS[ev["asset"]]
    if age > window and age <= MAX_EVENT_AGE_HOURS[ev["asset"]]:
        ev["state"] = "summary"
        ev["state_reason"] = (f"first reported {age:.0f} hours ago - older than the {window}-hour alert window, "
                              f"so it goes in the weekly summary ({why})")
    elif proven and age > MAX_EVENT_AGE_HOURS[ev["asset"]]:
        ev["state"] = "old"
        ev["state_reason"] = f"first reported {age:.0f} hours ago - too old to be news"
    elif not proven and (age > MAX_EVENT_AGE_HOURS[ev["asset"]] or
                         hours_between(now, parse_time(ev["first_seen"])) > WATCH_EXPIRES_HOURS[ev["asset"]]):
        ev["state"] = "expired"
        ev["state_reason"] = "only one source, and no second source came in time - not confirmed (still tracked, no alert)"
    elif factor.get("strength") != "strong":
        ev["state"] = "context"
        ev["state_reason"] = (f"{factor.get('name', ev['factor'])} is a moderate factor - shown for context, "
                              f"never an alert ({why})")
    elif proven:
        if factor.get("strength") == "strong":
            ev["state"] = "confirmed"
            ev["state_reason"] = f"enough proof ({why}) - waiting for the Decision agent"
            if ev["old_clues"]:
                ev["state_reason"] += f"; the Decision agent must check the date ({', '.join(ev['old_clues'])})"
        else:
            ev["state"] = "context"
            ev["state_reason"] = (f"enough proof ({why}), but {factor.get('name', ev['factor'])} is a moderate "
                                  "factor - shown for context, no alert")
    else:
        ev["state"] = "watch"
        ev["state_reason"] = why
        if ev["old_clues"]:
            ev["state_reason"] += f"; headline may be about an older event ({', '.join(ev['old_clues'])})"

    if ev["state"] != old_state:
        note(ev, now, f"{old_state or 'new'} -> {ev['state']}: {ev['state_reason']}")
    return old_state != ev["state"]


def asset_states(memory, reports, now):
    """Calm, Watch or Alert for each asset, with the reasons in plain English."""
    result = {}
    for asset in ("Bitcoin", "Gold"):
        reasons_alert, reasons_watch = [], []
        for ev in memory["events"]:
            if ev["asset"] != asset:
                continue
            decided = parse_time((ev.get("decision") or {}).get("time"))
            if ev["state"] == "alerted" and decided and hours_between(now, decided) <= ALERT_STAYS_HOURS[asset]:
                reasons_alert.append(f"Alert: {ev['title']}")
            elif ev["state"] in ("watch", "confirmed"):
                reasons_watch.append(f"Watching: {ev['title']} ({ev['proof_reason']})")
        for report in reports:
            if asset == "Bitcoin" and report.get("scout") == "bitcoin":
                checks = report.get("data_checks", {})
                b3, b5 = checks.get("B3", {}), checks.get("B5", {})
                if b3.get("warning"):
                    reasons_watch.append(f"US stock market (Nasdaq) fell {b3['change_percent']}% on its last day")
                if b5.get("warning"):
                    reasons_watch.append(f"Interest in Bitcoin jumped ({b5['ratio']}x last week's Wikipedia views)")
            if asset == "Gold" and report.get("scout") == "gold":
                for w in report.get("calendar", {}).get("warnings", []):
                    reasons_watch.append(w)
        failed, total = 0, 0
        for report in reports:
            if report.get("scout") not in ("bitcoin", "gold") or (asset == "Bitcoin") != (report.get("scout") == "bitcoin"):
                continue
            groups = report.get("news", []) + report.get("weekly_news", [])
            total += len(groups) + len(report.get("data_checks", {}))
            failed += sum(1 for g in groups if g.get("error"))
            failed += sum(1 for v in report.get("data_checks", {}).values() if "error" in v)
            failed += sum(1 for n in report.get("calendar", {}).get("notes", []) if "FAILED" in n)
        if failed:
            reasons_watch.append(f"{failed} check(s) failed in the last run (internet problem?) - see Agents")
        if reasons_alert:
            state = "Alert"
        elif total and failed >= total:
            state = "Unknown"
        elif reasons_watch:
            state = "Watch"
        else:
            state = "Calm"
        result[asset] = {"state": state, "reasons": reasons_alert + reasons_watch or ["Nothing important found."]}
    return result


def run(reports=None, test_mode=False, now=None, learn_first_run=None):
    """The Supervisor's job. Returns a summary (also saved in data/supervisor_last.json)."""
    now = now or datetime.now(timezone.utc)
    reports = reports if reports is not None else newest_reports(test_mode)
    sources = load_json("sources.json")["sources"]
    factors = {f["id"]: f for f in load_json("factors.json")["factors"]}
    memory = load_memory(test_mode)
    lines = []
    say = lambda text: (lines.append(text), print(text))
    # FIRST RUN: MINT has no memory yet, so it cannot tell new events from ones that were already in the news.
    # The first real run only LEARNS what is already out there (no alerts). From the second run on, only
    # events MINT has never seen before can become alerts.
    if learn_first_run is None:
        learn_first_run = False      # switched off: everything found is reported (old news is still caught by the
                                     # date check and the 24 / 48-hour alert window)
    for ev in memory["events"]:      # events from the old first-run learning are checked normally again
        if ev["state"] == "baseline":
            ev["state"] = None
            note(ev, now, "re-checked: first-run learning is switched off")
    learning = learn_first_run and not memory["events"] and not memory.get("learned_at")
    tidy_lines = []
    tidy_memory(memory, now, tidy_lines.append)

    say(f"MINT SUPERVISOR  {now:%Y-%m-%d %H:%M} UTC{'  (TEST MODE)' if test_mode else ''}")
    stories = collect_stories(reports, sources)
    say(f"Read {len(reports)} Scout report(s): {len(stories)} kept stories to check.")
    if learning:
        say("FIRST RUN: MINT has no memory yet, so this run only learns what is already in the news. No alerts.")
    for t in tidy_lines:
        say(t)
    say("")

    checked, off_topic = [], 0
    for s in stories:
        words = s["clean_title"].split()
        if s["kind"] != "social" and not_news_title(s["clean_title"]):
            off_topic += 1                       # e.g. "Investment" - a website section, not a news headline
            say(f"OFF-TOPIC   {s['clean_title']}  ({s.get('organisation')}) - not a news headline")
            continue
        fid, why = factor_check(s, factors)
        if fid is None:
            off_topic += 1
            say(f"OFF-TOPIC   {s['clean_title']}  ({s.get('organisation')}) - {why}")
            continue
        if fid != s["factor"]:
            say(f"RE-FILED    {s['clean_title']} - {why}")
            s["factor"] = fid
        checked.append(s)
    stories = checked

    for s in stories:
        s["words"] = sorted(key_words(s["clean_title"]))
        s["amounts"] = sorted(amounts(s["clean_title"]))
        s["old_clues"] = old_event_clues(s["clean_title"], now)
    stories.sort(key=lambda s: s.get("published") or "")     # oldest first, so the first report starts the event

    counts = {"stories": len(stories) + off_topic, "off_topic": off_topic, "repeats": 0, "new_events": 0,
              "joined": 0, "updates_ignored": 0}
    touched = []
    for s in stories:
        ev, why = find_matching_event(s, memory["events"], now)
        if ev is None:
            when = parse_time(s.get("published")) or now
            ev = {
                "id": new_event_id(memory, s["asset"], when), "asset": s["asset"], "factor": s["factor"],
                "factor_name": factors.get(s["factor"], {}).get("name", s["factor"]),
                "title": s["clean_title"], "state": None, "state_reason": "", "proof": "", "proof_reason": "",
                "first_seen": now.isoformat(), "earliest_report": s.get("published") or now.isoformat(),
                "last_updated": now.isoformat(), "words": [], "amounts": [], "old_clues": [],
                "sources": [], "history": [], "decision": None, "outcome": {},
            }
            add_story(ev, s, now)
            memory["events"].append(ev)
            note(ev, now, f"new event, first report by {s.get('organisation')}")
            counts["new_events"] += 1
            say(f"NEW EVENT   [{ev['id']}] {s['clean_title']}  ({s.get('organisation')})")
            touched.append(ev)
            continue

        if not add_story(ev, s, now):
            counts["repeats"] += 1
            continue                                   # the same article as before - nothing to do
        if ev["state"] in CLOSED_STATES:
            counts["updates_ignored"] += 1
            note(ev, now, f"another report from {s.get('organisation')} - same event, no new alert")
            say(f"SAME EVENT  [{ev['id']}] {s['clean_title']}  ({s.get('organisation')}) - already {ev['state']}, "
                "no new alert")
        else:
            counts["joined"] += 1
            note(ev, now, f"another report from {s.get('organisation')} ({why})")
            say(f"JOINED      [{ev['id']}] {s['clean_title']}  ({s.get('organisation')}) - {why}")
        touched.append(ev)

    say("")
    changes = {"confirmed": 0, "context": 0, "summary": 0, "old": 0, "expired": 0, "watch": 0}
    library_entries = None
    for ev in memory["events"]:
        if learning and ev["state"] is None:
            ev["title"] = best_title(ev)
            ev["proof"], ev["proof_reason"] = proof_level(ev)
            ev["state"] = "baseline"
            ev["state_reason"] = "already in the news when MINT first started - remembered, no alert"
            note(ev, now, "new -> baseline: " + ev["state_reason"])
            continue
        # PREDICTION: made from similar past events + the factor pattern. It is kept up to date while the event
        # is open, then FROZEN once the 1-day price is known (so MINT can never change a prediction after the fact).
        pred = ev.get("prediction") or {}
        frozen = ((ev.get("outcome") or {}).get("d1") is not None) and pred
        old_style = pred.get("by") in ("factor rules", None)
        if not frozen and not str(pred.get("by", "")).startswith("AI") and \
                (ev["state"] in (None, "watch", "confirmed") or not pred or old_style or pred.get("direction") == "unclear"):
            if ev["state"] in (None, "watch", "confirmed"):
                ev["title"] = best_title(ev)
            import library
            if library_entries is None:
                library_entries = library.all_entries(test_mode, memory)
            ev["prediction"] = library.make_prediction(ev["factor"], ev["title"], ev["asset"], library_entries,
                                                       exclude_id=ev["id"], before=ev["earliest_report"][:10])
        if ev["state"] in (None, "watch", "confirmed"):
            ev["title"] = best_title(ev)
            if update_state(ev, factors, now) and ev["state"] in changes:
                changes[ev["state"]] += 1
                say(f"{ev['state'].upper():<11} [{ev['id']}] {ev['title']}\n            -> {ev['state_reason']}")

    states = asset_states(memory, reports, now)
    say("")
    for asset, st in states.items():
        say(f"{asset}: {st['state'].upper()}")
        for r in st["reasons"]:
            say(f"   - {r}")

    open_events = [e for e in memory["events"] if e["state"] in ("watch", "confirmed")]
    summary = {
        "run_time": now.isoformat(), "test_mode": test_mode, **counts,
        "changes": changes, "open_events": len(open_events), "asset_states": states,
    }
    say(f"\nBOTTOM LINE: {counts['stories']} stories ({off_topic} off-topic) -> {counts['new_events']} new event(s), "
        f"{counts['joined']} joined an event, {counts['updates_ignored']} update(s) to old events (no new alert), "
        f"{counts['repeats']} repeat(s). {changes['confirmed']} event(s) passed to the Decision agent.")

    if learning:
        memory["learned_at"] = now.isoformat()
    memory["last_run"] = summary
    save_memory(memory, test_mode)
    (data_folder(test_mode) / "supervisor_last.json").write_text(
        json.dumps({**summary, "output": "\n".join(lines)}, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv,
        now=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc) if "--test" in sys.argv else None)
