"""Simulation de marché pour les tickers suivis par le bot.

Quatre modèles quantitatifs classiques (publics, rien de secret) :
  1. GBM          : mouvement brownien géométrique (dérive et volatilité constantes)
  2. GARCH(1,1)-t : la volatilité change dans le temps, queues épaisses (Student)
  3. Régimes      : modèle de Markov à 2 états (calme / agité) de Hamilton
  4. Bootstrap    : on rejoue des blocs de vrais rendements passés

Chaque modèle génère des milliers de trajectoires sur 20 séances. On en tire :
  - la densité de probabilité du prix pour chaque jour futur (surface 3D)
  - des estimations de tendance : P(hausse), médiane, intervalle 90 %
Puis un test hors échantillon (walk-forward) mesure si ces estimations
valent mieux qu'un pile ou face. Rien n'est envoyé au bot tant que ce
test n'est pas concluant.

Usage :
  python market_sim.py run        # simule et écrit simulation/latest.json
  python market_sim.py validate   # test hors échantillon, écrit simulation/validation.json
  python market_sim.py html       # génère simulation/atelier.html (visualisation 3D)
  python market_sim.py all        # tout
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests
from scipy.optimize import minimize
from scipy.special import gammaln

TZ = ZoneInfo("America/Toronto")
ROOT = Path(__file__).parent
OUT = ROOT / "simulation"
H = 20            # séances simulées
N_PATHS = 4000
GRID = 70         # nombre de cases de prix dans la surface
RNG = np.random.default_rng(42)

LABELS = {
    "BTCC-B.TO": "BTCC.B", "BTCX-B.TO": "BTCX.B", "FBTC.TO": "FBTC",
    "ENB.TO": "ENB", "TSLA.NE": "TSLA (CDR)", "MSFT.NE": "MSFT (CDR)", "AMZN.NE": "AMZN (CDR)",
}


# ---------------------------------------------------------------- données

def _chart(ticker, range_, interval):
    """Bougies Yahoo : liste de (heure Toronto, open, high, low, close, volume)."""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"range": range_, "interval": interval},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append((datetime.fromtimestamp(t, TZ), o, h, l, c, q["volume"][i] or 0))
    return out


def fetch_daily_ohlcv(ticker):
    """Bougies quotidiennes. Yahoo ne donne pas d'historique journalier pour les
    CDR (.NE) : on les reconstruit alors depuis les bougies d'une heure."""
    rows = _chart(ticker, "5y", "1d")
    if len(rows) >= 250:
        return [(t.date(), o, h, l, c, v) for t, o, h, l, c, v in rows]
    days = {}
    for t, o, h, l, c, v in _chart(ticker, "730d", "1h"):
        d = days.get(t.date())
        if d is None:
            days[t.date()] = [t.date(), o, h, l, c, v]
        else:
            d[2], d[3], d[4], d[5] = max(d[2], h), min(d[3], l), c, d[5] + v
    return [tuple(d) for _, d in sorted(days.items())]


def fetch_daily(ticker):
    rows = fetch_daily_ohlcv(ticker)
    return [r[0].isoformat() for r in rows], np.array([r[4] for r in rows], float)


# ---------------------------------------------------------------- liquidité

def corwin_schultz(h, l):
    """Écart achat-vente estimé à partir des plus hauts / plus bas de 2 jours
    (Corwin & Schultz 2012). Yahoo ne donne pas le carnet d'ordres, c'est donc une estimation."""
    h, l = np.asarray(h, float), np.asarray(l, float)
    b = np.log(h[1:] / l[1:]) ** 2 + np.log(h[:-1] / l[:-1]) ** 2
    g = np.log(np.maximum(h[1:], h[:-1]) / np.minimum(l[1:], l[:-1])) ** 2
    k = 3 - 2 * np.sqrt(2)
    a = (np.sqrt(2 * b) - np.sqrt(b)) / k - np.sqrt(g / k)
    s = 2 * (np.exp(a) - 1) / (1 + np.exp(a))
    return np.clip(s, 0, None)


