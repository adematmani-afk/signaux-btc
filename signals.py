"""Signaux intraday sur un FNB Bitcoin coté au TSX, envoyés sur le téléphone via ntfy.

Aucun ordre n'est passé : le script propose, l'utilisateur exécute dans Wealthsimple.

    python signals.py live               # un passage (lancé toutes les 5 min par GitHub Actions)
    python signals.py backtest [TICKER]  # rejoue les ~60 derniers jours de bougies 5 min
"""
import json
import os
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
TZ = ZoneInfo(CFG["timezone"])
STATE_PATH = os.path.join(ROOT, "state", "state.json")
OPEN, CLOSE = time(9, 30), time(16, 0)


def hhmm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


# ---------------------------------------------------------------- données

def fetch_bars(ticker, range_="1d", interval="5m"):
    """Bougies Yahoo Finance, en heure de Toronto, séance régulière uniquement."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = requests.get(url, params={"range": range_, "interval": interval},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        t = datetime.fromtimestamp(ts, TZ)
        if OPEN <= t.time() < CLOSE:
            bars.append({"t": t, "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                         "c": q["close"][i], "v": q["volume"][i] or 0})
    return bars


def by_day(bars):
    days = {}
    for b in bars:
        days.setdefault(b["t"].date(), []).append(b)
    return days


# ---------------------------------------------------------------- stratégie

def context(day_bars, i):
    """VWAP, range d'ouverture et volume moyen calculés avec les bougies 0..i."""
    upto = day_bars[: i + 1]
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in upto)
    vol = sum(b["v"] for b in upto)
    vwap = pv / vol if vol else upto[-1]["c"]
    or_end = (datetime.combine(upto[0]["t"].date(), OPEN) + timedelta(minutes=CFG["opening_range_minutes"])).time()
    orb = [b for b in upto if b["t"].time() < or_end]
    prev_vols = [b["v"] for b in upto[:-1]]
    return {
        "vwap": vwap,
        "day_open": upto[0]["o"],
        "or_ready": upto[-1]["t"].time() >= or_end,
        "or_high": max(b["h"] for b in orb) if orb else None,
        "or_low": min(b["l"] for b in orb) if orb else None,
        "avg_vol": sum(prev_vols) / len(prev_vols) if prev_vols else 0,
    }


def new_day_state():
    return {"position": None, "entries": 0, "orb_used": False, "prev_close_below_vwap": False}


def step(bar, ctx, st, equity, paused):
    """Fait avancer la stratégie d'une bougie. Retourne la liste des événements."""
    events = []
    t = bar["t"].time()
    pos = st["position"]

    if pos:
        # Prudence : si stop et objectif sont touchés dans la même bougie, on compte le stop.
        if bar["l"] <= pos["stop"]:
            events.append(exit_event(bar, pos, pos["stop"], "stop", equity))
            st["position"] = None
        elif not pos["t1_done"] and bar["h"] >= pos["t1"]:
            pos["t1_done"] = True
            if CFG["breakeven_after_t1"]:
                pos["stop"] = pos["entry"]
            events.append({"type": "t1", "t": bar["t"], "price": pos["t1"], "pos": dict(pos)})
            if bar["h"] >= pos["t2"]:
                events.append(exit_event(bar, pos, pos["t2"], "t2", equity))
                st["position"] = None
        elif pos["t1_done"] and bar["h"] >= pos["t2"]:
            events.append(exit_event(bar, pos, pos["t2"], "t2", equity))
            st["position"] = None
        elif t >= hhmm(CFG["force_exit_at"]):
            events.append(exit_event(bar, pos, bar["c"], "fin_de_seance", equity))
            st["position"] = None

    below = bar["c"] < ctx["vwap"]
    can_enter = (st["position"] is None and not events and not paused and ctx["or_ready"]
                 and st["entries"] < CFG["max_entries_per_day"] and t < hhmm(CFG["no_entry_after"]))
    if can_enter:
        setup, target = None, None
        vol_ok = bar["v"] >= CFG["volume_factor"] * ctx["avg_vol"]
        strategy = CFG.get("strategy", "momentum")
        if strategy == "momentum":
            if not st["orb_used"] and bar["c"] > ctx["or_high"] and bar["c"] > ctx["vwap"] and vol_ok:
                setup = "Cassure du range d'ouverture"
                st["orb_used"] = True
            elif t >= hhmm(CFG["vwap_reclaim_after"]) and st["prev_close_below_vwap"] and not below and vol_ok:
                setup = "Reprise du VWAP"
        elif strategy == "reversion":
            # Achat d'un excès de baisse sous le VWAP, revente au retour vers le VWAP.
            if bar["c"] <= ctx["vwap"] * (1 - CFG["reversion_dev_pct"] / 100):
                setup, target = "Retour vers le VWAP", round(ctx["vwap"], 2)
        elif strategy == "trend":
            # Une seule entrée par jour, si la première heure est déjà nettement haussière.
            day_open = ctx["day_open"]
            if not st["orb_used"] and t >= hhmm(CFG["trend_check_at"]):
                st["orb_used"] = True
                if bar["c"] >= day_open * (1 + CFG["trend_min_pct"] / 100) and bar["c"] > ctx["vwap"]:
                    setup = "Journée haussière"
        if setup:
            entry = bar["c"]
            pos = {"setup": setup, "entry": entry, "t": bar["t"].isoformat(),
                   "stop": round(entry * (1 - CFG["stop_pct"] / 100), 2),
                   "t1": target or round(entry * (1 + CFG["target1_pct"] / 100), 2),
                   "t2": target or round(entry * (1 + CFG["target2_pct"] / 100), 2),
                   "t1_done": False, "vwap": round(ctx["vwap"], 2), "or_high": ctx["or_high"]}
            st["position"] = pos
            st["entries"] += 1
            events.append({"type": "entry", "t": bar["t"], "price": entry, "pos": dict(pos), "equity": equity})
    st["prev_close_below_vwap"] = below
    return events


