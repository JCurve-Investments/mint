"""
MINT Social Watcher
===================
Follows a short list of important people and companies on social media, using only FREE ways to read them
(the list is in social_accounts.json):

  - RSS feeds        e.g. Donald Trump's Truth Social posts (through the free trumpstruth.org archive),
                     or any Bluesky profile (https://bsky.app/profile/<handle>/rss)
  - Telegram         public channels, read from their public web page (https://t.me/s/<channel>)

Why X (Twitter) is not included: X no longer has a free way for programs to read posts (it charges per post).

A post is KEPT only if:
  1. it is newer than 24 hours,
  2. it mentions Bitcoin/crypto or gold, OR one of the gold factors (Fed, rates, dollar...),
  3. it matches one of MINT's factor keywords (so "great rally tonight" is ignored).

Important rule (the Supervisor enforces it): a social media post can only start a WATCH.
Accounts can be hacked or fake (in January 2024 the SEC's own X account was hacked and posted a false
Bitcoin ETF approval), so MINT waits for a news or official source before any alert.

Run on its own:  py social_watcher.py        py social_watcher.py --test
"""

import json
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import feedparser

from common import FOLDER, load_json, fetch_page, clean_text, save_results, TOPIC_WORDS

TEST_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def read_rss(account, test_mode):
    if test_mode:
        text = (FOLDER / "test_data" / account["test_file"]).read_text(encoding="utf-8")
    else:
        text = fetch_page(account["url"])
    feed = feedparser.parse(text)
    if feed.get("bozo") and not feed.entries:
        raise RuntimeError(f"could not read the feed ({feed.get('bozo_exception')})")
    posts = []
    for e in feed.entries:
        body = clean_text(e.get("summary") or e.get("description") or "")
        title = clean_text(e.get("title", ""))
        if not body or title.lower().startswith(("[no title]", "no title")):
            body = body or title
        else:
            body = title if title and title in body else (title + ". " + body if title else body)
        published = None
        if e.get("published_parsed"):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        posts.append({"text": body, "link": e.get("link", ""), "published": published})
    return posts


def read_telegram(account, test_mode):
    """Read a public Telegram channel from its web page (no login, no key)."""
    if test_mode:
        html = (FOLDER / "test_data" / account["test_file"]).read_text(encoding="utf-8")
    else:
        html = fetch_page(f"https://t.me/s/{account['channel']}")
    posts = []
    for block in html.split('class="tgme_widget_message_wrap')[1:]:
        post_id = re.search(r'data-post="([^"]+)"', block)
        text = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not text:
            continue
        body = unescape(re.sub(r"<br\s*/?>", " ", text.group(1)))
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        published = datetime.fromisoformat(when.group(1)).astimezone(timezone.utc) if when else None
        link = f"https://t.me/{post_id.group(1)}" if post_id else f"https://t.me/s/{account['channel']}"
        posts.append({"text": body, "link": link, "published": published})
    if not posts and "tgme_channel_info" not in html and not test_mode:
        raise RuntimeError("no posts found - the channel may be private or the name may be wrong")
    return posts


def match_post(text, factors):
    """Which asset and factor is a post about? Returns (asset, factor id, matched word) or None."""
    low = text.lower()
    gold_factor_words = {"fed", "federal reserve", "interest rate", "rate cut", "rate hike", "powell", "dollar"}
    for f in factors:
        words = f.get("keywords") or []
        hit = next((k for k in words if re.search(r"\b" + re.escape(k) + r"(s|es|ed|d)?\b", low)), None)
        if not hit:
            continue
        mentions_asset = any(w in low for w in TOPIC_WORDS[f["asset"]])
        # Fed/rates/dollar posts matter for gold even when they do not say "gold"
        if mentions_asset or (f["asset"] == "Gold" and hit in gold_factor_words):
            return f["asset"], f["id"], hit
    return None


def run(test_mode=False, now=None):
    now = now or (TEST_NOW if test_mode else datetime.now(timezone.utc))
    config = load_json("social_accounts.json")
    factors = load_json("factors.json")["factors"]
    max_age = config.get("max_age_hours", 24)
    report = {"scout": "social", "run_time": now.isoformat(), "test_mode": test_mode, "news": [], "accounts": []}
    by_factor = {}
    print(f"MINT SOCIAL WATCHER  {now:%Y-%m-%d %H:%M} UTC{'  (TEST MODE)' if test_mode else ''}")
    errors = 0

    for acc in config["accounts"]:
        if not acc.get("on", True):
            continue
        if test_mode and not acc.get("test_file"):
            continue
        print(f"\n== {acc['name']} ==")
        info = {"name": acc["name"], "read": 0, "kept": 0, "error": None}
        try:
            posts = read_rss(acc, test_mode) if acc["type"] == "rss" else read_telegram(acc, test_mode)
            info["read"] = len(posts)
            for p in posts:
                if not p["published"]:
                    continue
                age = (now - p["published"]).total_seconds() / 3600
                if age > max_age or age < -1:
                    continue
                m = match_post(p["text"], factors)
                short = p["text"][:110] + ("..." if len(p["text"]) > 110 else "")
                if not m:
                    continue
                asset, fid, word = m
                story = {"title": p["text"][:160], "link": p["link"], "source_site": acc.get("channel") or acc["name"],
                         "organisation": acc["name"], "kind": "social", "asset": asset, "summary": p["text"][:600],
                         "published": p["published"].isoformat(), "age_hours": round(age, 1)}
                by_factor.setdefault(fid, []).append(story)
                info["kept"] += 1
                print(f"   KEPT  [{fid}, '{word}', {age:.1f}h old] {short}")
            print(f"   read {info['read']} posts, kept {info['kept']}")
        except Exception as e:
            info["error"] = str(e)
            errors += 1
            print(f"   ERROR: {e}")
        report["accounts"].append(info)

    report["news"] = [{"id": fid, "name": "social media", "kept": stories, "rejected": [], "error": None}
                      for fid, stories in by_factor.items()]
    kept = sum(len(s) for s in by_factor.values())
    print(f"\nBOTTOM LINE: {len(report['accounts'])} account(s) checked, {kept} post(s) kept "
          f"(a post can only start a Watch), {errors} error(s).")
    save_results("social", report, test_mode)
    return report


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv)
