"""
Shared tools used by BOTH Scouts (so the same checks are written only once).

  - load_json      : open a .json list file
  - fetch_page     : download a web page (with a polite name, so sites don't block us)
  - search_news    : Google News search, then keep only approved + fresh stories
  - save_results   : save a run to the results folder (your evidence)
"""

import json
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote_plus

import feedparser

FOLDER = Path(__file__).parent

# Secure connections: check website certificates the SAME way Windows and your browser do (the "truststore"
# add-on). This fixes "certificate verify failed" when antivirus or a school network checks secure traffic.
# Certificate checks are never switched off - that would let someone else read the bot token.
ORIGINAL_SSLCONTEXT = ssl.SSLContext
try:
    import truststore
    truststore.inject_into_ssl()
    TRUSTSTORE = True
except Exception:                     # not installed yet (run SETUP again, or Settings > Check connection)
    TRUSTSTORE = False

# Some sites block programs with no name, so we say who we are.
# The SEC asks programs to give a contact email. Type an email YOU are happy to share into the MINT app
# (Settings > Contact email). It replaces your-email@example.com below. Never put a password here.
USER_AGENT = "MINT-student-project/1.0 (school project; contact: your-email@example.com)"


def load_json(name):
    with open(FOLDER / name, encoding="utf-8") as f:
        return json.load(f)


def user_agent():
    """Our polite name. Uses the contact email typed into the MINT app (Settings), if there is one."""
    import os
    try:
        email = json.loads((FOLDER / "data" / "settings.json").read_text(encoding="utf-8")).get("contact_email", "")
    except Exception:
        email = ""
    email = os.environ.get("CONTACT_EMAIL") or email          # in the cloud: from a GitHub secret
    return USER_AGENT.replace("your-email@example.com", email) if email else USER_AGENT


def friendly_error(e, site=""):
    """Turn a network error into plain English, with what to try."""
    text = str(e)
    where = f" to {site}" if site else ""
    if "10054" in text or "reset" in text.lower() or "forcibly closed" in text.lower():
        return (f"the connection{where} was cut off by the network (WinError 10054). Usually your internet "
                "provider, a school/office network or antivirus is blocking this website. Try another network "
                "(e.g. a phone hotspot) or Settings > Check connection")
    if "Tunnel connection failed" in text or "ProxyError" in text:
        return f"a proxy on this network refused the connection{where} (school/office network?)"
    if "timed out" in text.lower():
        return f"no answer{where} in time (slow or blocked network)"
    if "getaddrinfo" in text or "Name or service not known" in text or "11001" in text:
        return f"could not find{where} (no internet, or the website name is blocked)"
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
        return (f"the security certificate{where} could not be trusted. Either antivirus / a network filter is "
                "checking secure connections, or the network is blocking the site. Settings > Check connection "
                "shows which")
    return text


def fetch_page(url, tries=2):
    """Download a web page and return its text. Tries twice. Raises a plain-English error if it fails."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise                                   # the website answered (e.g. 403): no point trying again
        except Exception as e:
            if attempt == tries - 1:
                raise RuntimeError(friendly_error(e, urlparse(url).netloc)) from e
            import time
            time.sleep(2)


def get_domain(url):
    """Turn 'https://www.reuters.com/xyz' into 'reuters.com'."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def find_organisation(domain, sources):
    """Return the organisation name if the website is approved for this asset, otherwise None."""
    for s in sources:
        if domain == s["domain"] or domain.endswith("." + s["domain"]):
            return s["organisation"]
    return None