def liquidity(ticker, daily):
    """Bougies + volume quotidiens (6 mois) et 5 minutes (5 dernières séances),
    plus quelques mesures simples de liquidité."""
    d = daily[-130:]
    dv = np.array([r[5] for r in d], float)
    dc = np.array([r[4] for r in d], float)
    dollar = dv * dc
    spread = corwin_schultz([r[2] for r in d], [r[3] for r in d])
    intra = [b for b in _chart(ticker, "5d", "5m") if (b[0].hour, b[0].minute) >= (9, 30) and b[0].hour < 16]
    days = sorted({b[0].date() for b in intra})
    today = [b for b in intra if b[0].date() == days[-1]] if days else []
    # VWAP par séance, pour superposer aux bougies 5 min
    vwap, cum_pv, cum_v, cur = [], 0.0, 0.0, None
    for t, o, h, l, c, v in intra:
        if t.date() != cur:
            cur, cum_pv, cum_v = t.date(), 0.0, 0.0
        cum_pv += (h + l + c) / 3 * v
        cum_v += v
        vwap.append(round(cum_pv / cum_v, 4) if cum_v else None)
    avg20 = float(dv[-21:-1].mean()) if len(dv) > 21 else float(dv.mean())
    # rythme normal : volume moyen à la même heure, sur les séances précédentes
    vol_today = float(sum(b[5] for b in today))
    n_today = len(today)
    prev = [[b for b in intra if b[0].date() == dd][:n_today] for dd in days[:-1]]
    pace = np.mean([sum(b[5] for b in p) for p in prev]) if prev and n_today else 0
    return {
        "daily": {"t": [r[0].isoformat() for r in d], "o": [round(r[1], 4) for r in d],
                  "h": [round(r[2], 4) for r in d], "l": [round(r[3], 4) for r in d],
                  "c": [round(r[4], 4) for r in d], "v": [int(r[5]) for r in d],
                  "spread_pct": [None] + (spread * 100).round(3).tolist()},
        "intraday": {"t": [b[0].strftime("%Y-%m-%d %H:%M") for b in intra],
                     "o": [round(b[1], 4) for b in intra], "h": [round(b[2], 4) for b in intra],
                     "l": [round(b[3], 4) for b in intra], "c": [round(b[4], 4) for b in intra],
                     "v": [int(b[5]) for b in intra], "vwap": vwap},
        "avg_volume_20d": round(avg20),
        "avg_dollar_volume_20d": round(float(dollar[-21:-1].mean() if len(dollar) > 21 else dollar.mean())),
        "spread_est_pct": round(float(np.mean(spread[-20:]) * 100), 3),
        "zero_volume_bars_pct": round(100 * sum(1 for b in intra if b[5] == 0) / max(1, len(intra)), 1),
        "today_volume": int(vol_today),
        "today_rel_volume": round(float(vol_today / pace), 2) if pace else None,
        "today_date": days[-1].isoformat() if days else None,
    }


# ---------------------------------------------------------------- modèles
# Chaque modèle : fit(r) -> params ; simulate(params, r, n, h) -> rendements log (n, h)

class GBM:
    name = "GBM"

    @staticmethod
    def fit(r):
        return {"mu": float(r.mean()), "sigma": float(r.std(ddof=1))}

    @staticmethod
    def simulate(p, r, n, h, rng):
        return rng.normal(p["mu"], p["sigma"], (n, h))


def _t_logpdf(z, nu):
    """Log-densité d'une Student standardisée (variance 1)."""
    return (gammaln((nu + 1) / 2) - gammaln(nu / 2) - 0.5 * np.log(np.pi * (nu - 2))
            - (nu + 1) / 2 * np.log1p(z * z / (nu - 2)))


