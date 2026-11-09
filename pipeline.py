"""
MINT pipeline - runs all the agents in order, and keeps track of what each one is doing.

    1. Bitcoin Scout     finds Bitcoin stories and data warnings
    2. Gold Scout        reads the calendars (and the gold news once a day)
    2b. Social Watcher   reads posts from a few important people and companies (free sources only)
    3. Supervisor        groups stories into events, checks proof, decides Watch / Confirmed
    4. Price Tracker     saves prices and fills in how the price moved after each event
    5. Decision agent    writes the alert for confirmed events

The MINT app calls run_cycle() when you press "Run now" or when the timer says it is time.
One agent failing never stops the others.
"""

import contextlib
import io
import json
import threading
import time
import traceback
from datetime import datetime, timezone

import btc_scout
import decision
import gold_scout
import prices
import social_watcher
import supervisor
import telegram_bot
from supervisor import data_folder, parse_time, hours_between

AGENT_NAMES = ["Bitcoin Scout", "Gold Scout", "Social Watcher", "Supervisor", "Price Tracker", "Decision agent",
               "Telegram bot"]
TEST_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

AGENTS = {n: {"state": "waiting", "text": "Waiting for the first run", "last_run": None, "last_result": "",
              "output": ""} for n in AGENT_NAMES}
LOG = []
STATUS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


def log(agent, text):
    with STATUS_LOCK:
        LOG.append({"time": datetime.now(timezone.utc).isoformat(), "agent": agent, "text": text})
        del LOG[:-300]


def set_agent(name, state, text, result=None):
    with STATUS_LOCK:
        a = AGENTS[name]
        a["state"], a["text"] = state, text
        if result is not None:
            a["last_result"] = result
            a["last_run"] = datetime.now(timezone.utc).isoformat()


class LiveOutput:
    """Catches what an agent prints, keeps all of it, and shows the newest line as 'what it is doing now'."""

    def __init__(self, agent):
        self.agent, self.parts = agent, []

    def write(self, text):
        self.parts.append(text)
        for line in text.splitlines():
            line = line.strip().strip("=").strip()
            if line:
                with STATUS_LOCK:
                    AGENTS[self.agent]["text"] = line[:160]
        return len(text)

    def flush(self):
        pass

    def text(self):
        return "".join(self.parts)


def run_agent(name, fn, *args, **kwargs):
    """Run one agent, catching its printed output and any error."""
    set_agent(name, "working", "Starting")
    log(name, "started")
    out = LiveOutput(name)
    start = time.time()
    try:
        with contextlib.redirect_stdout(out):
            result = fn(*args, **kwargs)
        bottom = [l for l in out.text().splitlines() if l.startswith("BOTTOM LINE")]
        summary = bottom[-1].replace("BOTTOM LINE: ", "") if bottom else "finished"
        set_agent(name, "done", f"Finished in {time.time() - start:.0f}s", summary)
        log(name, summary)
        ok = True
    except Exception as e:
        result, ok = None, False
        out.write(f"\nERROR: {e}\n{traceback.format_exc()}")
        set_agent(name, "error", f"Error: {e}", f"ERROR: {e}")
        log(name, f"ERROR: {e}")
    with STATUS_LOCK:
        AGENTS[name]["output"] = out.text()
    return result, ok


def send_step(test_mode, now, warnings):
    s = decision.load_settings()
    token, channel = s.get("telegram_token", ""), s.get("telegram_channel", "")
    print(f"MINT TELEGRAM BOT{'  (TEST MODE - nothing is sent)' if test_mode else ''}")
    return telegram_bot.send_alerts(token, test_mode, now, warnings, channel)


def load_state(test_mode):
    path = data_folder(test_mode) / "pipeline_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(state, test_mode):
    (data_folder(test_mode) / "pipeline_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_cycle(test_mode=False, reason="manual"):
    """Run every agent once. Returns False if a run was already going."""
    if not RUN_LOCK.acquire(blocking=False):
        return False
    try:
        now = TEST_NOW if test_mode else datetime.now(timezone.utc)
        settings = decision.load_settings()
        state = load_state(test_mode)
        for n in AGENT_NAMES:
            set_agent(n, "queued", "Waiting for its turn")
        log("MINT", f"{'TEST run' if test_mode else 'Run'} started ({reason})")
        started = time.time()

        btc_report, ok_btc = run_agent("Bitcoin Scout", btc_scout.run, test_mode)
        last_weekly = parse_time(state.get("last_gold_weekly"))
        weekly = test_mode or not last_weekly or \
            hours_between(now, last_weekly) >= settings.get("gold_news_every_hours", 24)
        gold_report, ok_gold = run_agent("Gold Scout", gold_scout.run, test_mode, weekly)
        if ok_gold and weekly:
            state["last_gold_weekly"] = now.isoformat()
        social_report, _ = run_agent("Social Watcher", social_watcher.run, test_mode, now)
        reports = [r for r in (btc_report, gold_report, social_report) if r]
        sup, _ = run_agent("Supervisor", supervisor.run, reports, test_mode, now)
        run_agent("Price Tracker", prices.run, test_mode, now)
        dec, _ = run_agent("Decision agent", decision.run, test_mode, now)
        warnings = (gold_report or {}).get("calendar", {}).get("warnings", [])
        run_agent("Telegram bot", send_step, test_mode, now, warnings)

        for n in AGENT_NAMES:
            if AGENTS[n]["state"] == "done":
                set_agent(n, "waiting", "Waiting for the next run")
        state["last_run"] = now.isoformat()
        state["last_run_real_time"] = datetime.now(timezone.utc).isoformat()
        save_state(state, test_mode)

        if not test_mode:
            try:                                   # success criterion 1: re-test only if the past events changed
                import backtest
                with contextlib.redirect_stdout(io.StringIO()):
                    backtest.run()
            except Exception as e:
                log("MINT", f"Past-event test problem: {e}")

        errors = [n for n in AGENT_NAMES if AGENTS[n]["state"] == "error"]
        compact = data_folder(test_mode) / "run_log.json"      # success criterion 5: every run, kept small
        run_log = json.loads(compact.read_text(encoding="utf-8")) if compact.exists() else []
        run_log.append({"start": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
                        "seconds": round(time.time() - started), "ok": not errors, "errors": errors,
                        "reason": reason})
        compact.write_text(json.dumps(run_log[-6000:]), encoding="utf-8")

        record = {"time": datetime.now(timezone.utc).isoformat(), "test_mode": test_mode, "reason": reason,
                  "seconds": round(time.time() - started),
                  "results": {n: AGENTS[n]["last_result"] for n in AGENT_NAMES}}
        runs_path = data_folder(test_mode) / "runs.json"
        runs = json.loads(runs_path.read_text(encoding="utf-8")) if runs_path.exists() else []
        runs.append(record)
        runs_path.write_text(json.dumps(runs[-200:], indent=2), encoding="utf-8")
        log("MINT", f"Run finished in {record['seconds']}s")
        return True
    finally:
        RUN_LOCK.release()


def is_running():
    return RUN_LOCK.locked()