def trade_return_pct(pos, exit_price):
    """Rendement net du trade : moitié vendue à T1 si atteint, coût aller-retour déduit."""
    leg = lambda p: (p / pos["entry"] - 1) * 100
    gross = (leg(pos["t1"]) + leg(exit_price)) / 2 if pos["t1_done"] else leg(exit_price)
    return gross - CFG["round_trip_cost_pct"]


def exit_event(bar, pos, price, reason, equity):
    r = trade_return_pct(pos, price)
    return {"type": "exit", "t": bar["t"], "price": price, "reason": reason, "pos": dict(pos),
            "ret_pct": r, "equity_after": equity * (1 + r / 100)}


# ---------------------------------------------------------------- messages

def fmt(e):
    """Notification courte : quoi faire, à quel prix, avec quel montant."""
    p = e["pos"]
    tk = CFG["ticker_label"].split()[0]
    if e["type"] == "entry":
        expiry = (e["t"] + timedelta(minutes=5 + CFG["signal_valid_minutes"])).strftime("%H:%M")
        return (f"ACHÈTE {tk} maintenant", 5, [
            f"Montant : {e['equity']:.0f} $ = {int(e['equity'] // (p['entry'] * 1.003))} parts",
            f"Ordre à cours limité : {p['entry'] * 1.003:.2f} $ (prix actuel {e['price']:.2f} $)",
            f"Stop : {p['stop']:.2f} $",
            f"Objectif : {p['t1']:.2f} $" if p["t1"] == p["t2"] else f"Objectif 1 : {p['t1']:.2f} $  |  Objectif 2 : {p['t2']:.2f} $",
            f"Valable jusqu'à {expiry}",
        ])
    if e["type"] == "t1":
        return (f"VENDS LA MOITIÉ {tk} maintenant", 5, [
            f"Prix : {e['price']:.2f} $",
            f"Nouveau stop pour le reste : {p['stop']:.2f} $",
        ])
    reason = {"stop": "stop touché", "t2": "objectif 2 atteint", "fin_de_seance": "fin de séance"}[e["reason"]]
    return (f"VENDS TOUT {tk} maintenant", 5, [
        f"Prix : {e['price']:.2f} $ ({reason})",
        f"Résultat : {e['ret_pct']:+.2f} %, compte ~{e['equity_after']:.2f} $",
    ])


def notify(title, priority, lines):
    if CFG.get("simulation"):
        title = "SIMULATION : " + title
        lines = lines + ["Simulation : ne place pas d'argent."]
    print(f"\n[{title}]\n" + "\n".join(lines))
    if os.environ.get("NO_NOTIFY"):
        return
    topic = os.environ.get("NTFY_TOPIC") or CFG["ntfy_topic"]
    requests.post("https://ntfy.sh/", json={"topic": topic, "title": title,
                                           "message": "\n".join(lines), "priority": priority,
                                           "tags": ["chart_with_upwards_trend"]}, timeout=20)


# ---------------------------------------------------------------- live

def load_state():
    try:
        return json.load(open(STATE_PATH))
    except FileNotFoundError:
        return {"equity": CFG["starting_equity"], "day": None, "day_state": None,
                "last_bar": None, "journal": [], "summary_sent": None, "pause_sent": False}


def save_state(s):
    json.dump(s, open(STATE_PATH, "w"), indent=1, ensure_ascii=False, default=str)


