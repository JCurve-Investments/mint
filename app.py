"""
MINT app - the control room.

Double-click "MINT.pyw" (or the MINT icon on your Desktop). MINT starts quietly in the background
and opens in your web browser at http://localhost:8765. No terminal needed.

  - "Run now" runs every agent once.
  - Turn on "Run automatically" and MINT runs every 30 minutes (or what you choose) while it is open.
  - Settings can put a MINT icon on your Desktop and start MINT when your computer starts.

The page is only reachable from THIS computer (127.0.0.1), never from the internet.
"""

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

FOLDER = Path(__file__).parent
os.chdir(FOLDER)
DATA = FOLDER / "data"
DATA.mkdir(exist_ok=True)

# When started by double-click (pythonw) there is no window to print to, so printing goes to a log file.
if sys.stdout is None or sys.stderr is None:
    log_file = open(DATA / "mint_app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log_file
    sys.stderr = sys.stderr or log_file

import decision                      # noqa: E402  (must come after the log-file lines above)
import pipeline                      # noqa: E402
import prices                        # noqa: E402
import telegram_bot                  # noqa: E402
import weekly                        # noqa: E402
from common import load_json         # noqa: E402
from supervisor import data_folder, load_memory   # noqa: E402

VERSION = "12"                         # must match UI_VERSION in ui/index.html
PORT = 8765
ADDRESS = f"http://localhost:{PORT}"
SCHEDULE = {"next_run": None}
TELEGRAM = {"error": ""}
VIEW = {"test_mode": False}          # which data the page shows: real or test


# ---------------------------------------------------------------------------
# Building what the page shows
# ---------------------------------------------------------------------------
def newest_result(prefix, test_mode):
    pattern = f"{prefix}_TEST_*.json" if test_mode else f"{prefix}_2*.json"
    files = sorted((FOLDER / "results").glob(pattern))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def chart_points(asset, test_mode):
    if test_mode:
        pts = prices.fake_history(asset)
        cut = prices.TEST_NOW.timestamp() * 1000
        return [p for p in pts if p[0] <= cut], "made-up test prices"
    path = data_folder(False) / f"price_history_{asset.lower()}.json"
    pts, source = [], "none yet"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        pts, source = saved["points"], saved["source"]
    snaps_path = data_folder(False) / "prices.json"
    if snaps_path.exists():
        for s in json.loads(snaps_path.read_text(encoding="utf-8"))[-200:]:
            t = int(datetime.fromisoformat(s["time"]).timestamp() * 1000)
            if not pts or t > pts[-1][0]:
                pts.append([t, s[asset]])
    return pts, source


CHECK_SITES = [
    ("Google News (Scouts)", "https://news.google.com/rss/search?q=gold&hl=en-US&gl=US&ceid=US:en"),
    ("SEC (official feeds)", "https://www.sec.gov/news/pressreleases.rss"),
    ("Federal Reserve (calendar)", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("CoinGecko (prices)", "https://api.coingecko.com/api/v3/ping"),
    ("Yahoo Finance (Nasdaq check)", "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?range=1d"),
    ("Trump's Truth archive (Social Watcher)", "https://www.trumpstruth.org/feed?limit=1"),
    ("Telegram channel pages, t.me (Social Watcher)", "https://t.me/s/binance_announcements"),
    ("Telegram bot service, api.telegram.org (alerts)", "https://api.telegram.org/"),
    ("Groq (AI for the Decision agent)", "https://api.groq.com/openai/v1/models"),
]


def certificate_details(url):
    """When a certificate is NOT trusted, find out who answered. The connection is opened only to READ the
    certificate - nothing is sent (no token). Returns a plain-English explanation."""
    import socket
    import tempfile
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                der = s.getpeercert(binary_form=True)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
        info = ssl._ssl._test_decode_cert(f.name)
        os.unlink(f.name)
    except Exception as e:
        return f"could not read the certificate ({e})"
    field = lambda part, key: next((v for rdn in info.get(part, ()) for k, v in rdn if k == key), "")
    names = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"] + [field("subject", "commonName")]
    issuer = field("issuer", "organizationName") or field("issuer", "commonName") or "unknown"
    base = ".".join(host.split(".")[-2:])
    matches = any(n == host or n.endswith("." + base) or n == base or (n.startswith("*.") and host.endswith(n[1:]))
                  for n in names if n)
    if not matches:
        return (f"BLOCKED BY THE NETWORK: a different server answered instead (its certificate is for "
                f"'{names[-1] or names[0] if names else '?'}', issued by '{issuer}'). MINT refuses to connect, which keeps "
                "your token safe. Try another network (phone hotspot) or DNS 1.1.1.1.")
    return (f"The certificate is for the right website but was issued by '{issuer}', which this computer does not "
            "trust. This usually means antivirus or a network filter is checking secure connections. "
            + ("Restart MINT after installing the certificate fix." if not TRUSTSTORE_ACTIVE() else
               "The certificate fix is already on, so turn off 'HTTPS scanning' / 'web shield' for Python in your "
               "antivirus, or use another network."))


def TRUSTSTORE_ACTIVE():
    from common import TRUSTSTORE
    return TRUSTSTORE


def check_connection():
    """Try every website MINT uses once. Any answer (even 'not allowed') means the website can be reached;
    only a connection error means it is blocked or down."""
    import urllib.error
    import urllib.request
    from common import user_agent, friendly_error
    results = []
    for name, url in CHECK_SITES:
        start = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=12) as r:
                code = r.status
            results.append({"name": name, "ok": True, "detail": f"reachable ({time.time() - start:.1f}s)"})
        except urllib.error.HTTPError as e:
            note = " - the SEC wants a contact email (Settings)" if "sec.gov" in url and e.code == 403 else ""
            results.append({"name": name, "ok": True, "detail": f"reachable (answered {e.code}){note}"})
        except Exception as e:
            detail = friendly_error(e)
            if "certificate" in str(e).lower():
                detail = certificate_details(url)
            results.append({"name": name, "ok": False, "detail": detail})
    return results


def library_state(test_mode, memory):
    import library
    researched = library.load_researched(test_mode)
    have = {(e["date"], e["asset"]) for e in researched}
    loaded = {(e["date"], e["asset"]) for e in researched}
    return {"researched": sorted(researched, key=lambda e: e["date"], reverse=True),
            "own": len(library.own_entries(memory)),
            "starter_left": 0 if test_mode else sum(1 for s in library.starter_events() if (s["date"], s["asset"]) not in loaded),
            "suggestions": []}


def telegram_log():
    path = DATA / "telegram_log.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def social_state(test_mode):
    config = load_json("social_accounts.json")
    last = newest_result("social", test_mode) or {}
    results = {a["name"]: a for a in last.get("accounts", [])}
    return {"checked": last.get("run_time"),
            "accounts": [{"name": a["name"], "via": a.get("via", ""), "why": a.get("why", ""), "on": a.get("on", True),
                          "result": results.get(a["name"])} for a in config["accounts"]]}


def public_event(ev):
    keep = ["id", "asset", "factor", "factor_name", "title", "state", "state_reason", "proof", "proof_reason",
            "first_seen", "earliest_report", "last_updated", "old_clues", "sources", "history", "decision",
            "outcome", "prediction", "analysis"]
    return {k: ev.get(k) for k in keep}


def build_state():
    test_mode = VIEW["test_mode"]
    settings = decision.load_settings()
    memory = load_memory(test_mode)
    factors = load_json("factors.json")["factors"]
    btc, gold = newest_result("btc_scout", test_mode), newest_result("gold_scout", test_mode)

    import supervisor
    now = pipeline.TEST_NOW if test_mode else datetime.now(timezone.utc)
    states = supervisor.asset_states(memory, [r for r in (btc, gold) if r], now)

    assets = {}
    for asset in ("Bitcoin", "Gold"):
        pts, source = chart_points(asset, test_mode)
        assets[asset] = {**states[asset], "points": pts, "price_source": source}

    events = sorted(memory["events"], key=lambda e: (e["earliest_report"], e["first_seen"]), reverse=True)
    factor_stats = []
    for f in factors:
        evs = [e for e in memory["events"] if e["factor"] == f["id"]]
        done = [e for e in evs if (e.get("outcome") or {}).get("effect") in ("right", "wrong")]
        right = [e for e in done if e["outcome"]["effect"] == "right"]
        wrong = [e for e in done if e["outcome"]["effect"] == "wrong"]
        flat = [e for e in evs if (e.get("outcome") or {}).get("effect") == "no_clear_move"]
        factor_stats.append({**f, "events": len(evs), "tracked": len(done), "right": len(right), "wrong": len(wrong),
                             "flat": len(flat)})

    runs_path = data_folder(test_mode) / "runs.json"
    runs = json.loads(runs_path.read_text(encoding="utf-8"))[-20:] if runs_path.exists() else []
    progress = load_json("progress.json") if (FOLDER / "progress.json").exists() else {}
    with pipeline.STATUS_LOCK:
        agents = [{"name": n, **{k: v for k, v in pipeline.AGENTS[n].items()}} for n in pipeline.AGENT_NAMES]
        log = list(pipeline.LOG[-120:])
    pid = settings.get("ai_provider", "none")
    keys = settings.get("ai_keys") or {}
    providers = [{"id": k, "name": v["name"], "needs_key": v["needs_key"], "has_key": bool(keys.get(k)),
                  "key_hint": ("..." + keys[k][-4:]) if keys.get(k) else "",
                  "model": (settings.get("ai_models") or {}).get(k) or v["model"], "default_model": v["model"]}
                 for k, v in decision.PROVIDERS.items()]
    return {
        "version": VERSION,
        "truststore": TRUSTSTORE_ACTIVE(),
        "now": datetime.now(timezone.utc).isoformat(),
        "test_mode": test_mode,
        "running": pipeline.is_running(),
        "next_run": SCHEDULE["next_run"],
        "last_run": pipeline.load_state(test_mode).get("last_run_real_time"),
        "settings": {"auto_run": settings["auto_run"], "interval_minutes": settings["interval_minutes"],
                     "ai_provider": pid, "providers": providers, "ai_ready": bool(decision.ai_choice(settings)),
                     "gold_news_every_hours": settings.get("gold_news_every_hours", 24),
                     "contact_email": settings.get("contact_email", "")},
        "assets": assets,
        "calendar": (gold or {}).get("calendar", {}),
        "data_checks": (btc or {}).get("data_checks", {}),
        "events": [public_event(e) for e in events[:150]],
        "factors": factor_stats,
        "agents": agents,
        "log": log,
        "runs": runs,
        "progress": progress,
        "windows": os.name == "nt",
        "social": social_state(test_mode),
        "telegram": {"set_up": bool(settings.get("telegram_token")), "username": settings.get("telegram_username", ""),
                     "channel": settings.get("telegram_channel", ""),
                     "subscribers": len(telegram_bot.load_subscribers()),
                     "gold": sum("Gold" in s.get("assets", []) for s in telegram_bot.load_subscribers().values()),
                     "bitcoin": sum("Bitcoin" in s.get("assets", []) for s in telegram_bot.load_subscribers().values()),
                     "error": TELEGRAM["error"], "log": telegram_log()[-12:]},
        "timing": smart_minutes(settings)[1] if settings.get("auto_run") else "",
        "weekly": weekly.load_all(test_mode)[-12:],
        "library": library_state(test_mode, memory),
    }


# ---------------------------------------------------------------------------
# Desktop icon and start-with-computer (Windows)
# ---------------------------------------------------------------------------
def pythonw_path():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def make_shortcut(where):
    """where = 'Desktop' or 'Startup'. Uses Windows' own shortcut maker through PowerShell."""
    if os.name != "nt":
        raise RuntimeError("This button works on Windows only.")
    icon = FOLDER / "mint.ico"
    q = lambda x: str(x).replace("'", "''")      # PowerShell needs ' written as '' inside quotes
    script = (
        f"$folder=[Environment]::GetFolderPath('{where}');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $folder 'MINT.lnk'));"
        f"$s.TargetPath='{q(pythonw_path())}';"
        f"$s.Arguments='\"{q(FOLDER / 'MINT.pyw')}\"';"
        f"$s.WorkingDirectory='{q(FOLDER)}';"
        + (f"$s.IconLocation='{q(icon)}';" if icon.exists() else "") +
        "$s.Description='MINT - Market Intelligence and News Tracker';$s.Save();Write-Output $folder"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                         timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300] or "PowerShell failed")
    return out.stdout.strip()


