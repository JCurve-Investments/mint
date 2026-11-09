"""
Tests for the Telegram bot (18 checks), using a PRETEND Telegram server on this computer (nothing is really sent).
Uses its own temporary folder (data/telegramtest), so real subscribers are never touched.

Run:  py test_telegram.py   (or press "Test the Telegram bot" in the MINT app)
"""
import contextlib
import io
import json
import shutil
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import supervisor
import telegram_bot as tb
import weekly
from common import FOLDER

FOLDER_T = FOLDER / "data" / "telegramtest"
shutil.rmtree(FOLDER_T, ignore_errors=True)
pretend_folder = lambda test_mode: (FOLDER_T.mkdir(parents=True, exist_ok=True), FOLDER_T)[1]
tb.data_folder = supervisor.data_folder = weekly.data_folder = pretend_folder

sent, results = [], []
MESSAGES = [
    {"update_id": 1, "message": {"chat": {"id": 111, "type": "private"}, "text": "/start"}},
    {"update_id": 2, "message": {"chat": {"id": 111, "type": "private"}, "text": "Bitcoin"}},
    {"update_id": 3, "message": {"chat": {"id": 222, "type": "private"}, "text": "/start"}},
    {"update_id": 4, "message": {"chat": {"id": 333, "type": "private"}, "text": "/start"}},
    {"update_id": 5, "message": {"chat": {"id": 333, "type": "private"}, "text": "/gold"}},
    {"update_id": 6, "message": {"chat": {"id": -99, "type": "group"}, "text": "/start"}},
]


class PretendTelegram(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        method = self.path.split("/")[-1]
        if "BADTOKEN" in self.path:
            res = {"ok": False, "description": "Unauthorized"}
        elif method == "getMe":
            res = {"ok": True, "result": {"username": "mint_test_bot"}}
        elif method == "getUpdates":
            res = {"ok": True, "result": [u for u in MESSAGES if u["update_id"] >= body.get("offset", 0)]}
        else:
            sent.append(body)
            res = {"ok": True, "result": {}}
        self.send_response(200 if res["ok"] else 401)
        self.end_headers()
        self.wfile.write(json.dumps(res).encode())


server = HTTPServer(("127.0.0.1", 0), PretendTelegram)
threading.Thread(target=server.serve_forever, daemon=True).start()
tb.API = f"http://127.0.0.1:{server.server_port}"


def check(name, ok):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}")


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


try:
    tb.check_token("BADTOKEN")
    check("1. A wrong token is rejected", False)
except Exception:
    check("1. A wrong token is rejected", True)
check("2. A good token gives the bot's username", tb.check_token("T") == "mint_test_bot")
tb.check_messages("T")
subs = tb.load_subscribers()
check("3. Three people signed up; a group chat is ignored", sorted(subs) == ["111", "222", "333"])
check("4. Each person's choice is stored (Bitcoin / both / gold)",
      subs["111"]["assets"] == ["Bitcoin"] and subs["333"]["assets"] == ["Gold"] and len(subs["222"]["assets"]) == 2)
n = len(sent)
tb.check_messages("T")
check("5. Old messages are not answered twice", len(sent) == n)

now = datetime.now(timezone.utc)
CH = "@mint_test_channel"


def ev(i, asset, state, hours=1, **extra):
    e = {"id": i, "asset": asset, "state": state, "title": f"Event {i}", "factor": "B2",
         "sources": [{"organisation": "Reuters"}], "state_reason": "test", "first_seen": (now - timedelta(hours=hours)).isoformat(),
         "history": [], "prediction": {"direction": "down", "reason": "test", "similar": [
             {"date": "2025-02-21", "title": "Bybit hacked", "d1": -2.0}]},
         "analysis": {"by": "rules", "confidence": "low", "confidence_reason": "test"}, "decision": None, "outcome": {}}
    if state == "alerted":
        e["decision"] = {"time": (now - timedelta(hours=hours)).isoformat(), "alert_title": i,
                         "telegram_message": f"MINT alert {i}\nRisk information only, not financial advice."}
    e.update(extra)
    return e


def to(text_part):
    return sorted(str(m["chat_id"]) for m in sent if text_part in m["text"])


