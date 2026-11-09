"""
MINT Gold Scout

Job 1 - Calendar (G2 Fed decisions, G3 US jobs and inflation reports), every run:
    reads the official calendars itself (US Federal Reserve and US Bureau of Labor Statistics)
    and lists any of these events in the next 7 days. If one is TOMORROW, it gives a warning.
    If an official site can't be read, it uses backup_calendar.json instead (and says so).
Job 2 - Weekly summary (G1 dollar, G2 rates, G4 central banks, G5 gold ETF flows), with --weekly:
    searches the news from the last 7 days, keeping only approved websites.

Run:  python gold_scout.py                  (calendar only)
      python gold_scout.py --weekly         (calendar + weekly news)
      add --test to either one to use saved sample data (no internet)
"""

import re
import sys
from datetime import datetime, date, timezone, timedelta
from html import unescape

from common import FOLDER, load_json, fetch_page, search_news, print_stories, print_news_summary, save_results

LOOK_AHEAD_DAYS = 7
WEEKLY_MAX_AGE_HOURS = 7 * 24
TEST_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def page_to_lines(html):
    """Remove the HTML code from a web page and return its visible text, one piece per line."""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)</?(strong|b|em|i|span|a)\b[^>]*>", "", html)  # bold/links stay on the same line
    text = unescape(re.sub(r"(?s)<[^>]+>", "\n", html))
    return [line.strip() for line in text.split("\n") if line.strip()]


def read_fed_calendar(html):
    """Find Fed meeting dates. The page shows '2026 FOMC Meetings', then a month, then days like '8-9*'.
    The decision is announced on the LAST day of the meeting."""
    events, year, months = [], None, None
    for line in page_to_lines(html):
        heading = re.match(r"^(\d{4}) FOMC Meetings$", line)
        if heading:
            year = int(heading.group(1))
            continue
        names = [m for m in MONTHS if m in line.split("/") or line.startswith(m)]
        if year and names and len(line) <= 20:
            months = names                     # e.g. ['December'] or ['April', 'May']
            continue
        days = re.match(r"^(\d{1,2})-(\d{1,2})\*?$", line)
        if year and months and days:
            first, last = int(days.group(1)), int(days.group(2))
            month = months[-1] if (last < first and len(months) > 1) else months[0]
            events.append({"date": date(year, MONTHS.index(month) + 1, last).isoformat(),
                           "factor": "G2", "event": "Fed interest-rate decision"})
            months = None
    return events


def read_bls_calendar(html):
    """Find US jobs and inflation report dates. The page shows a date like 'Friday, November 6, 2026'
    and, in the same row, 'Employment Situation for October 2026'."""
    events, last_date = [], None
    for line in page_to_lines(html):
        d = re.match(r"^\w+day, (\w+) (\d{1,2}), (\d{4})$", line)
        if d and d.group(1) in MONTHS:
            last_date = date(int(d.group(3)), MONTHS.index(d.group(1)) + 1, int(d.group(2)))
            continue
        if last_date and line.startswith("Employment Situation for"):
            events.append({"date": last_date.isoformat(), "factor": "G3", "event": "US jobs report"})
        if last_date and line.startswith("Consumer Price Index for"):
            events.append({"date": last_date.isoformat(), "factor": "G3", "event": "US inflation report (CPI)"})
    return events