def run_live():
    now = datetime.now(TZ)
    s = load_state()
    if not CFG.get("alerts_enabled") or now.weekday() >= 5 or not time(9, 30) <= now.time() <= time(16, 20):
        return
    today = now.date().isoformat()
    if s["day"] != today:
        s.update(day=today, day_state=new_day_state(), last_bar=None)

    bars = [b for b in fetch_bars(CFG["ticker"]) if b["t"].date() == now.date()
            and b["t"] + timedelta(minutes=5) <= now]
    paused = s["equity"] <= CFG["pause_below_equity"]
    if paused and not s["pause_sent"]:
        notify("PAUSE : plus de signaux d'achat", 4, [f"Compte estimé sous {CFG['pause_below_equity']} $ : {s['equity']:.2f} $"])
        s["pause_sent"] = True

    for i, bar in enumerate(bars):
        if s["last_bar"] and bar["t"].isoformat() <= s["last_bar"]:
            continue
        # On rejoue toujours la journée pour garder l'état juste, mais on ne notifie que les bougies récentes.
        fresh = now - bar["t"] <= timedelta(minutes=20)
        for e in step(bar, context(bars, i), s["day_state"], s["equity"], paused):
            if fresh:
                notify(*fmt(e))
            if e["type"] == "exit":
                s["equity"] = round(e["equity_after"], 4)
                s["journal"].append({"date": today, "setup": e["pos"]["setup"], "entry": e["pos"]["entry"],
                                     "exit": e["price"], "reason": e["reason"], "ret_pct": round(e["ret_pct"], 3)})
        s["last_bar"] = bar["t"].isoformat()

    save_state(s)


# ---------------------------------------------------------------- backtest

def simulate(days):
    equity, peak, max_dd, trades = CFG["starting_equity"], CFG["starting_equity"], 0.0, []
    for d in sorted(days):
        bars, st = days[d], new_day_state()
        for i, bar in enumerate(bars):
            for e in step(bar, context(bars, i), st, equity, equity <= CFG["pause_below_equity"]):
                if e["type"] == "exit":
                    equity = e["equity_after"]
                    peak = max(peak, equity)
                    max_dd = max(max_dd, (peak - equity) / peak * 100)
                    trades.append({"date": str(d), "setup": e["pos"]["setup"], "reason": e["reason"],
                                   "ret_pct": e["ret_pct"]})
    return equity, max_dd, trades


