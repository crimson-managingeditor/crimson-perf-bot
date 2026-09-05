#!/usr/bin/env python3
"""Harvard Athletics roster scraper + change tracker (gocrimson.com, Sidearm platform).

  python3 rosters.py sports                 # list the sport slugs it can scrape
  python3 rosters.py show <sport> [sortby]  # print a roster; sortby = class|hometown|highschool|name|pos
  python3 rosters.py scrape [sport ...]     # scrape -> research/rosters_data/<sport>.json (all sports if none given)
  python3 rosters.py diff  [sport ...]      # scrape live, compare to saved json -> added/removed players

Data feeds the /roster Slack command (reads the saved json) and a scheduled tracker
that posts adds/drops to Slack.
"""
import json, os, re, sys, urllib.request
from bs4 import BeautifulSoup

BASE = "https://gocrimson.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rosters_data")

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def sports():
    """Discover sport slugs that have a roster, from the site nav (homepage lists them all)."""
    html = get(BASE + "/")
    return sorted(set(re.findall(r"/sports/([a-z0-9\-]+)/roster", html)))

def scrape_sport(slug):
    html = get(f"{BASE}/sports/{slug}/roster")
    s = BeautifulSoup(html, "html.parser")
    def txt(el, cls):
        e = el.select_one(".sidearm-roster-player-" + cls)
        return e.get_text(" ", strip=True) if e else ""
    out = []
    for p in s.select("li.sidearm-roster-player"):
        name = re.sub(r"^\s*#?\d+\s+", "", txt(p, "name")).strip()
        if not name:
            continue
        pos = txt(p, "position")
        out.append({
            "name": name,
            "jersey": txt(p, "jersey"),
            "class": txt(p, "academic-year"),
            "pos": pos.split()[0] if pos else "",
            "hometown": txt(p, "hometown"),
            "highschool": txt(p, "highschool"),
        })
    # dedupe by (name, jersey) — Sidearm can render a player in >1 view
    seen, uniq = set(), []
    for p in out:
        k = (p["name"], p["jersey"])
        if k not in seen:
            seen.add(k); uniq.append(p)
    return uniq

def _save(slug, players):
    os.makedirs(DATA, exist_ok=True)
    json.dump(players, open(os.path.join(DATA, slug + ".json"), "w"), ensure_ascii=False, indent=1)

def _load(slug):
    try: return json.load(open(os.path.join(DATA, slug + ".json")))
    except Exception: return None

def cmd_sports():
    sl = sports(); print(f"{len(sl)} sports:\n  " + "\n  ".join(sl))

def cmd_show(argv):
    if not argv: print("usage: show <sport> [sortby]"); return
    slug = argv[0]; key = argv[1] if len(argv) > 1 else "jersey"
    players = _load(slug) or scrape_sport(slug)
    kf = {"class": lambda p: p["class"], "hometown": lambda p: p["hometown"],
          "highschool": lambda p: p["highschool"], "name": lambda p: p["name"].split()[-1],
          "pos": lambda p: p["pos"], "jersey": lambda p: int(re.sub(r"\D", "", p["jersey"]) or 999)}
    players = sorted(players, key=kf.get(key, kf["jersey"]))
    print(f"{slug} — {len(players)} players (by {key}):\n")
    for p in players:
        print(f"  #{p['jersey']:<3} {p['name']:<24} {p['class']:<4} {p['pos']:<4} "
              f"{p['hometown']:<24} {p['highschool']}")

def cmd_scrape(argv):
    slugs = argv or sports()
    for slug in slugs:
        try:
            pl = scrape_sport(slug); _save(slug, pl); print(f"  {slug}: {len(pl)} players")
        except Exception as e:
            print(f"  {slug}: ERROR {e}")

def cmd_diff(argv):
    slugs = argv or [f[:-5] for f in os.listdir(DATA)] if os.path.isdir(DATA) else argv
    changes = []
    for slug in slugs:
        old = _load(slug)
        if old is None: continue
        try: new = scrape_sport(slug)
        except Exception as e: print(f"  {slug}: fetch error {e}"); continue
        on = {p["name"] for p in old}; nn = {p["name"] for p in new}
        for name in nn - on: changes.append((slug, "＋ ADDED", name))
        for name in on - nn: changes.append((slug, "－ LEFT", name))
    for slug, tag, name in changes: print(f"  {slug:22} {tag}  {name}")
    if not changes: print("  no roster changes")
    return changes

def cmd_track(argv):
    """One pass for the scheduled tracker: scrape every sport, diff vs the saved baseline,
    UPDATE the baseline, and print one line per change (empty output = nothing changed)."""
    slugs = argv or sports()
    for slug in slugs:
        old = _load(slug)
        try: new = scrape_sport(slug)
        except Exception: continue
        if not new: continue
        if old is not None:
            on = {p["name"] for p in old}; nn = {p["name"] for p in new}
            for name in sorted(nn - on): print(f"{slug}\tADDED\t{name}")
            for name in sorted(on - nn): print(f"{slug}\tLEFT\t{name}")
        _save(slug, new)

def main():
    if len(sys.argv) < 2: print(__doc__); return
    cmd, argv = sys.argv[1], sys.argv[2:]
    {"sports": lambda: cmd_sports(), "show": lambda: cmd_show(argv),
     "scrape": lambda: cmd_scrape(argv), "diff": lambda: cmd_diff(argv),
     "track": lambda: cmd_track(argv)}.get(cmd, lambda: print(__doc__))()

if __name__ == "__main__":
    main()
