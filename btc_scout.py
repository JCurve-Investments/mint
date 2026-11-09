"""
MINT Bitcoin Scout

Job 1 - Breaking news (B1 bans, B2 exchange collapses and hacks, B4 ETF flows):
    searches the news, keeps only approved websites and stories under 24 hours old.
Job 2 - Data checks (numbers, not news):
    B3 stock market mood : did the Nasdaq fall more than 2% on its last trading day?
    B5 investor attention: did views of the Wikipedia "Bitcoin" page jump this week?

Run:  python btc_scout.py          (real run, needs internet)
      python btc_scout.py --test   (uses saved sample data, no internet)
"""

import json
import sys
from datetime import datetime, timezone, timedelta

from common import (FOLDER, load_json, fetch_page, search_news, read_official_feed,
                    print_stories, print_news_summary, save_results)

MAX_AGE_HOURS = 24          # breaking news must be newer than this
NASDAQ_DROP_LIMIT = -2.0    # warn if the Nasdaq falls more than 2% in a day (my starting guess - test it)
ATTENTION_JUMP_LIMIT = 1.5  # warn if this week's Wikipedia views are 1.5x last week's (starting guess - test it)
TEST_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def nasdaq_check(test_mode):
    """B3: get the Nasdaq's last two daily closing prices and work out the % change."""
    if test_mode:
        text = (FOLDER / "test_data" / "sample_nasdaq.json").read_text()
    else:
        text = fetch_page("https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?range=5d&interval=1d")
    closes = json.loads(text)["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    closes = [c for c in closes if c is not None]
    change = (closes[-1] - closes[-2]) / closes[-2] * 100
    return {"last_close": round(closes[-1], 2), "change_percent": round(change, 2),
            "warning": change <= NASDAQ_DROP_LIMIT}


def attention_check(now, test_mode):
    """B5: compare Wikipedia 'Bitcoin' page views for the last 7 days with the 7 days before."""
    if test_mode:
        text = (FOLDER / "test_data" / "sample_wikipedia.json").read_text()
    else:
        end = now - timedelta(days=1)
        start = end - timedelta(days=13)
        url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/"
               f"all-access/user/Bitcoin/daily/{start:%Y%m%d}00/{end:%Y%m%d}00")
        text = fetch_page(url)
    views = [day["views"] for day in json.loads(text)["items"]]
    last_week, week_before = sum(views[-7:]), sum(views[-14:-7])
    ratio = last_week / week_before
    return {"views_last_7_days": last_week, "views_previous_7_days": week_before,
            "ratio": round(ratio, 2), "warning": ratio >= ATTENTION_JUMP_LIMIT}


def print_summary(report):
    """A short summary at the very bottom, so you can see at a glance what the Scout did."""
    print("\n" + "=" * 60)
    print(f"SUMMARY - Bitcoin Scout  ({report['run_time'][:16].replace('T', ' ')} UTC)")
    print("=" * 60)
    print("News (last 24 hours, approved websites only):")
    print_news_summary(report["news"])

    print("\nData checks:")
    warnings = 0
    b3 = report["data_checks"]["B3"]
    if "error" in b3:
        print(f"   B3 Stock market (Nasdaq): ERROR ({b3['error']})")
    else:
        warnings += b3["warning"]
        print(f"   B3 Stock market (Nasdaq): {b3['change_percent']}% on its last day -> "
              f"{'WARNING' if b3['warning'] else 'no warning'}")
    b5 = report["data_checks"]["B5"]
    if "error" in b5:
        print(f"   B5 Investor attention: ERROR ({b5['error']})")
    else:
        warnings += b5["warning"]
        print(f"   B5 Investor attention: {b5['ratio']}x last week's views -> "
              f"{'WARNING' if b5['warning'] else 'no warning'}")

    stories = sum(len(x["kept"]) for x in report["news"])
    errors = sum(1 for x in report["news"] if x["error"]) + \
        sum(1 for v in report["data_checks"].values() if "error" in v)
    print(f"\nBOTTOM LINE: {stories} news stories to read, {warnings} data warning(s), {errors} error(s).")


def run(test_mode=False):
    now = TEST_NOW if test_mode else datetime.now(timezone.utc)
    factors = load_json("factors.json")["factors"]
    report = {"scout": "bitcoin", "run_time": now.isoformat(), "test_mode": test_mode,
              "news": [], "data_checks": {}}
    print(f"MINT BITCOIN SCOUT  {now:%Y-%m-%d %H:%M} UTC{'  (TEST MODE)' if test_mode else ''}")

    # ---- Job 1: breaking news ----
    for f in factors:
        if f["asset"] != "Bitcoin" or f["method"] != "breaking_news":
            continue
        print(f"\n== {f['id']} {f['name']} ==")
        result = {"id": f["id"], "name": f["name"], "kept": [], "rejected": [], "error": None}
        try:
            test_file = FOLDER / "test_data" / f"sample_{f['id']}.xml" if test_mode else None
            result["kept"], result["rejected"] = search_news("Bitcoin", f["search"], MAX_AGE_HOURS, now, test_file)
            print_stories(result["kept"], result["rejected"])
        except Exception as e:            # one failed search must not stop the others
            result["error"] = str(e)
            print(f"   ERROR: {e}")
        report["news"].append(result)

    # ---- Job 1b: official feeds (read directly from the source, no Google in between) ----
    for f in factors:
        if f["asset"] != "Bitcoin" or not f.get("official_feeds"):
            continue
        print(f"\n== {f['id']} {f['name']} - official feeds ==")
        result = {"id": f["id"], "name": f"{f['name']} (official feeds)", "kept": [], "rejected": [], "error": None}
        errors = []
        for feed in f["official_feeds"]:
            try:
                test_file = FOLDER / "test_data" / feed["test_file"] if test_mode else None
                kept, rejected = read_official_feed("Bitcoin", feed["name"], feed["url"], MAX_AGE_HOURS, now, test_file)
                result["kept"] += kept
                result["rejected"] += rejected
                print(f"   {feed['name']}: read OK, {len(kept)} kept, {len(rejected)} rejected")
            except Exception as e:             # one failed feed must not stop the others
                errors.append(f"{feed['name']}: {e}")
                print(f"   ERROR in {feed['name']}: {e}")
        result["error"] = "; ".join(errors) or None
        print_stories(result["kept"], result["rejected"], show_rejected=False)
        report["news"].append(result)

    # ---- Job 2: data checks ----
    print("\n== B3 US stock market mood (Nasdaq) ==")
    try:
        r = nasdaq_check(test_mode)
        flag = "WARNING: Bitcoin may fall too" if r["warning"] else "no warning"
        print(f"   Nasdaq changed {r['change_percent']}% on its last day -> {flag}")
        report["data_checks"]["B3"] = r
    except Exception as e:
        print(f"   ERROR: {e}")
        report["data_checks"]["B3"] = {"error": str(e)}

    print("\n== B5 Investor attention (Wikipedia views) ==")
    try:
        r = attention_check(now, test_mode)
        flag = "WARNING: attention jumped" if r["warning"] else "no warning"
        print(f"   {r['views_last_7_days']:,} views this week vs {r['views_previous_7_days']:,} "
              f"the week before ({r['ratio']}x) -> {flag}")
        report["data_checks"]["B5"] = r
    except Exception as e:
        print(f"   ERROR: {e}")
        report["data_checks"]["B5"] = {"error": str(e)}

    print_summary(report)
    save_results("btc_scout", report, test_mode)
    return report


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv)
