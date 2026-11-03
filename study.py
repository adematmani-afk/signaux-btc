"""Étude : existe-t-il un signal, tôt dans la séance, qui annonce qu'un titre va exploser plus tard dans la journée ?

Univers : titres volatils tirés des écrans Yahoo (hausses, baisses, plus actifs, petites capitalisations).
Données : ~60 jours de bougies 5 min. Pour chaque titre, chaque jour et chaque heure de décision, on mesure
ce qu'on sait à ce moment-là (écart d'ouverture, hausse depuis l'ouverture, volume relatif) puis ce qui
arrive ensuite (objectif +25 % touché ? résultat d'un trade avec stop -10 %, objectif +25 %, sinon clôture).

Les règles sont choisies sur la première moitié des jours et jugées sur la seconde, comparées au hasard.

    python study.py
"""
import statistics
import sys
from datetime import time

import requests

import signals as S

SCREENS = ["day_gainers", "small_cap_gainers", "most_actives", "aggressive_small_caps", "day_losers"]
CHECK_TIMES = [time(9, 45), time(10, 0), time(10, 30), time(11, 0)]
STOP, TARGET = 10.0, 25.0
COST = 0.2 + 3.0  # frais Wealthsimple + conversion USD aller-retour
SLIPPAGE = 0.5


def universe():
    syms = set()
    for scr in SCREENS:
        try:
            r = requests.get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                             params={"scrIds": scr, "count": 100}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            for q in r.json()["finance"]["result"][0]["quotes"]:
                if q.get("regularMarketPrice", 0) >= 1 and q.get("averageDailyVolume3Month", 0) >= 300_000:
                    syms.add(q["symbol"])
        except Exception as ex:
            print(f"écran {scr} indisponible ({ex})", file=sys.stderr)
    return sorted(syms)


def daily_volumes(sym):
    bars = S.fetch_bars(sym, "6mo", "1d")
    return {b["t"].date(): (b["c"], b["v"]) for b in bars}


def trade(after, entry):
    stop, target = entry * (1 - STOP / 100), entry * (1 + TARGET / 100)
    for b in after:
        if b["l"] <= stop:
            return -STOP - COST, False
        if b["h"] >= target:
            return TARGET - COST, True
    return (after[-1]["c"] / entry - 1) * 100 - COST, False


def samples(sym):
    out = []
    try:
        days = S.by_day(S.fetch_bars(sym, "60d", "5m"))
        daily = daily_volumes(sym)
    except Exception as ex:
        print(f"{sym} : données indisponibles ({ex})", file=sys.stderr)
        return out
    dates = sorted(daily)
    for d, bars in days.items():
        prev = [x for x in dates if x < d][-20:]
        if len(prev) < 10 or len(bars) < 70:
            continue
        prev_close = daily[prev[-1]][0]
        avg_vol = statistics.mean(daily[x][1] for x in prev)
        if not avg_vol or not prev_close:
            continue
        o = bars[0]["o"]
        for T in CHECK_TIMES:
            idx = [i for i, b in enumerate(bars) if b["t"].time() < T]
            if not idx:
                continue
            i = idx[-1]
            after = bars[i + 1:]
            if len(after) < 5:
                continue
            price = bars[i]["c"]
            frac = (i + 1) / 78
            cumvol = sum(b["v"] for b in bars[: i + 1])
            ret, hit = trade(after, price * (1 + SLIPPAGE / 100))
            out.append({"sym": sym, "date": d, "T": T, "gap": (o / prev_close - 1) * 100,
                        "move": (price / o - 1) * 100, "relvol": cumvol / (avg_vol * frac),
                        "above_vwap": price >= S.context(bars, i)["vwap"], "ret": ret, "hit": hit})
    return out


def summarize(rows):
    if not rows:
        return "0 trade"
    n = len(rows)
    hits = sum(r["hit"] for r in rows)
    avg = statistics.mean(r["ret"] for r in rows)
    return f"{n} trades, objectif +25 % touché {hits / n * 100:.1f} % du temps, résultat moyen {avg:+.2f} % par trade"


