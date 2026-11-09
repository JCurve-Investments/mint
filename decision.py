"""
MINT Decision agent
===================
The only part of MINT that uses AI.

It takes every event the Supervisor CONFIRMED (enough proof) and decides:
  - Is this really a NEW event, or an article about something that happened earlier?
    (it looks for the real event date in the text it is given)
  - Should an alert be sent?
  - What should the alert say, in plain words?

Rules it must follow (they are written into the instructions it gets):
  - use ONLY the headlines and descriptions MINT gives it - no outside knowledge, no web search
  - never give buy/sell advice and never predict a price
  - say how much the price had ALREADY moved when MINT noticed (so nobody thinks it is early)

Two modes:
  AI mode     - uses an AI service chosen in the MINT app (Settings): Groq (free tier), Google Gemini
                (free tier), Ollama (free, runs on your own computer) or Anthropic Claude (paid).
                Keys stay on this computer in data/settings.json.
  Rules mode  - no key: a simple fixed rule writes the alert. If the headline looks like an older
                event, rules mode does NOT alert (it cannot check the date).

Telegram is not connected yet: the finished message is saved with the event, ready to send.

Run on its own:  py decision.py          py decision.py --test
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone

from common import FOLDER, load_json
from supervisor import data_folder, load_memory, save_memory, parse_time, hours_between, asset_states

DEFAULT_MODEL = "claude-haiku-5-5"      # the cheapest Claude model; can be changed in Settings

# The AI services the Decision agent can use. All except Anthropic have a free option.
# "openai" style means they all accept the same kind of request, so one piece of code works for all three.
PROVIDERS = {
    "groq":      {"name": "Groq (free tier - recommended)", "style": "openai", "needs_key": True,
                  "url": "https://api.groq.com/openai/v1/chat/completions", "model": "openai/gpt-oss-120b"},
    "openrouter": {"name": "OpenRouter free models (50 a day)", "style": "openai", "needs_key": True,
                  "url": "https://openrouter.ai/api/v1/chat/completions", "model": "openai/gpt-oss-120b:free"},
    "gemini":    {"name": "Google Gemini (free tier - Google may use what you send)", "style": "openai",
                  "needs_key": True, "model": "gemini-flash-latest",
                  "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"},
    "ollama":    {"name": "Ollama (free, runs on this computer)", "style": "openai", "needs_key": False,
                  "url": "http://localhost:11434/v1/chat/completions", "model": "llama3.2"},
    "anthropic": {"name": "Anthropic Claude (paid)", "style": "anthropic", "needs_key": True,
                  "url": "https://api.anthropic.com/v1/messages", "model": DEFAULT_MODEL},
}
TEST_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

INSTRUCTIONS = """You are the Decision agent of MINT, a student project that warns people about news risks for Gold and Bitcoin.
Another program (the Supervisor) has already checked that the sources below are trusted and that there is enough proof.
Your job is to decide if this is a NEW event worth an alert, and to write the alert.

Strict rules:
1. Use ONLY the information given below. Do not add facts, names, numbers or dates from your own knowledge.
2. Decide the date the event itself happened, using only the text given. Article dates are given too.
   If the text says or suggests the event happened earlier (for example "last week", "a year after", an older year),
   it is NOT a new event. If the text gives no clue, assume the event happened around the first report.
3. Never give financial advice (no buy, sell, hold). Never predict a price or a percentage.
4. Write for a 15-year-old: short sentences, simple words, no jargon.
5. If the price had already moved before MINT noticed, say so plainly.