def get_calendar(today, test_mode):
    """Read both official calendars. Use the backup file for any that fails."""
    events, notes = [], []
    backup = load_json("backup_calendar.json")["events"]

    try:
        html = (FOLDER / "test_data" / "sample_fed.html").read_text() if test_mode else \
            fetch_page("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
        fed = read_fed_calendar(html)
        if not fed:
            raise RuntimeError("no meeting dates found on the page")
        events += fed
        notes.append(f"Fed calendar read from {'sample file' if test_mode else 'official site'} ({len(fed)} meetings found)")
    except Exception as e:
        events += [ev for ev in backup if ev["factor"] == "G2"]
        notes.append(f"Fed calendar FAILED ({e}) - used backup file")

    try:
        bls = []
        for year in (today.year, today.year + 1):
            html = (FOLDER / "test_data" / "sample_bls.html").read_text() if test_mode else \
                fetch_page(f"https://www.bls.gov/schedule/{year}/home.htm")
            bls += read_bls_calendar(html)
            if test_mode:
                break
        if not bls:
            raise RuntimeError("no release dates found on the page")
        events += bls
        notes.append(f"Jobs/inflation calendar read from {'sample file' if test_mode else 'official site'} ({len(bls)} releases found)")
    except Exception as e:
        events += [ev for ev in backup if ev["factor"] == "G3"]
        notes.append(f"Jobs/inflation calendar FAILED ({e}) - used backup file")

    return events, notes


def print_summary(report, weekly):
    """A short summary at the very bottom, so you can see at a glance what the Scout did."""
    print("\n" + "=" * 60)
    print(f"SUMMARY - Gold Scout  ({report['run_time'][:16].replace('T', ' ')} UTC)")
    print("=" * 60)
    cal = report["calendar"]
    print("Calendar:")
    for n in cal["notes"]:
        print(f"   {n}")
    if cal["upcoming"]:
        for d, name, factor in cal["upcoming"]:
            print(f"   Coming up: {d}  {name} ({factor})")
    else:
        print(f"   No scheduled gold events in the next {LOOK_AHEAD_DAYS} days.")
    for w in cal["warnings"]:
        print(f"   WARNING: {w}")

    stories = 0
    if weekly:
        print("\nWeekly news (last 7 days, approved websites only):")
        print_news_summary(report["weekly_news"])
        stories = sum(len(x["kept"]) for x in report["weekly_news"])
    else:
        print("\nWeekly news: not run (add --weekly to include it)")

    errors = sum(1 for n in cal["notes"] if "FAILED" in n) + sum(1 for x in report["weekly_news"] if x["error"])
    print(f"\nBOTTOM LINE: {len(cal['warnings'])} calendar warning(s), {len(cal['upcoming'])} event(s) in the next "
          f"{LOOK_AHEAD_DAYS} days, {stories} news stories to read, {errors} problem(s).")


def run(test_mode=False, weekly=False):
    now = TEST_NOW if test_mode else datetime.now(timezone.utc)
    today = now.date()
    report = {"scout": "gold", "run_time": now.isoformat(), "test_mode": test_mode,
              "calendar": {}, "weekly_news": []}
    print(f"MINT GOLD SCOUT  {now:%Y-%m-%d %H:%M} UTC{'  (TEST MODE)' if test_mode else ''}")

    # ---- Job 1: calendar ----
    print("\n== Calendar: G2 Fed decisions, G3 jobs and inflation reports ==")
    events, notes = get_calendar(today, test_mode)
    for n in notes:
        print(f"   {n}")
    upcoming = sorted({(e["date"], e["event"], e["factor"]) for e in events
                       if today <= date.fromisoformat(e["date"]) <= today + timedelta(days=LOOK_AHEAD_DAYS)})
    warnings = []
    if not upcoming:
        print(f"   No scheduled gold events in the next {LOOK_AHEAD_DAYS} days.")
    for d, name, factor in upcoming:
        days_away = (date.fromisoformat(d) - today).days
        if days_away == 1:
            warnings.append(f"Tomorrow: {name}. Gold may move.")
            print(f"   WARNING  {d}  {name} ({factor})  <- TOMORROW")
        else:
            print(f"   upcoming {d}  {name} ({factor})  in {days_away} days")
    report["calendar"] = {"notes": notes, "upcoming": [list(u) for u in upcoming], "warnings": warnings}

    # ---- Job 2: weekly news summary ----
    if weekly:
        for f in load_json("factors.json")["factors"]:
            if f["asset"] != "Gold" or "weekly" not in f["method"]:
                continue
            print(f"\n== Weekly news: {f['id']} {f['name']} ==")
            result = {"id": f["id"], "name": f["name"], "kept": [], "rejected": [], "error": None}
            try:
                test_file = FOLDER / "test_data" / f"sample_{f['id']}.xml" if test_mode else None
                result["kept"], result["rejected"] = search_news("Gold", f["search"], WEEKLY_MAX_AGE_HOURS, now, test_file)
                print_stories(result["kept"], result["rejected"])
            except Exception as e:
                result["error"] = str(e)
                print(f"   ERROR: {e}")
            report["weekly_news"].append(result)

    print_summary(report, weekly)
    save_results("gold_scout", report, test_mode)
    return report


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv, weekly="--weekly" in sys.argv)