class GARCH:
    name = "GARCH"

    @staticmethod
    def _filter(x, r):
        mu, omega, alpha, beta, nu = x
        e = r - mu
        s2 = np.empty(len(r) + 1)
        s2[0] = e.var()
        for t in range(len(r)):
            s2[t + 1] = omega + alpha * e[t] ** 2 + beta * s2[t]
        return e, s2

    @staticmethod
    def fit(r):
        v = r.var()

        def nll(x):
            mu, omega, alpha, beta, nu = x
            if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 0.999 or nu <= 2.1:
                return 1e10
            e, s2 = GARCH._filter(x, r)
            s2 = s2[:-1]
            return -np.sum(_t_logpdf(e / np.sqrt(s2), nu) - 0.5 * np.log(s2))

        x0 = [r.mean(), v * 0.05, 0.08, 0.88, 6.0]
        res = minimize(nll, x0, method="Nelder-Mead", options={"maxiter": 3000, "xatol": 1e-8, "fatol": 1e-6})
        mu, omega, alpha, beta, nu = res.x
        _, s2 = GARCH._filter(res.x, r)
        return {"mu": float(mu), "omega": float(omega), "alpha": float(alpha), "beta": float(beta),
                "nu": float(nu), "sigma_next": float(np.sqrt(s2[-1])),
                "sigma_long": float(np.sqrt(omega / max(1e-9, 1 - alpha - beta)))}

    @staticmethod
    def simulate(p, r, n, h, rng):
        nu = p["nu"]
        z = rng.standard_t(nu, (n, h)) / np.sqrt(nu / (nu - 2))
        out = np.empty((n, h))
        s2 = np.full(n, p["sigma_next"] ** 2)
        for k in range(h):
            e = np.sqrt(s2) * z[:, k]
            out[:, k] = p["mu"] + e
            s2 = p["omega"] + p["alpha"] * e ** 2 + p["beta"] * s2
        return out


class Regime:
    """Markov à 2 états (Hamilton 1989), estimé par maximum de vraisemblance."""
    name = "Régimes"

    @staticmethod
    def _unpack(x):
        mu1, mu2, ls1, ls2, a, b = x
        p11 = 1 / (1 + np.exp(-a))
        p22 = 1 / (1 + np.exp(-b))
        return mu1, mu2, np.exp(ls1), np.exp(ls2), p11, p22

    @staticmethod
    def _filter(x, r):
        mu1, mu2, s1, s2, p11, p22 = Regime._unpack(x)
        P = np.array([[p11, 1 - p11], [1 - p22, p22]])
        xi = np.array([(1 - p22) / (2 - p11 - p22), (1 - p11) / (2 - p11 - p22)])
        d1 = np.exp(-0.5 * ((r - mu1) / s1) ** 2) / s1
        d2 = np.exp(-0.5 * ((r - mu2) / s2) ** 2) / s2
        ll = 0.0
        for t in range(len(r)):
            pred = xi @ P
            f = pred * np.array([d1[t], d2[t]]) + 1e-300
            tot = f.sum()
            ll += np.log(tot)
            xi = f / tot
        return ll, xi

    @staticmethod
    def fit(r):
        sd = r.std()
        x0 = [r.mean(), r.mean(), np.log(sd * 0.7), np.log(sd * 1.6), 3.0, 2.5]
        res = minimize(lambda x: -Regime._filter(x, r)[0], x0, method="Nelder-Mead",
                       options={"maxiter": 2500, "xatol": 1e-6, "fatol": 1e-6})
        mu1, mu2, s1, s2, p11, p22 = Regime._unpack(res.x)
        _, xi = Regime._filter(res.x, r)
        if s1 > s2:  # état 0 = calme
            mu1, mu2, s1, s2, p11, p22, xi = mu2, mu1, s2, s1, p22, p11, xi[::-1]
        return {"mu": [float(mu1), float(mu2)], "sigma": [float(s1), float(s2)],
                "p_stay": [float(p11), float(p22)], "p_now": [float(xi[0]), float(xi[1])]}

    @staticmethod
    def simulate(p, r, n, h, rng):
        mu, sg, ps = np.array(p["mu"]), np.array(p["sigma"]), np.array(p["p_stay"])
        state = (rng.random(n) > p["p_now"][0]).astype(int)
        out = np.empty((n, h))
        for k in range(h):
            switch = rng.random(n) > ps[state]
            state = np.where(switch, 1 - state, state)
            out[:, k] = rng.normal(mu[state], sg[state])
        return out