def stats(label, equity, max_dd, trades, n_days):
    wins = [t["ret_pct"] for t in trades if t["ret_pct"] > 0]
    losses = [t["ret_pct"] for t in trades if t["ret_pct"] <= 0]
    n = len(trades)
    lines = [f"### {label}",
             f"- Jours de Bourse : {n_days}, trades : {n}",
             f"- Compte : {CFG['starting_equity']} $ -> {equity:.2f} $ ({(equity / CFG['starting_equity'] - 1) * 100:+.1f} %)",
             f"- Baisse max depuis un sommet : {max_dd:.1f} %"]
    if n:
        lines += [f"- Taux de réussite : {len(wins) / n * 100:.0f} %",
                  f"- Gain moyen : {sum(wins) / len(wins) if wins else 0:+.2f} %  |  Perte moyenne : {sum(losses) / len(losses) if losses else 0:+.2f} %",
                  f"- Espérance par trade (net de frais) : {sum(t['ret_pct'] for t in trades) / n:+.2f} %"]
        for setup in sorted({t["setup"] for t in trades}):
            ts = [t["ret_pct"] for t in trades if t["setup"] == setup]
            lines.append(f"- {setup} : {len(ts)} trades, moyenne {sum(ts) / len(ts):+.2f} %")
        reasons = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        lines.append("- Sorties : " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    return "\n".join(lines)


def run_backtest(tickers):
    out = [f"## Backtest (bougies 5 min, ~60 jours, coût aller-retour {CFG['round_trip_cost_pct']} %)"]
    for tk in tickers:
        try:
            days = by_day(fetch_bars(tk, range_="60d"))
        except Exception as ex:
            out.append(f"### {tk}\n- Données indisponibles : {ex}")
            continue
        keys = sorted(days)
        half = len(keys) // 2
        bars = [b for d in keys for b in days[d]]
        vol = sum(b["c"] * b["v"] for b in bars) / max(len(keys), 1)
        out.append(f"\n**{tk}** : {keys[0]} -> {keys[-1]}, valeur échangée moyenne ~{vol:,.0f} $ par jour")
        out.append(stats("Période complète", *simulate(days), len(keys)))
        out.append(stats("1re moitié", *simulate({d: days[d] for d in keys[:half]}), half))
        out.append(stats("2e moitié", *simulate({d: days[d] for d in keys[half:]}), len(keys) - half))
    report = "\n\n".join(out)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(report + "\n")


def run_sweep(tickers):
    """Teste quelques réglages : choisis sur la 1re moitié, jugés sur la 2e (hors échantillon)."""
    out = ["## Comparaison de réglages (choix sur la 1re moitié, vérification sur la 2e)"]
    # stop à 99 % = pas de stop, seulement la vente de fin de séance
    grid = [(sl, t1, t2) for sl in (1.25, 2.0, 3.0, 99.0) for t1 in (1.0, 2.0, 3.0) for t2 in (2.0, 3.0, 5.0) if t2 > t1]
    base = dict(CFG)
    for tk in tickers:
        days = by_day(fetch_bars(tk, range_="60d"))
        keys = sorted(days)
        first, second = {d: days[d] for d in keys[: len(keys) // 2]}, {d: days[d] for d in keys[len(keys) // 2:]}
        rows = []
        for sl, t1, t2 in grid:
            CFG.update(stop_pct=sl, target1_pct=t1, target2_pct=t2)
            rows.append((simulate(first)[0], simulate(second)[0], simulate(days)[0], sl, t1, t2))
        CFG.clear(); CFG.update(base)
        rows.sort(reverse=True)
        out.append(f"\n**{tk}** (compte final sur 100 $ : 1re moitié / 2e moitié / total)")
        out += [f"- stop {sl} %, obj. {t1} % / {t2} % : {a:.2f} / {b:.2f} / {c:.2f}" for a, b, c, sl, t1, t2 in rows]
    report = "\n".join(out)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(report + "\n")


def run_alternatives(tickers):
    """Compare d'autres familles de stratégies, mêmes règles : choix sur la 1re moitié, vérification sur la 2e."""
    base = dict(CFG)
    variants = [("momentum", {})]
    variants += [("reversion", {"reversion_dev_pct": d, "stop_pct": sl}) for d in (0.5, 0.75, 1.0, 1.5) for sl in (1.0, 2.0, 99.0)]
    variants += [("trend", {"trend_check_at": c, "trend_min_pct": m, "stop_pct": sl, "target1_pct": 3.0, "target2_pct": 5.0})
                 for c in ("10:30", "11:30") for m in (0.0, 0.5, 1.0) for sl in (1.0, 2.0, 99.0)]
    out = ["## Autres approches (compte final sur 100 $ : 1re moitié / 2e moitié / total, nb de trades)"]
    for tk in tickers:
        days = by_day(fetch_bars(tk, range_="60d"))
        keys = sorted(days)
        first, second = {d: days[d] for d in keys[: len(keys) // 2]}, {d: days[d] for d in keys[len(keys) // 2:]}
        rows = []
        for name, params in variants:
            CFG.clear(); CFG.update(base); CFG.update(params, strategy=name, max_entries_per_day=99)
            a, b, full = simulate(first)[0], simulate(second)[0], simulate(days)
            rows.append((a, b, full[0], len(full[2]), name, params))
        CFG.clear(); CFG.update(base)
        rows.sort(key=lambda r: r[0], reverse=True)
        buy_hold = days[keys[-1]][-1]["c"] / days[keys[0]][0]["o"] * 100
        out.append(f"\n**{tk}** (achat-conservation sur la période : {buy_hold:.2f})")
        out += [f"- {n} {p} : {a:.2f} / {b:.2f} / {c:.2f} ({k} trades)" for a, b, c, k, n, p in rows[:12]]
    report = "\n".join(out)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(report + "\n")


def run_loop(max_minutes):
    """Vérifie chaque minute pendant la séance, jusqu'à 16 h 20 ou max_minutes."""
    import time as _time
    start = _time.monotonic()
    while _time.monotonic() - start < max_minutes * 60:
        now = datetime.now(TZ)
        if now.time() > time(16, 20):
            break
        try:
            run_live()
        except Exception as ex:  # une erreur réseau ne doit pas arrêter la séance
            print(f"{now:%H:%M} erreur : {ex}")
        _time.sleep(60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "live"
    if cmd == "backtest":
        run_backtest(sys.argv[2:] or [CFG["ticker"], "FBTC.TO"])
    elif cmd == "loop":
        run_loop(int(sys.argv[2]) if len(sys.argv) > 2 else 340)
    elif cmd == "alternatives":
        run_alternatives(sys.argv[2:] or [CFG["ticker"], "FBTC.TO"])
    elif cmd == "sweep":
        run_sweep(sys.argv[2:] or [CFG["ticker"], "FBTC.TO"])
    else:
        run_live()