def startup_shortcut_exists():
    if os.name != "nt":
        return False
    appdata = os.environ.get("APPDATA", "")
    return (Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/MINT.lnk").exists()


def remove_startup():
    appdata = os.environ.get("APPDATA", "")
    p = Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/MINT.lnk"
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# The timer
# ---------------------------------------------------------------------------
def smart_minutes(s):
    """How long to wait before the next automatic run, and why (shown in Settings).
    Smart timing: every 30 minutes normally, every 15 minutes while something is on Watch (to catch the
    second source quickly) or when a gold calendar event is tomorrow. Never more often than every 15 minutes,
    so the free websites MINT reads are not overloaded."""
    if s.get("interval_minutes") != "smart":
        return max(15, int(s["interval_minutes"])), f"every {s['interval_minutes']} minutes (fixed)"
    memory = load_memory(False)
    watching = [e for e in memory["events"] if e["state"] in ("watch", "confirmed")]
    if watching:
        return 15, f"every 15 minutes: {len(watching)} event(s) on Watch"
    gold = newest_result("gold_scout", False)
    if gold and gold.get("calendar", {}).get("warnings"):
        return 15, "every 15 minutes: a gold calendar event is tomorrow"
    return 30, "every 30 minutes: nothing on Watch"


def scheduler():
    while True:
        try:
            s = decision.load_settings()
            if s.get("auto_run"):
                if not SCHEDULE["next_run"]:
                    SCHEDULE["next_run"] = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
                if datetime.now(timezone.utc) >= datetime.fromisoformat(SCHEDULE["next_run"]):
                    pipeline.run_cycle(test_mode=False, reason="automatic")
                    SCHEDULE["next_run"] = (datetime.now(timezone.utc) +
                                            timedelta(minutes=smart_minutes(s)[0])).isoformat()
            else:
                SCHEDULE["next_run"] = None
        except Exception:
            traceback.print_exc()
        time.sleep(10)


def telegram_listener():
    """Every few seconds: answer people who messaged the bot (sign-ups, /status, /stop)."""
    while True:
        token = decision.load_settings().get("telegram_token", "")
        if token:
            try:
                for n in telegram_bot.check_messages(token):
                    pipeline.log("Telegram bot", n)
                TELEGRAM["error"] = ""
            except Exception as e:
                TELEGRAM["error"] = str(e)[:200]
                time.sleep(20)
        time.sleep(4)


# ---------------------------------------------------------------------------
# The web server (only for this computer)
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass                                           # keep quiet

    def safe(self):
        """Only accept requests made by the MINT page itself on this computer."""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def reply(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.safe():
            return self.reply(403, {"error": "not allowed"})
        if self.path in ("/", "/index.html"):
            return self.reply(200, (FOLDER / "ui" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            try:
                state = build_state()
                state["startup"] = startup_shortcut_exists()
                return self.reply(200, state)
            except Exception as e:
                traceback.print_exc()
                return self.reply(500, {"error": str(e)})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.safe() or self.headers.get("X-MINT") != "1":
            return self.reply(403, {"error": "not allowed"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            return self.reply(200, self.action(self.path, body))
        except Exception as e:
            traceback.print_exc()
            return self.reply(400, {"error": str(e)})

    def action(self, path, body):
        if path == "/api/run":
            test = bool(body.get("test"))
            if pipeline.is_running():
                return {"message": "A run is already going."}
            VIEW["test_mode"] = test
            threading.Thread(target=pipeline.run_cycle, args=(test, "test" if test else "manual"),
                             daemon=True).start()
            return {"message": "Test run started (sample data)." if test else "Run started."}
        if path == "/api/view":
            VIEW["test_mode"] = bool(body.get("test"))
            return {"message": "Showing test data." if VIEW["test_mode"] else "Showing real data."}
        if path == "/api/settings":
            s = decision.load_settings()
            if "auto_run" in body:
                s["auto_run"] = bool(body["auto_run"])
                SCHEDULE["next_run"] = None
            if "interval_minutes" in body:
                v = body["interval_minutes"]
                s["interval_minutes"] = "smart" if v == "smart" else max(15, min(720, int(v)))
                SCHEDULE["next_run"] = None
            if "gold_news_every_hours" in body:
                s["gold_news_every_hours"] = max(1, min(168, int(body["gold_news_every_hours"])))
            if "contact_email" in body:
                email = str(body["contact_email"]).strip()
                if email and ("@" not in email or " " in email or len(email) > 120):
                    raise RuntimeError("That does not look like an email address.")
                s["contact_email"] = email
            if "ai_provider" in body:
                if body["ai_provider"] not in list(decision.PROVIDERS) + ["none"]:
                    raise RuntimeError("Unknown AI service.")
                s["ai_provider"] = body["ai_provider"]
            target = body.get("provider") or s.get("ai_provider")
            if "ai_key" in body and target in decision.PROVIDERS:
                s.setdefault("ai_keys", {})[target] = str(body["ai_key"]).strip()
            if "ai_model" in body and target in decision.PROVIDERS:
                s.setdefault("ai_models", {})[target] = str(body["ai_model"]).strip()
            decision.save_settings(s)
            return {"message": "Saved."}
        if path == "/api/library/add":
            import library
            e = library.add_researched(VIEW["test_mode"], body.get("date", ""), body.get("asset", ""),
                                       body.get("factor", ""), body.get("title", ""))
            return {"message": f"Added: {e['asset']} moved {e['d1']:+.1f}% on the day"
                               + (f", {e['d7']:+.1f}% after 7 days." if e["d7"] is not None else ".")}
        if path == "/api/library/delete":
            import library
            library.delete_researched(VIEW["test_mode"], body.get("id", ""))
            return {"message": "Removed."}
        if path == "/api/test-ai":
            return {"message": decision.test_ai()}
        if path == "/api/install-cert-fix":
            exe = sys.executable.replace("pythonw", "python")
            out = subprocess.run([exe, "-m", "pip", "install", "truststore"], capture_output=True, text=True,
                                 timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if out.returncode != 0:
                raise RuntimeError("Install failed: " + (out.stderr or out.stdout)[-300:])
            return {"message": "Certificate fix installed. Now press Stop MINT and open MINT again."}
        if path == "/api/check-connection":
            return {"results": check_connection()}
        if path == "/api/weekly-preview":
            now = pipeline.TEST_NOW if VIEW["test_mode"] else datetime.now(timezone.utc)
            return {"output": weekly.build(VIEW["test_mode"], now)["text"]}
        if path == "/api/telegram":
            s = decision.load_settings()
            token = str(body.get("token", "")).strip()
            if token:
                s["telegram_username"] = telegram_bot.check_token(token)
                s["telegram_token"] = token
                decision.save_settings(s)
                return {"message": f"Connected to @{s['telegram_username']}."}
            s["telegram_token"], s["telegram_username"] = "", ""
            decision.save_settings(s)
            return {"message": "Telegram bot disconnected."}
        if path == "/api/telegram-channel":
            s = decision.load_settings()
            ch = str(body.get("channel", "")).strip()
            if ch and not (ch.startswith("@") or ch.lstrip("-").isdigit()):
                ch = "@" + ch.lstrip("@").split("/")[-1]          # accept t.me/name or name
            if ch:
                telegram_bot.call(s["telegram_token"], "sendMessage", {
                    "chat_id": ch, "text": "MINT is connected to this channel. Every event, update and result will be posted here."})
            s["telegram_channel"] = ch
            decision.save_settings(s)
            return {"message": f"Connected to channel {ch}: a test post was sent." if ch else "Channel removed."}
        if path == "/api/telegram-test":
            token = decision.load_settings().get("telegram_token", "")
            if not token:
                raise RuntimeError("Set up the bot first.")
            n = telegram_bot.send_test(token)
            return {"message": f"Test message sent to {n} subscriber(s)." if n else
                    "No subscribers yet: open the bot in Telegram and press Start first."}
        if path == "/api/social":
            path_ = FOLDER / "social_accounts.json"
            config = json.loads(path_.read_text(encoding="utf-8"))
            for a in config["accounts"]:
                if a["name"] == body.get("name"):
                    a["on"] = bool(body.get("on"))
            path_.write_text(json.dumps(config, indent=2), encoding="utf-8")
            return {"message": f"{body.get('name')} switched {'on' if body.get('on') else 'off'}."}
        if path == "/api/shortcut":
            where = make_shortcut("Desktop")
            return {"message": f"MINT icon added to your Desktop ({where})."}
        if path == "/api/startup":
            if body.get("on"):
                make_shortcut("Startup")
                return {"message": "MINT will start when your computer starts."}
            remove_startup()
            return {"message": "MINT will no longer start with your computer."}
        if path in ("/api/test-supervisor", "/api/test-telegram"):
            script = "test_supervisor.py" if path.endswith("supervisor") else "test_telegram.py"
            out = subprocess.run([sys.executable.replace("pythonw", "python"), str(FOLDER / script)],
                                 capture_output=True, text=True, timeout=120, cwd=FOLDER,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"message": "Tests finished.", "output": out.stdout + out.stderr}
        if path == "/api/reset-real":
            for name in ("seen_events.json", "supervisor_last.json", "pipeline_state.json"):
                (DATA / name).unlink(missing_ok=True)
            return {"message": "MINT's memory was cleared. The next run is a first run: it only learns, no alerts."}
        if path == "/api/reset-test":
            import shutil
            shutil.rmtree(DATA / "test", ignore_errors=True)
            return {"message": "Test data cleared."}
        if path == "/api/stop":
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return {"message": "MINT stopped. You can close this tab."}
        raise RuntimeError("unknown action")


def already_running():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main(open_browser=True):
    if already_running():                       # MINT is already open: just show it
        webbrowser.open(ADDRESS)
        return
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    print(f"{datetime.now():%Y-%m-%d %H:%M} MINT started at {ADDRESS}")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(ADDRESS)).start()
    server.serve_forever()


if __name__ == "__main__":
    main(open_browser="--no-browser" not in sys.argv)
