"""Premières séances des grosses IPO tech, pour comparer avec celle d'Anthropic (ANT).

Faits historiques uniquement : prix d'IPO, fourchettes, montants levés et valorisations
viennent des prospectus et de la presse de l'époque (arrondis). Les cours viennent de Yahoo.
Claude est fait par Anthropic : cette page ne donne aucun avis d'achat ou de vente sur ANT.

Usage : python ipo_study.py   -> simulation/ipos.json
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
SESSIONS = 10

# ticker, nom, date de 1re séance, prix IPO, fourchette initiale, fourchette relevée/abaissée,
# montant levé (G$ US), valorisation à l'IPO (G$ US), ouverture réelle J1, facteur de division
# d'actions à appliquer aux cours Yahoo (ajustés), note.
IPOS = [
    ("GOOGL", "Google", "2004-08-19", 85, (108, 135), (85, 95), 1.67, 23, 100.01, 40, "Fourchette abaissée, enchère hollandaise"),
    ("TSLA", "Tesla", "2010-06-29", 17, (14, 16), None, 0.226, 1.7, 19.00, 15, ""),
    ("META", "Facebook", "2012-05-18", 38, (28, 35), (34, 38), 16.0, 104, 42.05, 1, "Pannes Nasdaq le 1er jour"),
    ("BABA", "Alibaba", "2014-09-19", 68, (60, 66), (66, 68), 21.8, 168, 92.70, 1, "Plus grosse IPO de l'époque"),
    ("SNAP", "Snap", "2017-03-02", 17, (14, 16), None, 3.4, 24, 24.00, 1, "Actions sans droit de vote"),
    ("UBER", "Uber", "2019-05-10", 45, (44, 50), None, 8.1, 82, 42.00, 1, "Prix fixé en bas de fourchette"),
    ("SNOW", "Snowflake", "2020-09-16", 120, (75, 85), (100, 110), 3.4, 33, 245.00, 1, ""),
    ("ABNB", "Airbnb", "2020-12-10", 68, (44, 50), (56, 60), 3.5, 47, 146.00, 1, ""),
    ("RIVN", "Rivian", "2021-11-10", 78, (57, 62), (72, 74), 11.9, 66.5, 106.75, 1, ""),
    ("ARM", "Arm", "2023-09-14", 51, (47, 51), None, 4.87, 54.5, 56.10, 1, "Prix fixé en haut de fourchette"),
    ("RDDT", "Reddit", "2024-03-21", 34, (31, 34), None, 0.748, 6.4, 47.00, 1, ""),
    ("CRWV", "CoreWeave", "2025-03-28", 40, (47, 55), None, 1.5, 23, 39.00, 1, "Prix et taille réduits"),
    ("CRCL", "Circle", "2025-06-05", 31, (24, 26), (27, 28), 1.05, 6.9, 69.00, 1, ""),
    ("FIG", "Figma", "2025-07-31", 33, (25, 28), (30, 32), 1.2, 19.3, 85.00, 1, ""),
    ("SPCX", "SpaceX", "2026-06-12", 135, None, None, 75.0, 1770, None, 1, "Prix annoncé dans le dépôt, sans fourchette"),
]

ANT = {
    "ticker": "ANT", "name": "Anthropic", "date": "2026-11-19", "range": [70, 78],
    "raise_low": 57.75, "raise_high": 64.35, "valuation_high": 1000, "valuation_diluted": 1405,
    "shares_m": 825, "exchange": "Nasdaq",
    "note": "Prix fixé prévu le 18 nov., 1re séance le 19 nov. Carnet d'ordres déjà couvert selon CNBC.",
    "sources": [
        ["IBTimes", "https://www.ibtimes.com.au/anthropic-sets-ipo-price-range-78-1878308"],
        ["Decrypt", "https://decrypt.co/382939/anthropic-sets-ipo-price-range-at-70-to-78-valuing-company-at-up-to-1-4-trillion"],
    ],
}


def bars(ticker, day):
    t0 = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) - 86400 * 2
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"period1": t0, "period2": t0 + 86400 * 25, "interval": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        if d < day or None in (q["open"][i], q["close"][i]):
            continue
        out.append({"d": d, "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": q["close"][i], "v": q["volume"][i] or 0})
    return out[:SESSIONS]


def main():
    rows = []
    for tk, name, day, ipo, rng, rng2, raised, val, open_real, split, note in IPOS:
        b = bars(tk, day)
        if not b or b[0]["d"] != day:
            raise SystemExit(f"{tk} : 1re séance introuvable ({b[0]['d'] if b else 'vide'})")
        # Cours Yahoo ajustés des divisions d'actions : on les remet en prix de l'époque.
        for x in b:
            for k in "ohlc":
                x[k] = round(x[k] * split, 2)
            x["v"] = int(x["v"] / split)
        if open_real and abs(b[0]["o"] / open_real - 1) > 0.02:
            raise SystemExit(f"{tk} : ouverture Yahoo {b[0]['o']} ≠ {open_real}")
        o1 = b[0]["o"]
        mid = (rng[0] + rng[1]) / 2 if rng else None
        rows.append({
            "ticker": tk, "name": name, "date": day, "ipo_price": ipo,
            "range": list(rng) if rng else None, "range_revised": list(rng2) if rng2 else None,
            "vs_range_pct": round((ipo / mid - 1) * 100, 1) if mid else None,
            "raised_b": raised, "valuation_b": val, "note": note, "bars": b,
            "pop_pct": round((o1 / ipo - 1) * 100, 1),
            "j1_open_close_pct": round((b[0]["c"] / o1 - 1) * 100, 1),
            "j1_ipo_close_pct": round((b[0]["c"] / ipo - 1) * 100, 1),
            "j5_from_open_pct": round((b[min(4, len(b) - 1)]["c"] / o1 - 1) * 100, 1),
            "j10_from_open_pct": round((b[-1]["c"] / o1 - 1) * 100, 1),
            "j5_from_ipo_pct": round((b[min(4, len(b) - 1)]["c"] / ipo - 1) * 100, 1),
        })
        r = rows[-1]
        print(f"{name:<10} IPO {ipo:>6}  ouv {o1:>7}  pop {r['pop_pct']:+6.1f}%  ouv→clô J1 {r['j1_open_close_pct']:+6.1f}%  "
              f"ouv→J5 {r['j5_from_open_pct']:+6.1f}%  ouv→J10 {r['j10_from_open_pct']:+6.1f}%")
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sessions": SESSIONS,
           "ipos": rows, "ant": ANT}
    (ROOT / "simulation").mkdir(exist_ok=True)
    (ROOT / "simulation" / "ipos.json").write_text(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