class Bootstrap:
    """Bootstrap stationnaire par blocs (Politis-Romano) sur les 2 dernières années."""
    name = "Bootstrap"

    @staticmethod
    def fit(r):
        return {"window": int(min(len(r), 500)), "block": 5}

    @staticmethod
    def simulate(p, r, n, h, rng):
        hist = r[-p["window"]:]
        m = len(hist)
        idx = np.empty((n, h), int)
        idx[:, 0] = rng.integers(0, m, n)
        for k in range(1, h):
            new = rng.random(n) < 1 / p["block"]
            idx[:, k] = np.where(new, rng.integers(0, m, n), (idx[:, k - 1] + 1) % m)
        return hist[idx]


MODELS = [GBM, GARCH, Regime, Bootstrap]


# ---------------------------------------------------------------- résumé d'une simulation

def summarize(S0, logret):
    paths = S0 * np.exp(np.cumsum(logret, axis=1))
    final = {}
    for hz in (1, 5, 20):
        px = paths[:, hz - 1]
        final[str(hz)] = {
            "p_up": float((px > S0).mean()),
            "median": float(np.median(px)),
            "mean_ret_pct": float((px / S0 - 1).mean() * 100),
            "q05": float(np.quantile(px, 0.05)),
            "q95": float(np.quantile(px, 0.95)),
            "p_gain_3pct": float((px > S0 * 1.03).mean()),
            "p_loss_2pct": float((px < S0 * 0.98).mean()),
        }
    bands = {q: [float(S0)] + np.quantile(paths, q / 100, axis=0).round(4).tolist()
             for q in (5, 25, 50, 75, 95)}
    return paths, final, bands


def surface(S0, paths_by_model):
    allp = np.concatenate([p for p in paths_by_model.values()])
    lo, hi = np.quantile(allp, 0.005), np.quantile(allp, 0.995)
    lo, hi = min(lo, S0 * 0.97), max(hi, S0 * 1.03)
    edges = np.linspace(lo, hi, GRID + 1)
    mids = (edges[:-1] + edges[1:]) / 2
    out = {}
    for name, paths in paths_by_model.items():
        z = []
        for k in range(H + 1):
            if k == 0:
                col = np.zeros(GRID)
                col[np.clip(np.searchsorted(edges, S0) - 1, 0, GRID - 1)] = 1.0
            else:
                col, _ = np.histogram(paths[:, k - 1], bins=edges)
                col = col / max(1, col.sum())
            z.append(col)
        # lissage léger le long de l'axe des prix pour une surface lisible
        z = np.array(z)
        ker = np.array([1, 2, 3, 2, 1], float)
        ker /= ker.sum()
        z[1:] = np.apply_along_axis(lambda c: np.convolve(c, ker, "same"), 1, z[1:])
        z[0] = z[0] * (z[1].max() if H else 1)
        out[name] = (z.T * 100).round(3).tolist()  # % de probabilité par case, [prix][jour]
    return mids.round(4).tolist(), out


