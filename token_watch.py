"""Veille : annonce d'un jeton crypto OFFICIEL de Rockstar Games ou Take-Two.

Ne regarde que des sources officielles (dépôts SEC de Take-Two, communiqués de
Take-Two, newswire de Rockstar, et communiqués de presse diffusés par fil de presse
au nom de Take-Two). Les articles de blogs ou les jetons « GTA » créés par des
inconnus ne déclenchent rien.

Usage :
  python token_watch.py          vérifie et envoie une alerte ntfy si nouveau
  python token_watch.py probe    affiche ce que chaque source renvoie, sans alerte
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "token_watch.json")
UA = {"User-Agent": "signaux-btc token watch (github.com/adematmani-afk/signaux-btc)",
      "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}

# Mots qui signalent un jeton / de la crypto.
CRYPTO = re.compile(r"\b(crypto\w*|token(?:s|ized|isation|ization)?|blockchain|web3|nfts?|"
                    r"stablecoin|airdrop|smart contract|on-chain|onchain)\b", re.I)
# Pour les fils de presse généraux : le communiqué doit venir de Take-Two / Rockstar.
ISSUER = re.compile(r"take-two interactive|take two interactive|rockstar games", re.I)

# Sources officielles. "kind" = "official" : tout article qui parle de crypto compte.
# "kind" = "wire" : il faut en plus que l'émetteur soit Take-Two / Rockstar.
SOURCES = [
    {"name": "SEC (dépôts 8-K de Take-Two)", "kind": "official",
     "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000946581&type=8-K&dateb=&owner=include&count=20&output=atom"},
    {"name": "Take-Two (communiqués)", "kind": "official",
     "url": "https://ir.take2games.com/rss/news-releases.xml"},
    {"name": "Take-Two (communiqués, autre flux)", "kind": "official",
     "url": "https://ir.take2games.com/news-releases/rss"},
    {"name": "Rockstar Newswire", "kind": "official",
     "url": "https://www.rockstargames.com/newswire/rss"},
    {"name": "Rockstar Newswire (autre flux)", "kind": "official",
     "url": "https://www.rockstargames.com/newswire/feed"},
    {"name": "Google Actualités (fils de presse officiels)", "kind": "wire",
     "url": "https://news.google.com/rss/search?q=%28%22Take-Two%22+OR+%22Rockstar+Games%22%29+%28token+OR+crypto+OR+blockchain+OR+cryptocurrency%29+%28site%3Abusinesswire.com+OR+site%3Aprnewswire.com+OR+site%3Aglobenewswire.com+OR+site%3Atake2games.com+OR+site%3Arockstargames.com%29&hl=en-US&gl=US&ceid=US%3Aen"},
]
MAX_AGE = timedelta(days=7)


def text(el, *names):
    for n in names:
        for child in el.iter():
            if child.tag.split("}")[-1] == n:
                if n == "link" and child.get("href"):
                    return child.get("href")
                if child.text:
                    return child.text.strip()
    return ""


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fetch(src):
    r = requests.get(src["url"], headers=UA, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        items.append({
            "title": text(el, "title"),
            "link": text(el, "link"),
            "summary": re.sub(r"<[^>]+>", " ", text(el, "description", "summary", "content")),
            "date": parse_date(text(el, "pubDate", "updated", "published")),
            "id": text(el, "guid", "id") or text(el, "link"),
        })
    return items


def matches(src, it):
    blob = f"{it['title']} {it['summary']}"
    if not CRYPTO.search(blob):
        return False
    if src["kind"] == "wire" and not ISSUER.search(blob):
        return False
    return True


def notify(title, lines):
    print(f"\n[{title}]\n" + "\n".join(lines))
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC manquant : notification non envoyée")
        return
    requests.post("https://ntfy.sh/", json={"topic": topic, "title": title, "message": "\n".join(lines),
                                           "priority": 5, "tags": ["rotating_light"]}, timeout=20)


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except FileNotFoundError:
        return {"seen": [], "initialised": False}


def main(probe=False):
    st = load_state()
    seen = set(st["seen"])
    now = datetime.now(timezone.utc)
    alerts = 0
    for src in SOURCES:
        try:
            items = fetch(src)
        except Exception as ex:
            print(f"- {src['name']} : inaccessible ({ex.__class__.__name__}: {str(ex)[:80]})")
            continue
        hits = [it for it in items if matches(src, it)]
        print(f"- {src['name']} : {len(items)} articles, {len(hits)} parlent de crypto")
        if probe:
            for it in items[:3]:
                print(f"    · {it['date']:%Y-%m-%d} {it['title'][:100]}" if it["date"] else f"    · {it['title'][:100]}")
            for it in hits[:5]:
                print(f"    ! {it['title'][:100]} {it['link']}")
            continue
        for it in hits:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            if not st["initialised"]:
                continue  # premier passage : on note l'existant sans alerter
            if it["date"] and now - it["date"] > MAX_AGE:
                continue
            alerts += 1
            notify("JETON OFFICIEL ? Rockstar / Take-Two", [
                it["title"][:200],
                f"Source officielle : {src['name']}",
                it["link"],
                "Avant tout achat : ouvre ce lien toi-même et vérifie l'adresse du contrat sur le site "
                "officiel. Des copies du jeton apparaîtront dans les minutes qui suivent.",
            ])
    if not probe:
        st["seen"] = sorted(seen)[-500:]
        st["initialised"] = True
        st["last_check"] = now.isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        old = json.dumps({k: v for k, v in load_state().items() if k != "last_check"}, sort_keys=True)
        new = json.dumps({k: v for k, v in st.items() if k != "last_check"}, sort_keys=True)
        if old != new:  # n'écrit (et donc ne committe) que si quelque chose a changé
            json.dump(st, open(STATE_PATH, "w"), indent=1, ensure_ascii=False)
    print(f"{alerts} alerte(s) envoyée(s)")


if __name__ == "__main__":
    main(probe=len(sys.argv) > 1 and sys.argv[1] == "probe")
