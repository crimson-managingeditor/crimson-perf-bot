#!/usr/bin/env python3
"""Harvard Athletics roster scraper + change tracker (gocrimson.com, Sidearm platform).

  python3 rosters.py sports                 # list the sport slugs it can scrape
  python3 rosters.py show <sport> [sortby]  # print a roster; sortby = class|hometown|highschool|name|pos
  python3 rosters.py scrape [sport ...]     # scrape -> research/rosters_data/<sport>.json (all sports if none given)
  python3 rosters.py diff  [sport ...]      # scrape live, compare to saved json -> added/removed players
  python3 rosters.py track [sport ...]      # scheduled pass: diff, update baseline, post changes to Slack
  python3 rosters.py ping                   # post a one-line routing test to the roster channel

Data feeds the /roster Slack command (reads the saved json) and a scheduled tracker
that posts adds/drops to Slack.

env:  SLACK_BOT_TOKEN, ROSTER_SLACK_CHANNEL (default #sports-admin)
"""
import json, os, re, sys, urllib.request
from bs4 import BeautifulSoup

BASE = "https://gocrimson.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rosters_data")

# Roster moves are a sports-desk story, so they go to the sports channel rather
# than the analytics firehose. Deliberately chat.postMessage and not
# SLACK_WEBHOOK_URL: an incoming webhook is welded to the one channel it was
# created for, so anything using it lands in analytics no matter what.
CHANNEL = os.environ.get("ROSTER_SLACK_CHANNEL", "#sports-admin")

STADIUM, GREEN, RED = "\U0001F3DF\uFE0F", "\U0001F7E2", "\U0001F534"

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

def slack(text, channel=None):
    """Post to Slack as the bot. Same shape as research/courtbot.py."""
    tok = os.environ.get("SLACK_BOT_TOKEN")
    ch = channel or CHANNEL
    if not tok or not ch:
        print("(no SLACK_BOT_TOKEN/channel — not posting)"); return False
    req = urllib.request.Request("https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": ch, "text": text, "unfurl_links": False}).encode(),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
        if not r.get("ok"):
            # channel_not_found / not_in_channel both mean: invite the bot to `ch`.
            print(f"slack error posting to {ch}: {r.get('error')}"); return False
        print(f"posted to {ch}"); return True
    except Exception as e:
        print(f"slack error posting to {ch}: {e}"); return False

def format_changes(changes):
    """changes = [(slug, 'ADDED'|'LEFT', name)] -> the Slack message."""
    return f"*{STADIUM} Harvard roster changes*\n" + "\n".join(
        f"{GREEN if tag == 'ADDED' else RED} *{slug}* — {tag.title()}: {name}"
        for slug, tag, name in changes)

def cmd_track(argv):
    """One pass for the scheduled tracker: scrape every sport, diff vs the saved baseline,
    UPDATE the baseline, print one line per change (empty output = nothing changed), and
    post the changes to the roster channel."""
    slugs = argv or sports()
    changes = []
    for slug in slugs:
        old = _load(slug)
        try: new = scrape_sport(slug)
        except Exception: continue
        if not new: continue
        if old is not None:
            on = {p["name"] for p in old}; nn = {p["name"] for p in new}
            for name in sorted(nn - on): changes.append((slug, "ADDED", name))
            for name in sorted(on - nn): changes.append((slug, "LEFT", name))
        _save(slug, new)
    for slug, tag, name in changes:
        print(f"{slug}\t{tag}\t{name}")     # kept: the run log is the audit trail
    if changes:
        slack(format_changes(changes))
    else:
        print("no roster changes")

def cmd_ping(argv):
    """Post a one-line test so you can confirm where roster changes land."""
    ch = argv[0] if argv else None
    slack("Roster tracker routing test — roster changes will post here.", ch)

def main():
    if len(sys.argv) < 2: print(__doc__); return
    cmd, argv = sys.argv[1], sys.argv[2:]
    {"sports": lambda: cmd_sports(), "show": lambda: cmd_show(argv),
     "scrape": lambda: cmd_scrape(argv), "diff": lambda: cmd_diff(argv),
     "track": lambda: cmd_track(argv), "ping": lambda: cmd_ping(argv),
     }.get(cmd, lambda: print(__doc__))()

if __name__ == "__main__":
    main()
