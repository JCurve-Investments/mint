# MINT – the two Scouts

| Program | Watches | How |
| --- | --- | --- |
| `btc_scout.py` | Bitcoin | **Breaking news** (bans, exchange collapses and hacks, ETF flows) + **official SEC feeds** (read directly) + **data checks** (Nasdaq daily fall, Wikipedia views) |
| `gold_scout.py` | Gold | **Calendar** (reads Fed meeting dates and US jobs/inflation report dates from the official websites) + **weekly news** (dollar, interest rates, central banks, gold ETFs) |

## Files

| File | What it is |
| --- | --- |
| `btc_scout.py`, `gold_scout.py` | The two Scouts |
| `common.py` | Checks both Scouts share (approved website? fresh enough?) |
| `sources.json` | Your approved websites, used by both Scouts |
| `factors.json` | Your 10 factors and how each one is found |
| `backup_calendar.json` | Spare copy of the calendar dates, used ONLY if an official website can't be read |
| `test_data/` | Made-up sample data for test runs (every sample story starts with "TEST") |
| `results/` | Every run is saved here – your evidence |

## One-time setup

1. **Install Python** from python.org/downloads. On Windows, tick **"Add Python to PATH"** on the first screen.
2. **Unzip** the `mint` folder somewhere easy, such as your Desktop.
3. **Open a terminal in the folder.**
   - Windows: open the `mint` folder, click the address bar, type `cmd`, press Enter.
   - Mac: right-click the `mint` folder → **New Terminal at Folder**.
4. **Install the one extra library:**
   - Windows: `py -m pip install -r requirements.txt`
   - Mac: `python3 -m pip install -r requirements.txt`

(Below, Windows users type `py`, Mac users type `python3`.)

## Test runs first (no internet needed)

- `py btc_scout.py --test` → 3 stories kept, 4 rejected (2 fake websites, 1 too old, 1 off-topic), plus a Nasdaq warning and an attention warning.
- `py gold_scout.py --weekly --test` → a "TOMORROW" warning, the jobs report in 5 days, and 3 weekly stories kept, 2 rejected.

## Real runs

- `py btc_scout.py`
- `py gold_scout.py` (calendar only) or `py gold_scout.py --weekly` (calendar + weekly news)

## Evidence to screenshot

- The first successful test run of each Scout.
- The first real run of each Scout, with the date and time visible.
- Any ERROR or "FAILED – used backup file" message **before** you fix it, then the run after the fix.

## New on 4 Nov

| File | What it is |
| --- | --- |
| `make_dashboard.py` | Builds `dashboard.html` from your `results/` files, `progress.json` and the `evidence/` folder. Run `py make_dashboard.py`. |
| `progress.json` | The build steps and the problem/fix log. **You edit this by hand** (status: done, doing, todo). |
| `evidence/` | Put your screenshots (.png) here. They show on the dashboard, captioned from their file names. |
| `wgc_inspect.py` | Shows what is inside the World Gold Council Excel file. You download that file by hand (their terms do not allow programs to scrape the site) and put it in `wgc_data/`. |

One-time: run `py -m pip install -r requirements.txt` again (it now also installs `openpyxl`).

**SEC feeds:** open `common.py` and replace `your-email@example.com` in `USER_AGENT` with an email you are happy to share (for example a school or family address). The SEC asks programs to say who they are. If the SEC feeds show an ERROR 403, this is the first thing to check.

Test: `py btc_scout.py --test` now also shows "B1 ... - official feeds" with 2 kept (SEC) and a SUMMARY box at the bottom.

## New on 4 Nov (evening): the MINT app and three new agents

**No more terminal.** First time only: double-click **`SETUP - double-click me first.bat`**. It installs what MINT needs and opens MINT in your browser. Then, in MINT, go to **Settings → Add to Desktop**. From then on, just double-click the MINT icon. (Or double-click `MINT.pyw` / `Start MINT.bat` in this folder.)

