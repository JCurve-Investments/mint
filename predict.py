"""
MINT predictions
================
Every event gets a PREDICTION: which way the asset's price is expected to move, and why.
It is based on what each factor has done in the past (your factor research), applied to the headline:

  B1 bans, crackdowns, lawsuits   -> Bitcoin DOWN   (but "lawsuit dropped", "approved" -> UP)
  B2 exchange hacks, collapses     -> Bitcoin DOWN
  B4 ETF flows                     -> inflows UP, outflows DOWN
  G1 US dollar                     -> dollar up = gold DOWN, dollar down = gold UP
  G2 interest rates, the Fed       -> higher / no cut = gold DOWN, rate cut = gold UP
  G3 surprise US data              -> hot data (strong jobs, high inflation) = gold DOWN, weak data = gold UP
  G4 central bank buying           -> buying UP, selling / slowing DOWN
  G5 ETF and investor flows        -> inflows / more demand UP, outflows / less demand DOWN

These are only the FACTOR PATTERNS. The final prediction (library.py) also looks at what the price actually did
after similar past events, and combines both. MINT never reads the price direction from the headline itself.

Later, the Price Tracker checks the price after 1 hour, 1 day and 7 days:
  RIGHT          the price clearly moved the predicted way
  WRONG          the price clearly moved the other way
  NO CLEAR MOVE  it did not move much either way
"""

import re

UP_WORDS = r"(rise|rises|rising|rose|jump|jumps|jumped|climb|climbs|gain|gains|surge|surges|soar|soars|strengthen\w*|" \
           r"rall\w*|firm\w*|higher|high|hits? (a )?(\d+-\w+ )?high|up)"
DOWN_WORDS = r"(fall|falls|falling|fell|slide|slides|slid|slip|slips|drop|drops|dropped|weaken\w*|tumble\w*|" \
             r"sink|sinks|ease|eases|eased|lower|low|slump\w*|declin\w*|down|retreat\w*)"


def has(pattern, text):
    return re.search(pattern, text) is not None


def direction_after(subject, text, window=4):
    """Look at the few words right after a subject (e.g. 'dollar') for an up or down word."""
    m = re.search(r"\b(?:" + subject + r")\b((?:\W+\w+){0," + str(window) + r"})", text)
    if not m:
        return None
    after = m.groups()[-1] or ""
    if re.search(r"\b" + UP_WORDS + r"\b", after):
        return "up"
    if re.search(r"\b" + DOWN_WORDS + r"\b", after):
        return "down"
    return None


def predict(factor_id, title, asset):
    """Returns {"direction": "up" / "down" / "unclear", "reason": "..."}"""
    t = " " + title.lower() + " "
    say = lambda d, r: {"direction": d, "reason": r}

    if factor_id == "B1":
        if has(r"\b(guilty|charged|sues|sued|bans?|banned|crackdown|arrest\w*|fraud)\b", t) and \
                not has(r"\b(drops?|dropped|dismiss\w*|wins?|won|overturn\w*)\b", t):
            return say("down", "Bans, crackdowns and lawsuits: Bitcoin has usually fallen afterwards")
        if has(r"\b(drops?|dropped|dismiss\w*|ends?|ended|settle\w*|approv\w*|wins?|won|overturn\w*|lift\w*|"
               r"reserve|legal tender|legaliz\w*|adopt\w*|embrac\w*|not securities)\b", t):
            return say("up", "Good news for crypto (a case dropped, settled or an approval): Bitcoin has tended to rise")
        return say("down", "Bans, crackdowns and lawsuits: Bitcoin has usually fallen afterwards")
    if factor_id == "B3":
        if has(r"\b(crash\w*|sell-off|selloff|plung\w*|tumbl\w*|panic\w*|rout|slump\w*)\b", t):
            return say("down", "A sharp fall in the stock market: Bitcoin has often fallen with it")
        if has(r"\b(rall\w*|surg\w*|record high|soar\w*)\b", t):
            return say("up", "A strong stock market: Bitcoin has often risen with it")
        return say("unclear", "The headline does not say which way the stock market moved")
    if factor_id == "B2":
        return say("down", "Exchange hacks and collapses: Bitcoin has usually fallen afterwards")
    if factor_id in ("B4", "G5"):
        a = "Bitcoin" if factor_id == "B4" else "Gold"
        if has(r"\b(outflow\w*|withdraw\w*|less demand|demand (falls|slows|drops)|slows|sell\w*)\b", t):
            return say("down", f"Money flowing OUT of {a.lower()} funds: the price has tended to fall")
        if has(r"\b(inflow\w*|demand (rises|jumps|grows)|record demand|buy\w*|more demand)\b", t):
            return say("up", f"Money flowing INTO {a.lower()} funds: the price has tended to rise")
        return say("unclear", "The headline does not say if money is flowing in or out")
    if factor_id == "G1":
        d = direction_after(r"(dollar|greenback|dxy)", t)
        if d == "up":
            return say("down", "The dollar is rising: gold has usually fallen when the dollar gets stronger")
        if d == "down":
            return say("up", "The dollar is falling: gold has usually risen when the dollar gets weaker")
        return say("unclear", "The headline does not say which way the dollar moved")
    if factor_id == "G2":
        if has(r"\b(pause\w* (rate )?hikes?|stop\w* (rate )?hik\w*|end\w* (rate )?hik\w*|halt\w* (rate )?hik\w*)\b", t):
            return say("up", "The Fed may stop raising rates: gold has usually risen")
        if has(r"\b(no (rate )?cuts?|not cut|fewer cuts|rule\w* out (a )?cut|hike\w*|rais\w* rates|higher for longer|"
               r"higher-for-longer|tighten\w*|hawkish|rates? stay high|keeps? rates high)\b", t):
            return say("down", "Interest rates staying high or going up: gold has usually fallen (it pays no interest)")
        if has(r"\b(cuts? (interest )?rates?|rate cuts?|cut rates|lower rates|eas\w*|dovish|pause\w*)\b", t):
            return say("up", "Interest rates coming down: gold has usually risen")
        return say("unclear", "The headline does not say if rates are going up or down")
    if factor_id == "G3":
        if has(r"\b(weak\w*|cool\w*|miss\w*|slow\w*|unemployment (rises|jumps|climbs)|falls|lower than)\b", t):
            return say("up", "Weaker US data or lower inflation: rate cuts become more likely, so gold has tended to rise")
        if has(r"\b(strong|hot|hotter|higher|beats?|surg\w*|jump\w*|rises|accelerat\w*)\b", t):
            return say("down", "Stronger US data or higher inflation: the Fed may keep rates high, so gold has tended to fall")
        return say("unclear", "The headline does not say if the data was strong or weak")
    if factor_id == "G4":
        if has(r"\b(sell\w*|sold|slow\w*|cut\w*|reduc\w*|less)\b", t):
            return say("down", "Central banks buying less or selling: gold has tended to fall")
        return say("up", "Central banks buying gold: it has tended to support the price")
    return say("unclear", "No past pattern for this kind of event")

