"""Données pour le point du matin : prix récents (avant-ouverture inclus) de quelques symboles Yahoo."""
import sys
from datetime import datetime, timezone

import requests

H = {"User-Agent": "Mozilla/5.0"}


def quote(sym):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "5d", "interval": "5m", "includePrePost": "true"}, headers=H, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    m, q = res["meta"], res["indicators"]["quote"][0]
    pts = [(t, c) for t, c in zip(res.get("timestamp") or [], q["close"]) if c is not None]
    last_t, last = pts[-1] if pts else (None, None)
    when = datetime.fromtimestamp(last_t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if last_t else "?"
    d = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "5d", "interval": "1d"}, headers=H, timeout=20).json()["chart"]["result"][0]
    dq = d["indicators"]["quote"][0]
    days = [(datetime.fromtimestamp(t, timezone.utc).date(), o, h, l, c) for t, o, h, l, c in
            zip(d["timestamp"], dq["open"], dq["high"], dq["low"], dq["close"]) if c is not None]
    prev = days[-2] if len(days) > 1 else None
    return (f"{sym} ({m.get('currency')}): dernier {last:.4f} à {when} | séance régulière {m.get('regularMarketPrice')} "
            f"| clôture veille {m.get('chartPreviousClose')} | jours: "
            + "; ".join(f"{dd} O{o:.2f} H{h:.2f} B{l:.2f} C{c:.2f}" for dd, o, h, l, c in days[-3:]))


for s in sys.argv[1:]:
    try:
        print(quote(s))
    except Exception as e:
        print(f"{s}: indisponible ({e})")