| File | What it is |
| --- | --- |
| `supervisor.py` | **Supervisor**: groups stories into events, applies the proof rules (official source alone / 2 news organisations / social = Watch only), remembers events in `data/seen_events.json`, sets Calm / Watch / Alert |
| `prices.py` | **Price Tracker**: CoinGecko prices (free, no key; gold = PAX Gold), and for every event the price move after 1 hour, 1 day, 7 days |
| `decision.py` | **Decision agent**: Claude (if you add an API key in Settings) or simple rules; checks the event date; writes the alert |
| `pipeline.py` | Runs all agents in order and tracks what each one is doing |
| `app.py`, `ui/index.html`, `MINT.pyw` | The MINT app (opens at http://localhost:8765, only reachable from your own computer) |
| `test_supervisor.py` | 10 tests for the Supervisor (also a button in Settings) |
| `data/` | MINT's memory, prices and settings. `data/test/` holds test data only. **Never share `data/settings.json`** (it holds your API key). |

`make_dashboard.py` still works, but the MINT app replaces it.

## New on 4 Nov (night)

| File | What it is |
| --- | --- |
| `social_watcher.py` | **Social Watcher**: reads Trump's Truth Social posts (through the free trumpstruth.org archive) and Binance's public Telegram channel. A post can only start a Watch. |
| `social_accounts.json` | The accounts it follows. Switch them on/off in Settings, or add RSS feeds / Bluesky profiles / public Telegram channels here. |

The Decision agent can now use **Groq** or **Gemini** (free tiers), **Ollama** (free, runs on your computer) or **Claude** (paid). Choose in Settings. Most AI services require the account holder to be 18 or over, so a parent should create the key.

## New on 4 Nov (late): Telegram alerts

| File | What it is |
| --- | --- |
| `telegram_bot.py` | Sign-ups (/start, /gold, /bitcoin, /both, /status, /stop) and sending stamped alerts. Set it up in Settings → Telegram alerts (token from @BotFather). |
| `test_telegram.py` | 11 tests with a pretend Telegram server (also a button in Settings). |
| `data/subscribers.json` | Subscribers' Telegram chat numbers and choices only. |

## New on 4 Nov (v5)

- **Telegram:** people choose *Alerts only* (alerts + Sunday weekly summary) or *Everything* (also watches, watch updates, heads-ups before gold events).
- **`weekly.py`:** the Sunday summary - prices, what MINT found, and how good its decisions were. Shown on the History page too (with a Preview button).
- **Smart timing** (Settings → Run automatically): every 30 min, every 15 min while something is on Watch or a gold event is tomorrow.
- `test_telegram.py` now has 17 checks.

## New on 5 Nov (v6, v7)

- **Settings → Check connection** tests every website MINT uses (OK / Blocked) and errors are in plain English.
- **Predictions (`predict.py`):** every event gets an expected direction (up / down / unclear) from its factor's past pattern. History judges results against the prediction: **Right / Wrong / No clear move**. Green = moved the predicted way.
- **History page redesigned:** accuracy cards, filters, prediction column, accuracy by factor, weekly summaries.
- "Dropped" is now called **Not confirmed** - no event is ever deleted.

## New on 6 Nov (v10): predictions from past events

- **`library.py`**: the past events library. Predictions now come from what the price did after **similar past events** (plus the factor pattern), never from the headline's own price words.
- **History → Past events library**: add real past events (date, asset, factor, headline). MINT downloads the real prices (Yahoo Finance) and records the 1-day and 7-day moves. Ideas to research are listed there.
- Each event shows *why* MINT predicts its direction: the similar past events and what happened after them.
- `test_supervisor.py` now has 20 checks.

## New on 9 Nov (v11)

- **Groq fix** (error 1010): AI requests now send a User-Agent.
- **Report everything:** no more first-run learning. Telegram sends every event with MINT's view, every update, and the 1-day result. Optional **Telegram channel** (people just tap Join).
- **`factor_knowledge.json`**: background knowledge for each factor, given to the Decision agent.
- **`starter_library.json`**: 36 major past events; MINT downloads their real prices (12 per run) into the past events library. Check the dates as part of your research.
- Every event gets **MINT's view**: summary, what similar past events show, confidence (low / medium / high from the evidence), what to watch next.
- Tests: Supervisor 20/20, Telegram 18/18.

## Cloud version (v12): MINT without your PC

- `cloud_run.py` + `.github/workflows/mint.yml`: GitHub Actions runs every agent every 15 minutes (free for public repositories) and saves MINT's memory back into the repository.
- `docs/`: the public, view-only dashboard (GitHub Pages), written by every cloud run.
- Keys come from **GitHub Secrets**: `TELEGRAM_TOKEN`, `TELEGRAM_CHANNEL`, `GROQ_KEY`, `CONTACT_EMAIL`. They are never written to a file.
- In the cloud, everything is posted to the Telegram **channel**; people who message the bot are pointed to the channel (no subscriber list in a public repository).
- `.gitignore` keeps private files (settings, subscribers, evidence) out of the repository.

## New on 9 Nov (v14): more Bitcoin news, no double counting

- **Scouts search inside the approved websites** too (`site:reuters.com OR site:coindesk.com ...`), so approved stories are found even when Google shows mostly small websites.
- **Same story = one event:** stories with the same factor on the same day that share key words are joined (e.g. the WGC's Q3 report and the WSJ story about it). Older duplicates in memory are joined automatically, and website sections ("Investment", "Gold Market Primer") are removed, so accuracy is not counted twice.
- Tests: Supervisor 22/22, Telegram 18/18.

## v15: approved websites only, more social accounts

- **Scouts search ONLY inside the approved websites** (Google News `site:` search, in groups of 7 websites).
- **Social Watcher** now also follows the Federal Reserve and the ECB (official Bluesky accounts), Watcher.Guru (Telegram) and Whale Alert (hack-type posts only). X (Elon Musk etc.) has no free way to read posts.
- **Cloud schedule** moved to minutes 7, 22, 37, 52 (GitHub often delays runs planned for :00).

- v16: the cloud now runs **every 5 minutes** (GitHub's fastest schedule).
- v18: Telegram messages start with an "at a glance" line (🔴🟠🟡 impact, 📈/📉 prediction, ▰▰▰ confidence); the channel gets a key to pin once. Tests: Supervisor 22/22, Telegram 20/20.
- v19: History - "So far" column, result countdown, impact and confidence, results re-judged after the Decision agent, old posts tidied, median delay.
- v20: silent "run complete - nothing new" note in the Telegram channel after runs with nothing to report. Tests: Supervisor 22/22, Telegram 21/21.
- v21: **one reliable source is enough** - an approved news organisation or an official source confirms an event on its own. Watch = a social media post only (impact tracked, alert needs an approved source).
- v22: predictions judged on **9 price checks** (1 h → 7 days) with a "clear move" bar that grows with time; History explains how success is measured and shows accuracy by time; Telegram sends a 1-day and a final 7-day result; social posts say where they came from.
- v23: 📏 impact size and how long it lasted for every event; approved social media accounts count on their own (labelled "From social media").