Answer with ONLY a JSON object, no other text, in exactly this shape:
{
  "is_new_event": true or false,
  "event_date": "YYYY-MM-DD" or "unknown",
  "date_evidence": "one short sentence: what in the text shows the date",
  "send_alert": true or false,
  "impact": "high" or "medium" or "low",
  "possible_effect": "one short phrase, for example: risk of a drop",
  "expected_direction": "up" or "down" or "unclear" (which way the asset's price is likely to move, based on how this kind of event has affected it in the past; "unclear" if the text does not say),
  "direction_reason": "one short sentence: why that direction",
  "alert_title": "max 10 words",
  "alert_message": "max 45 words: what happened and why it can matter for the asset",
  "why_it_matters": ["short reason 1", "short reason 2"],
  "reason": "one sentence explaining your decision"
}"""


# ---------------------------------------------------------------------------
# Settings (the API key lives here, on this computer only)
# ---------------------------------------------------------------------------
def cloud_settings(s):
    """In the cloud (GitHub Actions), keys come from GitHub SECRETS (environment variables), never from a file,
    so they can never end up in the public repository."""
    import os
    if os.environ.get("MINT_CLOUD") != "1":
        return s
    env = os.environ.get
    if env("GROQ_KEY"):
        s["ai_provider"] = "groq"
        s["ai_keys"] = {"groq": env("GROQ_KEY")}
    s["telegram_token"] = env("TELEGRAM_TOKEN", "")
    s["telegram_channel"] = env("TELEGRAM_CHANNEL", "")
    s["telegram_channel_link"] = env("TELEGRAM_CHANNEL_LINK", "")   # invite link, for a private channel
    s["contact_email"] = env("CONTACT_EMAIL", "")
    return s


def load_settings():
    path = data_folder(False) / "settings.json"
    defaults = {"auto_run": False, "interval_minutes": "smart", "ai_provider": "none", "ai_keys": {}, "ai_models": {},
                "gold_news_every_hours": 24}
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    cloud_settings(defaults)
    if defaults.get("ai_key"):                     # older settings file: one Anthropic key
        defaults["ai_keys"].setdefault("anthropic", defaults.pop("ai_key"))
        if defaults["ai_provider"] == "none":
            defaults["ai_provider"] = "anthropic"
    return defaults


def ai_choice(settings):
    """(provider id, key, model) if AI can be used, otherwise None."""
    pid = settings.get("ai_provider", "none")
    if pid not in PROVIDERS:
        return None
    key = (settings.get("ai_keys") or {}).get(pid, "").strip()
    if PROVIDERS[pid]["needs_key"] and not key:
        return None
    model = (settings.get("ai_models") or {}).get(pid) or PROVIDERS[pid]["model"]
    return pid, key, model


def save_settings(settings):
    (data_folder(False) / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Asking Claude
# ---------------------------------------------------------------------------
def describe_event(ev, now, factor):
    """Everything the AI is allowed to know about the event, as plain text."""
    o = ev.get("outcome") or {}
    lines = [
        f"Time now (UTC): {now:%Y-%m-%d %H:%M}",
        f"Asset: {ev['asset']}",
        f"Factor: {factor.get('name', ev['factor'])} (past pattern: {factor.get('effect', 'unknown')})",
        f"Proof checked by the Supervisor: {ev['proof_reason']}",
        f"First report published (UTC): {ev['earliest_report'][:16].replace('T', ' ')}",
    ]
    if ev.get("old_clues"):
        lines.append(f"Warning from the Supervisor: headline words suggest an older event: {', '.join(ev['old_clues'])}")
    if o.get("moved_before_mint") is not None:
        lines.append(f"{ev['asset']} price change between the first report and when MINT noticed it: "
                     f"{o['moved_before_mint']:+.1f}%")
    lines.append("\nSources (trusted, already checked):")
    for i, s in enumerate(ev["sources"], 1):
        lines.append(f"{i}. {s['organisation']} ({s['kind']}), published {str(s['published'])[:16].replace('T', ' ')} UTC")
        lines.append(f"   Headline: {s['title']}")
        if s.get("summary"):
            lines.append(f"   Description: {s['summary'][:500]}")
    return "\n".join(lines)


def ask_ai(text, provider, key, model, system=None):
    system = system or INSTRUCTIONS
    """Send the event to the chosen AI service and return its answer as a dictionary."""
    p = PROVIDERS[provider]
    if p["style"] == "anthropic":
        body = {"model": model, "max_tokens": 900, "system": system,
                "messages": [{"role": "user", "content": text}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
                   "User-Agent": "MINT/1.0 (student project)"}
    else:
        body = {"model": model, "temperature": 0.2, "max_tokens": 1500,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
        # A User-Agent is needed: without it Groq's security (Cloudflare) refuses the request with error 1010
        headers = {"content-type": "application/json", "User-Agent": "MINT/1.0 (student project)"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(p["url"], data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=90) as r:
            reply = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{p['name']} error {e.code}: {detail}")
    except urllib.error.URLError as e:
        hint = " - is Ollama installed and running?" if provider == "ollama" else ""
        raise RuntimeError(f"could not reach {p['name']}: {e.reason}{hint}")
    if p["style"] == "anthropic":
        answer = "".join(part.get("text", "") for part in reply.get("content", []))
    else:
        answer = (reply.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    match = re.search(r"\{.*\}", answer, re.S)
    if not match:
        raise RuntimeError("the AI did not answer in the right format")
    return json.loads(match.group(0))


def check_answer(a):
    """Make sure the AI's answer has every field, with sensible values. Raises an error if not."""
    needed = ["is_new_event", "event_date", "send_alert", "impact", "alert_title", "alert_message", "reason"]
    missing = [k for k in needed if k not in a]
    if missing:
        raise RuntimeError(f"the AI's answer is missing: {', '.join(missing)}")
    if a["impact"] not in ("high", "medium", "low"):
        a["impact"] = "medium"
    if not a["is_new_event"]:
        a["send_alert"] = False            # never alert on an old event, even if the AI says so
    banned = re.compile(r"\b(buy now|sell now|you should (buy|sell|hold)|time to (buy|sell)|invest now|"
                        r"price will|guaranteed?|we recommend)\b", re.I)
    if banned.search(a["alert_message"] + " " + a["alert_title"]):
        raise RuntimeError("the AI's message sounded like financial advice - not used")
    a.setdefault("why_it_matters", [])
    a.setdefault("date_evidence", "")
    a.setdefault("possible_effect", "")
    return a


# ---------------------------------------------------------------------------
# Rules mode (no AI key)
# ---------------------------------------------------------------------------
def rules_decision(ev, factor):
    effect = factor.get("effect", "")
    if ev.get("old_clues"):
        return {"is_new_event": False, "event_date": "unknown",
                "date_evidence": f"headline words: {', '.join(ev['old_clues'])}",
                "send_alert": False, "impact": "low", "possible_effect": "",
                "alert_title": ev["title"][:80], "alert_message": "",
                "why_it_matters": [],
                "reason": "The headline suggests an older event and rules mode cannot check the date, so no alert."}
    orgs = sorted({s["organisation"] for s in ev["sources"]})
    return {"is_new_event": True, "event_date": ev["earliest_report"][:10],
            "date_evidence": "no date clue in the headlines, so the first report date is used",
            "send_alert": True, "impact": "high" if factor.get("strength") == "strong" else "medium",
            "possible_effect": "risk of a drop" if factor.get("expected_move") == "down" else "the price may move",
            "alert_title": ev["title"][:80],
            "alert_message": f"{' and '.join(orgs)} {'report' if len(orgs) > 1 else 'reports'}: {ev['title']}. "
                             f"In the past, events like this ({factor.get('name', '').lower()}) have often been "
                             f"followed by: {effect.lower()}.",
            "why_it_matters": [f"Past pattern for this factor: {effect}."],
            "reason": "Rules mode: enough proof and no sign of an older event."}


def telegram_text(ev, d):
    o = ev.get("outcome") or {}
    lines = [f"MINT alert · {ev['asset']}", d["alert_title"], "", d["alert_message"]]
    pred = ev.get("prediction") or {}
    if pred.get("direction") in ("up", "down"):
        lines.append(f"\nMINT's prediction: {ev['asset']} {pred['direction'].upper()} - {pred.get('reason', '')}")
    if o.get("moved_before_mint") is not None:
        lines.append(f"\n{ev['asset']} had already moved {o['moved_before_mint']:+.1f}% since the first report.")
    lines.append("Sources: " + ", ".join(sorted({s["organisation"] for s in ev["sources"]})))
    lines.append("Risk information only, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The main job
# ---------------------------------------------------------------------------
PREDICT_INSTRUCTIONS = """You help MINT, a student project, predict which way a news event is likely to move the price
of Gold or Bitcoin over the next day, using what is generally known about how this kind of event has affected the price
in the past (for example: a stronger US dollar or higher interest rates have usually pushed gold down; exchange hacks
and crackdowns have usually pushed Bitcoin down; big inflows into funds have usually pushed prices up).
Use only the headline given. You MUST choose "up" or "down" - pick the more likely one even if it is close.
Answer with ONLY a JSON object: {"direction": "up" or "down", "reason": "one short sentence a 15-year-old understands"}"""
AI_PREDICTIONS_PER_RUN = 15


def ai_predictions(memory, choice, factors):
    """Give every event whose headline the factor rules could not read ('unclear') a prediction from the AI.
    Each event is asked once. Returns how many predictions were added."""
    provider, key, model = choice
    added = 0
    for ev in memory["events"]:
        pred = ev.get("prediction") or {}
        if pred.get("direction") != "unclear" or pred.get("by") == "AI" or ev.get("ai_predict_tried"):
            continue
        if added >= AI_PREDICTIONS_PER_RUN:
            break
        ev["ai_predict_tried"] = True
        f = factors.get(ev["factor"], {})
        text = (f"Asset: {ev['asset']}\nFactor: {f.get('name', ev['factor'])} (past pattern: {f.get('effect', '?')})\n"
                f"Headline: {ev['title']}\nMINT's own evidence so far: {pred.get('reason', 'none')}")
        try:
            a = ask_ai(text, provider, key, model, PREDICT_INSTRUCTIONS)
            if a.get("direction") in ("up", "down"):
                ev["prediction"] = {"direction": a["direction"], "by": "AI",
                                    "reason": str(a.get("reason", "AI prediction"))[:200]}
                added += 1
                print(f"   PREDICTION [{ev['id']}] {ev['asset']} {a['direction'].upper()}: {ev['title'][:70]}")
        except Exception as e:
            print(f"   AI could not predict [{ev['id']}]: {e}")
            break
    return added


ANALYSE_INSTRUCTIONS = """You are the Decision agent of MINT, a student project that tracks news risks for Gold and Bitcoin.
For ONE news event you get: the headline and sources, MINT's background knowledge about the factor, and the most
similar PAST events with what the price REALLY did 1 day later (real prices).
Write MINT's view of the event. Rules:
1. Base your prediction mainly on the similar past events and the factor knowledge. Do not invent facts or numbers.
2. If the past events disagree or there are few of them, say so honestly.
3. Choose "up" or "down" for the likely 1-day direction.
4. Plain words for a 15-year-old. No buy/sell advice. Never promise a price.
Answer with ONLY a JSON object:
{"summary": "1-2 sentences: what happened and why it can matter",
 "what_past_events_show": "1-2 sentences about what happened after similar past events (use the numbers given)",
 "direction": "up" or "down",
 "why": ["short reason 1", "short reason 2"],
 "watch_next": "one sentence: what would make this bigger or smaller"}"""
ANALYSES_PER_RUN = 25


def knowledge(factor_id):
    try:
        k = json.loads((FOLDER / "factor_knowledge.json").read_text(encoding="utf-8"))["factors"].get(factor_id, {})
    except Exception:
        k = {}
    return k


def confidence(pred):
    """How much evidence is behind a prediction - from the past events, never a made-up percentage."""
    sims = [s for s in (pred.get("similar") or []) if s.get("d1") is not None]
    d = pred.get("direction")
    if d not in ("up", "down"):
        return "low", "no clear evidence"
    agree = [s for s in sims if (s["d1"] > 0) == (d == "up") and abs(s["d1"]) >= 0.1]
    share = len(agree) / len(sims) if sims else 0
    if len(sims) >= 4 and share >= 0.75:
        return "high", f"{len(agree)} of {len(sims)} similar past events moved this way"
    if len(sims) >= 2 and share >= 0.6:
        return "medium", f"{len(agree)} of {len(sims)} similar past events moved this way"
    if sims:
        return "low", f"only {len(agree)} of {len(sims)} similar past events moved this way"
    return "low", "no similar past events yet - based on the factor pattern only"


def analyse(ev, factor, choice):
    """MINT's view of one event. Uses the AI if switched on, otherwise fixed rules. Returns a dict."""
    pred = ev.get("prediction") or {}
    k = knowledge(ev["factor"])
    sims = pred.get("similar") or []
    if choice:
        provider, key, model = choice
        lines = [f"Asset: {ev['asset']}", f"Factor: {factor.get('name', ev['factor'])}",
                 f"Headline: {ev['title']}",
                 "Sources: " + ", ".join(sorted({s['organisation'] for s in ev['sources']})),
                 f"MINT status: {ev.get('state_reason', '')}",
                 "Background knowledge: " + " ".join(f"{a.replace('_', ' ')}: {b}" for a, b in k.items()),
                 "Similar past events (real 1-day price moves):"]
        lines += [f"- {s['date']}: {s['title']} -> {s['d1']:+.1f}% after 1 day" for s in sims] or ["- none yet"]
        a = ask_ai("\n".join(lines), provider, key, model, ANALYSE_INSTRUCTIONS)
        if a.get("direction") not in ("up", "down") or not a.get("summary"):
            raise RuntimeError("the AI's analysis was incomplete")
        banned = re.compile(r"\b(buy now|sell now|you should (buy|sell|hold)|time to (buy|sell)|price will reach)\b", re.I)
        if banned.search(json.dumps(a)):
            raise RuntimeError("the AI's analysis sounded like financial advice - not used")
        out = {"by": f"AI ({PROVIDERS[provider]['name'].split(' (')[0]})", **{x: a.get(x) for x in
               ("summary", "what_past_events_show", "direction", "why", "watch_next")}}
    else:
        out = {"by": "rules", "summary": ev["title"] + ".",
               "what_past_events_show": pred.get("reason", ""), "direction": pred.get("direction"),
               "why": [k["typical_reaction"]] if k.get("typical_reaction") else [],
               "watch_next": ("Bigger if: " + k["bigger_when"]) if k.get("bigger_when") else ""}
    return out


def analyse_events(memory, choice, factors, now):
    """Give every event that has no analysis yet MINT's view. Returns how many were analysed."""
    done = 0
    for ev in memory["events"]:
        if ev.get("analysis") or ev["state"] is None:
            continue
        if done >= ANALYSES_PER_RUN:
            break
        f = factors.get(ev["factor"], {})
        try:
            a = analyse(ev, f, choice)
        except Exception as e:
            print(f"   AI analysis problem [{ev['id']}]: {e}. Using rules for this one.")
            a = analyse(ev, f, None)
        pred = ev.get("prediction") or {}
        frozen = (ev.get("outcome") or {}).get("d1") is not None
        if a["by"].startswith("AI") and a.get("direction") in ("up", "down") and not frozen:
            ev["prediction"] = {**pred, "direction": a["direction"], "by": "AI + past events",
                                "reason": a.get("what_past_events_show") or pred.get("reason", "")}
        level, why = confidence(ev.get("prediction") or {})
        a["confidence"], a["confidence_reason"], a["time"] = level, why, now.isoformat()
        ev["analysis"] = a
        done += 1
        print(f"   ANALYSED  [{ev['id']}] {ev['asset']} {(ev.get('prediction') or {}).get('direction', '?').upper()} "
              f"({level} confidence): {ev['title'][:60]}")
    return done


def test_ai():
    """Send one tiny request to the chosen AI service, to check the key works. Returns a message."""
    import time
    choice = ai_choice(load_settings())
    if not choice:
        raise RuntimeError("No AI chosen, or no key saved yet (Settings > AI for the Decision agent).")
    provider, key, model = choice
    start = time.time()
    a = ask_ai("Asset: Gold\nFactor: US dollar (past pattern: dollar up, gold down)\n"
               "Headline: Dollar jumps to a 3-month high", provider, key, model, PREDICT_INSTRUCTIONS)
    return (f"{PROVIDERS[provider]['name'].split(' (')[0]} works ({model}, {time.time() - start:.1f}s). "
            f"Test answer: gold {a.get('direction', '?')} - {a.get('reason', '')}")


def run(test_mode=False, now=None):
    now = now or (TEST_NOW if test_mode else datetime.now(timezone.utc))
    settings = load_settings()
    choice = ai_choice(settings)
    mode = "AI" if choice else "rules"
    provider, key, model = choice if choice else (None, "", None)
    factors = {f["id"]: f for f in load_json("factors.json")["factors"]}
    memory = load_memory(test_mode)
    todo = [e for e in memory["events"] if e["state"] == "confirmed"]
    print(f"MINT DECISION AGENT  {now:%Y-%m-%d %H:%M} UTC{'  (TEST MODE)' if test_mode else ''}")
    print(f"Mode: {'AI: ' + PROVIDERS[provider]['name'] + ', ' + model if choice else 'rules (no AI chosen in Settings)'}. "
          f"{len(todo)} confirmed event(s) to decide.\n")
    n = analyse_events(memory, choice, factors, now)
    if n:
        print(f"   Wrote MINT's view for {n} event(s).\n")

    summary = {"run_time": now.isoformat(), "mode": mode, "decided": 0, "alerts": 0, "rejected": 0, "errors": []}
    for ev in todo:
        factor = factors.get(ev["factor"], {})
        used = mode
        try:
            if choice:
                d = check_answer(ask_ai(describe_event(ev, now, factor), provider, key, model))
            else:
                d = rules_decision(ev, factor)
        except Exception as e:
            summary["errors"].append(f"{ev['id']}: {e}")
            print(f"   AI problem for [{ev['id']}]: {e}. Using rules mode for this one.")
            d, used = rules_decision(ev, factor), "rules (AI failed)"
        d.update({"time": now.isoformat(), "mode": used,
                  "model": f"{PROVIDERS[provider]['name'].split(' (')[0]} {model}" if used == "AI" else None})
        if d.get("expected_direction") in ("up", "down") and used == "AI" and \
                (ev.get("prediction") or {}).get("direction") == "unclear":
            ev["prediction"] = {"direction": d["expected_direction"], "by": "AI",
                                "reason": d.get("direction_reason") or "AI prediction"}
        if d["send_alert"]:
            ev["state"] = "alerted"
            ev["state_reason"] = "stamped by the Decision agent - alert ready"
            d["telegram_message"] = telegram_text(ev, d)
            d["telegram_status"] = "waiting to be sent"
            summary["alerts"] += 1
            print(f"   ALERT     [{ev['id']}] {d['alert_title']}\n             {d['alert_message']}")
        else:
            ev["state"] = "rejected"
            ev["state_reason"] = f"Decision agent: {d['reason']}"
            summary["rejected"] += 1
            print(f"   NO ALERT  [{ev['id']}] {ev['title']}\n             {d['reason']}")
        ev["decision"] = d
        ev["history"].append({"time": now.isoformat(), "note": f"confirmed -> {ev['state']}: {d['reason']}"})
        summary["decided"] += 1

    memory["last_states"] = asset_states(memory, [], now)
    save_memory(memory, test_mode)
    print(f"\nBOTTOM LINE: {summary['decided']} decided, {summary['alerts']} alert(s) stamped, "
          f"{summary['rejected']} rejected, {len(summary['errors'])} AI problem(s).")
    return summary


if __name__ == "__main__":
    run(test_mode="--test" in sys.argv)