def run():
    cfg = json.loads((ROOT / "config.json").read_text())
    OUT.mkdir(exist_ok=True)
    result = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "horizon_days": H, "n_paths": N_PATHS, "tickers": {}}
    for tk in cfg["tickers"]:
        daily = fetch_daily_ohlcv(tk)
        dates = [r[0].isoformat() for r in daily]
        close = np.array([r[4] for r in daily], float)
        r = np.diff(np.log(close))
        r = r[-750:]
        S0 = float(close[-1])
        info = {"label": LABELS.get(tk, tk), "last": S0, "last_date": dates[-1],
                "history": {"dates": dates[-120:], "close": close[-120:].round(4).tolist()},
                "models": {}}
        paths_by_model = {}
        for M in MODELS:
            p = M.fit(r)
            lr = M.simulate(p, r, N_PATHS, H, RNG)
            paths, final, bands = summarize(S0, lr)
            paths_by_model[M.name] = paths
            info["models"][M.name] = {"params": p, "horizons": final, "bands": bands}
        ens = np.concatenate([np.log(p / S0) for p in paths_by_model.values()])
        ens = np.diff(np.concatenate([np.zeros((len(ens), 1)), ens], axis=1), axis=1)
        _, final, bands = summarize(S0, ens)
        info["models"]["Ensemble"] = {"params": {}, "horizons": final, "bands": bands}
        paths_by_model["Ensemble"] = S0 * np.exp(np.cumsum(ens, axis=1))
        info["price_grid"], info["surface"] = surface(S0, paths_by_model)
        g = info["models"]["GARCH"]["params"]
        info["vol_today_pct"] = round(g["sigma_next"] * 100, 2)
        info["vol_normal_pct"] = round(g["sigma_long"] * 100, 2)
        info["regime_agite_pct"] = round(info["models"]["Régimes"]["params"]["p_now"][1] * 100, 1)
        info["liquidity"] = liquidity(tk, daily)
        result["tickers"][tk] = info
        e5 = final["5"]
        print(f"{info['label']:<11} {S0:>8.2f}  P(hausse 5j)={e5['p_up']:.0%}  "
              f"90%: {e5['q05']:.2f}–{e5['q95']:.2f}  vol jour GARCH={info['vol_today_pct']}% "
              f"(normale {info['vol_normal_pct']}%)  régime agité={info['regime_agite_pct']}%")
    (OUT / "latest.json").write_text(json.dumps(result, ensure_ascii=False))
    return result


# ---------------------------------------------------------------- test hors échantillon

