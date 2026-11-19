"""Suivi du premier jour de cotation d'ANT (Nasdaq) : faits seulement, aucun conseil.

Alertes ntfy : première cotation (écart avec le prix d'introduction), chaque mouvement
de 5 points depuis la dernière alerte, et un résumé à la clôture.
Le résumé est aussi écrit dans state/ant_day.json pour le bilan de fin de séance.

    python ant_watch.py loop     # jusqu'à 16 h 05 (heure de Toronto)
"""
import json
import os
import sys
import time as _t
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests

TICKER = "ANT"
IPO_PRICE = 83.0  # prix d'introduction annoncé par la presse le 18 nov.
STEP_PCT = 5.0
TZ = ZoneInfo("America/Toronto")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "ant_day.json")
NOTE = "Faits seulement, pas un conseil. Je ne commente pas ce titre : Anthropic m'a créé."


def quote():
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}",
                     params={"range": "1d", "interval": "1m"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    res = (r.json().get("chart", {}).get("result") or [None])[0]
    if not res:
        return None
    q = res["indicators"]["quote"][0]
    rows = [(ts, o, h, l, c, v) for ts, o, h, l, c, v in
            zip(res.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"], q["volume"])
            if c is not None]
    if not rows:
        return None
    day = datetime.fromtimestamp(rows[-1][0], TZ).date()
    if day != datetime.now(TZ).date():
        return None
    return {"first_t": datetime.fromtimestamp(rows[0][0], TZ), "open": rows[0][1] or rows[0][4],
            "high": max(r[2] for r in rows if r[2] is not None),
            "low": min(r[3] for r in rows if r[3] is not None),
            "last": rows[-1][4], "last_t": datetime.fromtimestamp(rows[-1][0], TZ),
            "volume": sum(r[5] or 0 for r in rows)}


def pct(x):
    return (x / IPO_PRICE - 1) * 100


def notify(title, lines):
    print(f"\n[{title}]\n" + "\n".join(lines), flush=True)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        requests.post("https://ntfy.sh/", json={"topic": topic, "title": title,
                                               "message": "\n".join(lines), "priority": 4,
                                               "tags": ["bar_chart"]}, timeout=20)


def loop():
    opened = False
    last_alert = None
    q = None
    while datetime.now(TZ).time() < time(16, 5):
        try:
            q = quote() or q
        except Exception as ex:
            print(f"{datetime.now(TZ):%H:%M} erreur : {ex}", flush=True)
        if q and not opened:
            opened = True
            last_alert = q["last"]
            notify(f"ANT cote : {q['open']:.2f} $ à l'ouverture",
                   [f"Première cotation à {q['first_t']:%H:%M}, {q['open']:.2f} $, soit {pct(q['open']):+.1f} % "
                    f"par rapport au prix d'introduction de {IPO_PRICE:.0f} $.",
                    f"Dernier : {q['last']:.2f} $ ({pct(q['last']):+.1f} %).", NOTE])
        elif q and abs(q["last"] / last_alert - 1) * 100 >= STEP_PCT:
            move = (q["last"] / last_alert - 1) * 100
            last_alert = q["last"]
            notify(f"ANT {move:+.0f} % : {q['last']:.2f} $",
                   [f"{q['last_t']:%H:%M} : {q['last']:.2f} $, {pct(q['last']):+.1f} % vs {IPO_PRICE:.0f} $. "
                    f"Haut {q['high']:.2f} $, bas {q['low']:.2f} $.", NOTE])
        if datetime.now(TZ).minute % 15 == 0 and q:
            print(f"{datetime.now(TZ):%H:%M} dernier {q['last']:.2f}", flush=True)
        if datetime.now(TZ).time() >= time(16, 1) and q:
            break
        _t.sleep(60)
    if q:
        summary = {"date": str(datetime.now(TZ).date()), "ipo_price": IPO_PRICE, "open": q["open"],
                   "first_trade": f"{q['first_t']:%H:%M}", "high": q["high"], "low": q["low"],
                   "close": q["last"], "close_vs_ipo_pct": round(pct(q["last"]), 2), "volume": q["volume"]}
        json.dump(summary, open(OUT, "w"), indent=1)
        notify(f"ANT clôture : {q['last']:.2f} $ ({pct(q['last']):+.1f} %)",
               [f"Ouverture {q['open']:.2f} $ à {q['first_t']:%H:%M}, haut {q['high']:.2f} $, bas {q['low']:.2f} $.",
                f"Prix d'introduction : {IPO_PRICE:.0f} $.", NOTE])
    else:
        print("ANT n'a pas coté aujourd'hui (aucune donnée Yahoo).")


if __name__ == "__main__":
    loop() if (sys.argv[1:] or ["loop"])[0] == "loop" else print(quote())