def clean_text(html):
    """Remove HTML tags and extra spaces from a short description."""
    from html import unescape
    import re
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def google_news_url(search_words, days):
    q = quote_plus(f"{search_words} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# Check 3: the headline must actually be about the asset (stops stories that only match "lawsuit" etc.)
TOPIC_WORDS = {
    "Bitcoin": ["bitcoin", "btc", "crypto", "cryptocurrenc", "binance", "coinbase", "stablecoin", "blockchain",
                "digital asset"],
    "Gold": ["gold", "bullion", "precious metal"],
}


SITES_PER_SEARCH = 7      # how many approved websites are put into one "site:" search


def approved_site_entries(search_words, days, sources):
    """Search ONLY inside the approved news websites ("site:reuters.com OR site:coindesk.com ...").
    Google allows only a few "site:" words per search, so the approved list is split into small groups.
    Official websites (SEC, Fed...) are skipped here - MINT reads their own feeds and calendars directly.
    Returns (entries, number of searches that failed)."""
    news_sites = [s for s in sources if "news" in s["type"] or "data" in s["type"]]
    news_sites.sort(key=lambda s: 0 if "specialist" in s["type"] else 1)     # specialist news first
    domains = list(dict.fromkeys(s["domain"] for s in news_sites))
    entries, failed, searches = [], [], 0
    for i in range(0, len(domains), SITES_PER_SEARCH):
        searches += 1
        sites = " OR ".join(f"site:{d}" for d in domains[i:i + SITES_PER_SEARCH])
        feed = feedparser.parse(google_news_url(f"{search_words} ({sites})", days))
        if feed.get("bozo") and not feed.entries:
            failed.append(str(feed.get("bozo_exception")))
        entries += list(feed.entries)
    if searches and len(failed) == searches:
        raise RuntimeError(f"could not read the news feed ({failed[0]})")
    return entries


def search_news(asset, search_words, max_age_hours, now, test_file=None):
    """
    Search the news and check every story for one asset ("Bitcoin" or "Gold").
    Returns (kept, rejected). Each rejected story says WHY it was rejected.
    test_file: in test mode, read this saved sample instead of the internet.
    """
    sources = [s for s in load_json("sources.json")["sources"] if asset in s["assets"]]
    days = max(1, round(max_age_hours / 24))
    if test_file:
        feed = feedparser.parse(test_file)
        if feed.get("bozo") and not feed.entries:
            raise RuntimeError(f"could not read the news feed ({feed.get('bozo_exception')})")
        entries = list(feed.entries)
    else:
        entries = approved_site_entries(search_words, days, sources)     # ONLY the approved websites

    kept, rejected, seen = [], [], set()
    for entry in entries:
        key = entry.get("link") or entry.get("title")
        if key in seen:
            continue                     # the same story came back from two searches
        seen.add(key)
        source_url = (entry.get("source") or {}).get("href", "")
        story = {
            "title": entry.get("title", "(no title)"),
            "link": entry.get("link", ""),
            "source_site": get_domain(source_url) if source_url else "(unknown)",
            "source_name": (entry.get("source") or {}).get("title", ""),
        }
        # Check 1: is the website on the approved list?
        org = find_organisation(story["source_site"], sources)
        if org is None:
            story["reason"] = "website not on approved list"
            rejected.append(story)
            continue
        story["organisation"] = org
        # Check 3 (done early): does the headline mention the asset?
        headline = story["title"].lower()
        if not any(word in headline for word in TOPIC_WORDS[asset]):
            story["reason"] = f"headline is not about {asset}"
            rejected.append(story)
            continue
        # Check 2: does it have a date, and is it fresh enough?
        if not entry.get("published_parsed"):
            story["reason"] = "no publish date"
            rejected.append(story)
            continue
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        age = (now - published).total_seconds() / 3600
        story["published"] = published.isoformat()
        story["age_hours"] = round(age, 1)
        if age > max_age_hours:
            story["reason"] = f"too old ({age:.0f} hours)"
            rejected.append(story)
            continue
        kept.append(story)
    return kept, rejected


def read_official_feed(asset, feed_name, feed_url, max_age_hours, now, test_file=None):
    """
    Read an OFFICIAL feed directly (for example the SEC's own news feed) - no Google in between.
    Returns (kept, rejected) in the same shape as search_news. A story is kept only if it is
    about the asset and newer than max_age_hours. The website must be on the approved list.
    test_file: in test mode, read this saved sample instead of the internet.
    """
    domain = get_domain(feed_url)
    sources = [s for s in load_json("sources.json")["sources"] if asset in s["assets"]]
    org = find_organisation(domain, sources)
    if org is None:
        raise RuntimeError(f"{domain} is not on the approved list for {asset}")

    text = Path(test_file).read_text(encoding="utf-8") if test_file else fetch_page(feed_url)
    feed = feedparser.parse(text)
    if feed.get("bozo") and not feed.entries:
        raise RuntimeError(f"could not read the feed ({feed.get('bozo_exception')})")

    kept, rejected = [], []
    for entry in feed.entries:
        story = {"title": entry.get("title", "(no title)"), "link": entry.get("link", ""),
                 "source_site": domain, "organisation": org, "feed": feed_name,
                 "summary": clean_text(entry.get("summary", ""))[:600]}
        # Does the title or the short description mention the asset?
        words = (story["title"] + " " + entry.get("summary", "")).lower()
        if not any(w in words for w in TOPIC_WORDS[asset]):
            story["reason"] = f"not about {asset}"
            rejected.append(story)
            continue
        if not entry.get("published_parsed"):
            story["reason"] = "no publish date"
            rejected.append(story)
            continue
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        age = (now - published).total_seconds() / 3600
        story["published"] = published.isoformat()
        story["age_hours"] = round(age, 1)
        if age > max_age_hours:
            story["reason"] = f"too old ({age:.0f} hours)"
            rejected.append(story)
            continue
        kept.append(story)
    return kept, rejected


def print_stories(kept, rejected, show_rejected=True):
    for s in kept:
        print(f"   KEPT     [{s['organisation']}, {s['age_hours']}h old] {s['title']}")
    if show_rejected:
        for s in rejected:
            print(f"   REJECTED [{s['source_site']}] {s['title']}  -> {s['reason']}")
    print(f"   ({len(kept)} kept, {len(rejected)} rejected)")


def print_news_summary(results):
    """For the SUMMARY at the bottom: one line per factor, then the stories worth reading."""
    for r in results:
        if r["error"]:
            status = f"ERROR ({r['error']})"
        elif r["kept"]:
            status = f"{len(r['kept'])} kept, {len(r['rejected'])} rejected"
        else:
            status = f"NOTHING from approved sources ({len(r['rejected'])} rejected)"
        print(f"   {r['id']} {r['name']}: {status}")
    kept = [(r["id"], s) for r in results for s in r["kept"]]
    if kept:
        print("\n   Stories to read:")
        for factor_id, s in kept:
            print(f"   - [{factor_id}] {s['organisation']}, {s['age_hours']}h old: {s['title']}")
    else:
        print("\n   No stories to read.")


def save_results(prefix, report, test_mode):
    (FOLDER / "results").mkdir(exist_ok=True)
    name = f"{prefix}_{'TEST_' if test_mode else ''}{datetime.now():%Y-%m-%d_%H%M}.json"
    with open(FOLDER / "results" / name, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved results to results/{name}")
    import os
    if os.environ.get("MINT_CLOUD") == "1":       # in the cloud keep only the newest 10 of each kind
        for old in sorted((FOLDER / "results").glob(f"{prefix}_2*.json"))[:-10]:
            old.unlink()
    return FOLDER / "results" / name