def rules():
    for T in CHECK_TIMES:
        for relvol in (2, 3, 5, 10, 20):
            for gmin, gmax in ((-100, 100), (0, 100), (5, 100), (10, 100), (-100, 0)):
                for mmin, mmax in ((-100, 100), (0, 100), (0, 5), (0, 15), (5, 15), (-100, 0)):
                    for vw in (None, True):
                        yield {"T": T, "relvol": relvol, "gap": (gmin, gmax), "move": (mmin, mmax), "vwap": vw}


def match(r, rule):
    return (r["T"] == rule["T"] and r["relvol"] >= rule["relvol"]
            and rule["gap"][0] <= r["gap"] < rule["gap"][1] and rule["move"][0] <= r["move"] < rule["move"][1]
            and (rule["vwap"] is None or r["above_vwap"] == rule["vwap"]))


def describe(rule):
    parts = [f"à {rule['T']:%H:%M}", f"volume ≥ {rule['relvol']}× la normale"]
    if rule["gap"] != (-100, 100):
        parts.append(f"écart d'ouverture {rule['gap'][0]} à {rule['gap'][1]} %")
    if rule["move"] != (-100, 100):
        parts.append(f"hausse depuis l'ouverture {rule['move'][0]} à {rule['move'][1]} %")
    if rule["vwap"]:
        parts.append("au-dessus du VWAP")
    return ", ".join(parts)


def main():
    syms = universe()
    print(f"## Étude des départs explosifs\n\nUnivers : {len(syms)} titres volatils (écrans Yahoo d'aujourd'hui).")
    rows = []
    for s in syms:
        rows += samples(s)
    dates = sorted({r["date"] for r in rows})
    if len(dates) < 20:
        print("Pas assez de données.")
        return
    cut = dates[len(dates) // 2]
    train = [r for r in rows if r["date"] < cut]
    test = [r for r in rows if r["date"] >= cut]
    print(f"Jours : {len(dates)} ({dates[0]} à {dates[-1]}), coupure au {cut}. Cas étudiés : {len(rows)}.")
    print(f"Trade : entrée +{SLIPPAGE} %, stop -{STOP:.0f} %, objectif +{TARGET:.0f} %, sinon clôture ; frais {COST} %.\n")

    print("### Hasard (acheter n'importe quel titre de l'univers à l'heure dite)")
    for T in CHECK_TIMES:
        print(f"- {T:%H:%M} : 1re moitié {summarize([r for r in train if r['T'] == T])} | "
              f"2e moitié {summarize([r for r in test if r['T'] == T])}")

    scored, seen = [], set()
    for rule in rules():
        tr = [r for r in train if match(r, rule)]
        key = frozenset(id(r) for r in tr)
        if len(tr) >= 15 and key not in seen:  # plusieurs règles peuvent choisir exactement les mêmes cas
            seen.add(key)
            scored.append((statistics.mean(r["ret"] for r in tr), rule, tr))
    scored.sort(key=lambda x: -x[0])
    print("\n### 10 meilleures règles sur la 1re moitié, jugées sur la 2e (jours jamais vus)")
    for _, rule, tr in scored[:10]:
        te = [r for r in test if match(r, rule)]
        print(f"- {describe(rule)}\n  - 1re moitié : {summarize(tr)}\n  - 2e moitié : {summarize(te)}")

    best = scored[0][1] if scored else None
    if best:
        te = [r for r in test if match(r, best)]
        print("\n### Exemples de la 2e moitié pour la meilleure règle")
        for r in sorted(te, key=lambda r: r["date"])[:15]:
            print(f"- {r['date']} {r['sym']} : écart {r['gap']:+.1f} %, hausse {r['move']:+.1f} %, "
                  f"volume x{r['relvol']:.1f} -> {r['ret']:+.1f} %{' (objectif)' if r['hit'] else ''}")


if __name__ == "__main__":
    main()