supervisor.save_memory({"events": [ev("BTC-1", "Bitcoin", "alerted"), ev("GOLD-1", "Gold", "alerted"),
                                   ev("BTC-OLD", "Bitcoin", "alerted", hours=10)], "counter": {}}, False)
sent.clear()
quiet(tb.send_alerts, "T", False, now)
check("6. A Bitcoin alert goes only to people who chose Bitcoin", to("MINT alert BTC-1") == ["111", "222"])
check("7. A gold alert goes only to people who chose gold", to("MINT alert GOLD-1") == ["222", "333"])
check("8. An alert older than 6 hours is not sent", to("MINT alert BTC-OLD") == [])
sent.clear()
quiet(tb.send_alerts, "T", False, now)
check("9. Nothing is sent twice", sent == [])
supervisor.save_memory({"events": [ev("T-1", "Bitcoin", "alerted")], "counter": {}}, True)
quiet(tb.send_alerts, "T", True)
check("10. A test run sends nothing", sent == [])
MESSAGES.append({"update_id": 7, "message": {"chat": {"id": 111, "type": "private"}, "text": "/stop"}})
MESSAGES.append({"update_id": 8, "message": {"chat": {"id": 222, "type": "private"}, "text": "Alerts only"}})
tb.check_messages("T")
subs = tb.load_subscribers()
check("11. /stop removes the person", "111" not in subs)
check("12. New subscribers get 'Everything' by default; 'Alerts only' can be chosen",
      subs["333"]["level"] == "everything" and subs["222"]["level"] == "alerts")

watch = ev("GOLD-W", "Gold", "watch")
supervisor.save_memory({"events": [watch], "counter": {}}, False)
sent.clear()
quiet(tb.send_alerts, "T", False, now, [], CH)
check("13. EVERY new event (here a Watch) goes to 'Everything' people and the channel, with MINT's view",
      to("Event GOLD-W") == ["333", CH] and "Prediction (next day)" in sent[0]["text"] and "Similar past events" in sent[0]["text"])
mem = supervisor.load_memory(False)
mem["events"][0].update(state="expired")
supervisor.save_memory(mem, False)
sent.clear()
quiet(tb.send_alerts, "T", False, now, [], CH)
check("14. When its status changes, an update is sent", to("MINT update") == ["333", CH] and "NOT CONFIRMED" in sent[0]["text"])
mem = supervisor.load_memory(False)
mem["events"][0]["outcome"] = {"effect": "right", "effect_reason": "predicted down: -0.8% after 1 day",
                               "d1": {"move": -0.8, "time": now.isoformat()}}
supervisor.save_memory(mem, False)
sent.clear()
quiet(tb.send_alerts, "T", False, now, [], CH)
quiet(tb.send_alerts, "T", False, now, [], CH)
check("15. After 1 day the RESULT is sent once (was the prediction right?)",
      to("MINT result") == ["333", CH] and "prediction RIGHT" in sent[0]["text"])
w2 = ev("BTC-W", "Bitcoin", "alerted", history=[{"time": now.isoformat(), "note": "new -> watch: 1 source"}])
supervisor.save_memory({"events": [w2], "counter": {}}, False)
sent.clear()
quiet(tb.send_alerts, "T", False, now)
check("16. An alert that started as a Watch says so ('was on Watch since')",
      sent and all("was on Watch since" in m["text"] for m in sent))
supervisor.save_memory({"events": [], "counter": {}}, False)
sent.clear()
quiet(tb.send_alerts, "T", False, now, ["Tomorrow: US jobs report. Gold may move."])
quiet(tb.send_alerts, "T", False, now, ["Tomorrow: US jobs report. Gold may move."])
check("17. A heads-up before a gold event is sent once, to 'Everything' people", [str(m["chat_id"]) for m in sent] == ["333"])
sunday = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)
sent.clear()
quiet(tb.send_alerts, "T", False, sunday, [], CH)
quiet(tb.send_alerts, "T", False, sunday, [], CH)
check("18. The weekly summary goes to everyone and the channel on Sunday, only once, and is saved",
      sorted(str(m["chat_id"]) for m in sent) == ["222", "333", CH] and len(weekly.load_all(False)) == 1
      and "How good were MINT's predictions" in sent[0]["text"])

print(f"\nRESULT: {sum(results)} of {len(results)} tests passed.")
server.shutdown()
shutil.rmtree(FOLDER_T, ignore_errors=True)
