"""
MINT in the cloud (GitHub Actions)
==================================
GitHub runs this file every 5 minutes (see .github/workflows/main.yml), so MINT works with your PC switched off.

One run:
  1. answers anyone who messaged the bot (it points them to the Telegram channel)
  2. runs every agent once: Scouts -> Social Watcher -> Supervisor -> Price Tracker -> Decision agent -> Telegram
  3. writes the public dashboard (docs/index.html + docs/state.json) for GitHub Pages
Afterwards the workflow saves MINT's memory (the data folder) back into the repository, ready for the next run.

Keys (Telegram token, Groq key, contact email) come from GitHub SECRETS, never from files, so they can never
appear in the public repository. Run it on your own PC (for testing) with:  py cloud_run.py
"""

import json
import os
import shutil
from datetime import datetime, timezone

os.environ.setdefault("MINT_CLOUD", "1")

import app                     # noqa: E402  (sets up folders; does not start the web server)
import decision                # noqa: E402
import pipeline                # noqa: E402
import telegram_bot            # noqa: E402
from common import FOLDER      # noqa: E402


def answer_messages():
    token = decision.load_settings().get("telegram_token", "")
    if not token:
        print("Telegram: no TELEGRAM_TOKEN secret - skipping messages and sending.")
        return
    try:
        telegram_bot.check_messages(token)
        print("Telegram: answered new messages (if any).")
    except Exception as e:
        print(f"Telegram: could not read messages ({e})")


def write_dashboard():
    """The public, read-only dashboard: the same page as the app, reading state.json instead of the app."""
    docs = FOLDER / "docs"
    docs.mkdir(exist_ok=True)
    app.VIEW["test_mode"] = False
    state = app.build_state()
    state["static"] = True
    state["startup"] = False
    # remove anything private before it goes on a public website
    state["settings"]["contact_email"] = ""
    for p in state["settings"].get("providers", []):
        p["key_hint"] = ""
    state["telegram"]["subscribers"] = state["telegram"]["gold"] = state["telegram"]["bitcoin"] = 0
    if str(state["telegram"].get("channel", "")).lstrip("-").isdigit():
        state["telegram"]["channel"] = ""            # a private channel's number is not needed on a public page
    (docs / "state.json").write_text(json.dumps(state), encoding="utf-8")
    shutil.copy(FOLDER / "ui" / "index.html", docs / "index.html")
    (docs / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Dashboard written: docs/state.json ({len(state['events'])} events)")


def main():
    started = datetime.now(timezone.utc)
    print(f"MINT cloud run {started:%Y-%m-%d %H:%M} UTC")
    answer_messages()
    pipeline.run_cycle(test_mode=False, reason="cloud (GitHub Actions)")
    for name in pipeline.AGENT_NAMES:                  # everything each agent printed, for the Actions log
        a = pipeline.AGENTS[name]
        print("\n" + "=" * 70 + f"\n{name}: {a['last_result']}\n" + "=" * 70)
        print(a["output"][-4000:])
    write_dashboard()


if __name__ == "__main__":
    main()