def validate(test_days=250, step=5, n=1500):
    """Walk-forward : à chaque origine, on estime avec le passé seulement,
    puis on compare la prévision à ce qui s'est réellement passé."""
    cfg = json.loads((ROOT / "config.json").read_text())
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(7)
    report = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "test_days": test_days, "step": step, "tickers": {}}
    names = [M.name for M in MODELS] + ["Ensemble"]
    pooled = {m: {h: [] for h in (1, 5)} for m in names}
    for tk in cfg["tickers"]:
        _, close = fetch_daily(tk)
        lr_all = np.diff(np.log(close))
        start = max(250, len(lr_all) - test_days)
        rows = {m: {h: [] for h in (1, 5)} for m in names}
        for t0 in range(start, len(lr_all) - 5, step):
            r = lr_all[max(0, t0 - 750):t0]
            sims = {}
            for M in MODELS:
                sims[M.name] = np.cumsum(M.simulate(M.fit(r), r, n, 5, rng), axis=1)
            sims["Ensemble"] = np.concatenate(list(sims.values()))
            for h in (1, 5):
                actual = lr_all[t0:t0 + h].sum()
                base_up = float((np.convolve(r, np.ones(h), "valid") > 0).mean())
                for m, cum in sims.items():
                    x = cum[:, h - 1]
                    rows[m][h].append((float((x > 0).mean()), float(np.quantile(x, 0.05)),
                                       float(np.quantile(x, 0.95)), float(actual), base_up))
        report["tickers"][tk] = {"label": LABELS.get(tk, tk), "metrics": {}}
        for m in names:
            report["tickers"][tk]["metrics"][m] = {}
            for h in (1, 5):
                pooled[m][h] += rows[m][h]
                report["tickers"][tk]["metrics"][m][str(h)] = metrics(rows[m][h])
        e = report["tickers"][tk]["metrics"]["Ensemble"]["5"]
        print(f"{LABELS.get(tk, tk):<11} n={e['n']:>3}  direction 5j={e['hit_rate']:.0%}  "
              f"Brier skill={e['brier_skill']:+.3f}  couverture 90%={e['coverage90']:.0%}")
    report["pooled"] = {m: {str(h): metrics(pooled[m][h]) for h in (1, 5)} for m in names}
    (OUT / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print("\nTous tickers confondus (horizon 1 j / 5 j) :")
    for m in names:
        a, b = report["pooled"][m]["1"], report["pooled"][m]["5"]
        print(f"  {m:<10} direction {a['hit_rate']:.1%} / {b['hit_rate']:.1%}   "
              f"Brier skill {a['brier_skill']:+.3f} / {b['brier_skill']:+.3f}   "
              f"couverture 90% {a['coverage90']:.0%} / {b['coverage90']:.0%}")
    return report


def metrics(rows):
    a = np.array(rows)
    p, q05, q95, y, base = a.T
    up = (y > 0).astype(float)
    brier = np.mean((p - up) ** 2)
    brier_base = np.mean((base - up) ** 2)  # référence : fréquence historique de hausse
    # stratégie test : acheter seulement quand P(hausse) > 55 %
    sel = p > 0.55
    return {"n": int(len(a)),
            "hit_rate": float(np.mean((p > 0.5) == (y > 0))),
            "always_up_rate": float(up.mean()),
            "brier": float(brier), "brier_skill": float(1 - brier / brier_base),
            "coverage90": float(np.mean((y >= q05) & (y <= q95))),
            "signal_n": int(sel.sum()),
            "signal_avg_ret_pct": float(np.mean(np.expm1(y[sel])) * 100) if sel.any() else None,
            "all_avg_ret_pct": float(np.mean(np.expm1(y)) * 100)}


def summary_fr(val):
    """Verdict en clair, calculé à partir des chiffres (pas écrit à la main)."""
    P = val["pooled"]
    e5, e1 = P["Ensemble"]["5"], P["Ensemble"]["1"]
    best = max(P, key=lambda m: P[m]["5"]["brier_skill"])
    bs = P[best]["5"]["brier_skill"]
    if bs > 0.02:
        tend = (f"Le modèle {best} bat un peu la référence sur la direction à 5 séances "
                f"(score Brier {bs:+.3f}). C'est faible : à confirmer sur d'autres périodes avant d'en faire un signal.")
    else:
        tend = (f"Aucun modèle ne prévoit la direction mieux que la simple fréquence historique de hausse "
                f"(meilleur score Brier à 5 séances : {bs:+.3f} pour {best}). "
                "Les probabilités de hausse affichées ne sont donc pas un signal d'achat.")
    cov = P["GARCH"]["5"]["coverage90"]
    risque = (f"Les intervalles de prix sont en revanche plutôt bien calibrés : le vrai prix est tombé dans l'intervalle à 90 % "
              f"{cov:.0%} du temps avec GARCH ({e5['coverage90']:.0%} pour l'ensemble). "
              "L'outil sert donc à mesurer le risque (taille du stop, objectifs réalistes), pas à deviner le sens.")
    if abs(cov - 0.9) > 0.06:
        risque = (f"Même les intervalles de prix sont mal calibrés ({cov:.0%} de couverture pour GARCH au lieu de 90 %), "
                  "donc l'outil reste descriptif.")
    return (f"Sur {e5['n']} prévisions à 5 séances (7 titres, dernière année), l'ensemble a vu juste sur la direction "
            f"{e5['hit_rate']:.0%} du temps, contre {e5['always_up_rate']:.0%} en pariant toujours sur la hausse. "
            f"{tend} {risque}")


def build_html():
    data = json.loads((OUT / "latest.json").read_text())
    val = json.loads((OUT / "validation.json").read_text())
    val["summary_fr"] = summary_fr(val)
    html = (ROOT / "sim_template.html").read_text()
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace(
        "__VAL__", json.dumps({"pooled": val["pooled"], "summary_fr": val["summary_fr"]}, ensure_ascii=False))
    (OUT / "atelier.html").write_text(html)
    print("écrit", OUT / "atelier.html", f"({len(html) // 1024} Ko)")
    print(val["summary_fr"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("run", "all"):
        run()
    if cmd in ("validate", "all"):
        validate()
    if cmd in ("html", "all"):
        build_html()
