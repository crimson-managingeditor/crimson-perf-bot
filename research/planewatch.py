#!/usr/bin/env python3
"""
penny-plane: ping a Slack channel whenever aircraft PLANE_HEX takes off or lands.

Data: free ADS-B community feeds (adsb.fi / adsb.lol / airplanes.live) — current
state only, so we poll and detect ground<->air transitions. `alt_baro == "ground"`
is the on-ground signal.

State machine (robust to coverage gaps — a plane that drops out of receiver range
mid-flight does NOT reset our last *definitive* status, so the eventual reappearance
still resolves to the right event):
  last=ground, now=air   -> TAKEOFF   (departure airport = last on-ground fix)
  last=air,    now=ground-> LANDING   (arrival airport   = current on-ground fix)
  unseen / unknown       -> no change, no alert
  first ever run         -> record baseline, no alert

One poll per invocation; the workflow calls it in a loop for ~5-min-ish cadence.
Stdlib only. Env:
  SLACK_BOT_TOKEN   bot token (chat:write; chat:write.public if bot isn't in the channel)
  PLANE_CHANNEL     default "#penny-plane"
  PLANE_HEX         default "a642a3"
  STATE_FILE        rolling state JSON (kept in Actions cache)   default plane/.state/state.json
  FLIGHT_LOG        durable event log (committed)                default plane/flights.log
  AIRPORTS_CSV      default plane/airports.csv
  PLANE_DRY=1       print instead of posting
"""
import os, sys, json, math, csv, time, urllib.request, datetime

HEX      = os.environ.get("PLANE_HEX", "a642a3").lower()
CHANNEL  = os.environ.get("PLANE_CHANNEL", "#penny-plane")
STATE_FILE = os.environ.get("STATE_FILE", "plane/.state/state.json")
FLIGHT_LOG = os.environ.get("FLIGHT_LOG", "plane/flights.log")
AIRPORTS_CSV = os.environ.get("AIRPORTS_CSV", "plane/airports.csv")
DRY = os.environ.get("PLANE_DRY") or ("--dry-run" in sys.argv)
UA  = {"User-Agent": "penny-plane-watch/1.0 (Harvard Crimson newsroom; dhruv.patel@thecrimson.com)"}
GLOBE = f"https://globe.adsbexchange.com/?icao={HEX}"

SOURCES = [
    ("adsb.fi",        f"https://opendata.adsb.fi/api/v2/hex/{HEX}"),
    ("adsb.lol",       f"https://api.adsb.lol/v2/hex/{HEX}"),
    ("airplanes.live", f"https://api.airplanes.live/v2/hex/{HEX}"),
]

# ---------------------------------------------------------------- data source
def fetch():
    """Return (obs, source) where obs is a dict, or (None, None) if unseen/unreachable.
    obs.status in {'ground','air'} when known, else None (seen but altitude ambiguous)."""
    for name, url in SOURCES:
        try:
            r = urllib.request.Request(url, headers=UA)
            d = json.load(urllib.request.urlopen(r, timeout=25))
        except Exception as e:
            print(f"  {name}: {e}")
            continue
        ac = d.get("ac") or d.get("aircraft") or []
        if not ac:
            print(f"  {name}: not currently seen")
            continue
        a = ac[0]
        alt = a.get("alt_baro", a.get("alt_geom"))
        if alt == "ground":
            status = "ground"
        elif isinstance(alt, (int, float)):
            status = "air"
        else:
            status = None
        return ({
            "status": status, "reg": (a.get("r") or "").strip(),
            "type": a.get("t"), "desc": a.get("desc"),
            "flight": (a.get("flight") or "").strip(),
            "lat": a.get("lat"), "lon": a.get("lon"),
            "alt": alt, "gs": a.get("gs"), "track": a.get("track"),
            "baro_rate": a.get("baro_rate"), "owner": a.get("ownOp"),
        }, name)
    return None, None

# ---------------------------------------------------------------- airports
_AIRPORTS = None
def _load_airports():
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = []
        try:
            with open(AIRPORTS_CSV, newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        _AIRPORTS.append((float(row["lat"]), float(row["lon"]), row))
                    except Exception:
                        pass
        except FileNotFoundError:
            print(f"  (airports db {AIRPORTS_CSV} missing — positions only)")
    return _AIRPORTS

def _km(a, b, c, d):
    R = 6371.0; p = math.pi / 180
    x = math.sin((c-a)*p/2)**2 + math.cos(a*p)*math.cos(c*p)*math.sin((d-b)*p/2)**2
    return 2 * R * math.asin(math.sqrt(x))

def nearest_airport(lat, lon):
    if lat is None or lon is None:
        return None
    aps = _load_airports()
    best, bestkm = None, 1e9
    for alat, alon, row in aps:
        k = _km(lat, lon, alat, alon)
        if k < bestkm:
            best, bestkm = row, k
    if not best:
        return None
    return {**best, "km": round(bestkm, 1)}

def airport_label(ap):
    """Human label. Confident when the fix is basically on the field (<= 6 km)."""
    if not ap:
        return None
    code = ap["iata"] or ap["ident"]
    place = ", ".join(x for x in (ap["muni"], ap["country"]) if x)
    base = f"{ap['name']} ({code})" + (f" — {place}" if place else "")
    if ap["km"] > 6:
        return f"near {base} (~{ap['km']:.0f} km away)"
    return base

# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)

def log_event(kind, ap, obs):
    os.makedirs(os.path.dirname(FLIGHT_LOG) or ".", exist_ok=True)
    code = (ap or {}).get("iata") or (ap or {}).get("ident") or "?"
    with open(FLIGHT_LOG, "a") as f:
        f.write(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}\t{kind}\t{code}\t"
                f"{obs.get('lat')},{obs.get('lon')}\t{obs.get('reg')}\n")

# ---------------------------------------------------------------- slack
def et_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%a %b %-d, %-I:%M %p ET")
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d, %H:%M UTC")

def post(blocks, text):
    token = os.environ.get("SLACK_BOT_TOKEN")
    if DRY or not token:
        print(f"── {'DRY' if DRY else 'NO TOKEN'} — would post to {CHANNEL} ──\n{text}\n")
        return True
    try:
        rq = urllib.request.Request("https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": CHANNEL, "text": text, "blocks": blocks,
                             "unfurl_links": False}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(rq, timeout=20))
        if not r.get("ok"):
            print("slack post failed:", r.get("error"))
        return bool(r.get("ok"))
    except Exception as e:
        print("slack post error:", e); return False

def craft(obs):
    reg = obs.get("reg") or HEX.upper()
    t = obs.get("desc") or obs.get("type") or ""
    owner = obs.get("owner") or ""
    return reg, t, owner

def alert_takeoff(obs, dep_ap):
    reg, t, owner = craft(obs)
    dep = airport_label(dep_ap) or (f"{obs['lat']:.3f}, {obs['lon']:.3f}" if obs.get("lat") else "unknown location")
    when = et_now()
    text = f"🛫 {reg} took off — departed {dep} ({when})"
    ctx = " · ".join(x for x in [t, owner, (f"climbing through {int(obs['alt']):,} ft" if isinstance(obs.get('alt'), (int,float)) else None),
                                 (f"{int(obs['gs'])} kt" if isinstance(obs.get('gs'), (int,float)) else None)] if x)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"🛫 *{reg} took off*\n*Departed:* {dep}\n_{when}_"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{ctx}  ·  <{GLOBE}|track on ADS-B Exchange>"}]},
    ]
    return blocks, text

def alert_landing(obs, arr_ap, flight):
    reg, t, owner = craft(obs)
    arr = airport_label(arr_ap) or (f"{obs['lat']:.3f}, {obs['lon']:.3f}" if obs.get("lat") else "unknown location")
    when = et_now()
    dur = ""
    dep_line = ""
    if flight and flight.get("dep_ts"):
        try:
            t0 = datetime.datetime.fromisoformat(flight["dep_ts"])
            mins = int((datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() // 60)
            dur = f"{mins//60}h {mins%60:02d}m" if mins >= 60 else f"{mins}m"
        except Exception:
            pass
    if flight and flight.get("dep_label"):
        dep_line = f" from {flight['dep_label']}"
    text = f"🛬 {reg} landed — arrived {arr}{(' , '+dur+' flight' if dur else '')} ({when})"
    body = f"🛬 *{reg} landed*\n*Arrived:* {arr}\n_{when}_"
    if dep_line or dur:
        body += "\n" + " · ".join(x for x in [f"Flight time {dur}" if dur else "", f"Departed{dep_line}" if dep_line else ""] if x)
    ctx = " · ".join(x for x in [t, owner, f"<{GLOBE}|track on ADS-B Exchange>"] if x)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]},
    ]
    return blocks, text

# ---------------------------------------------------------------- main
def main():
    if os.environ.get("PLANE_TEST_PING") == "1":
        reg = HEX.upper()
        text = f"✅ penny-plane watch is live — tracking N502P ({reg}) for takeoffs & landings."
        blocks = [{"type": "section", "text": {"type": "mrkdwn",
                   "text": f"✅ *penny-plane watch is live*\nTracking *N502P* (Gulfstream G500, PSP Capital) — "
                           f"you'll get a 🛫 on takeoff and 🛬 on landing.\n<{GLOBE}|live map>"}}]
        ok = post(blocks, text)
        print(f"test ping posted={ok}")
        return
    obs, src = fetch()
    st = load_state()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if obs is None:
        print("unseen this poll — no change"); return
    cur = obs["status"]
    if cur is None:
        print("seen but altitude ambiguous — no change"); return
    print(f"[{src}] status={cur} alt={obs.get('alt')} gs={obs.get('gs')} "
          f"pos={obs.get('lat')},{obs.get('lon')} reg={obs.get('reg')}")

    prev = st.get("status")
    # record most-recent on-ground fix (used to name the DEPARTURE airport at next takeoff)
    if cur == "ground":
        st["last_ground"] = {"lat": obs.get("lat"), "lon": obs.get("lon"), "ts": now_iso}
    st["last_seen"] = now_iso

    if prev is None:
        st["status"] = cur
        save_state(st)
        print(f"baseline set: {cur} (no alert)")
        return

    if cur == prev:
        st["status"] = cur
        save_state(st)
        return

    if prev == "ground" and cur == "air":
        # departure airport from the last on-ground fix if we have one, else current
        g = st.get("last_ground") or {}
        dep_ap = nearest_airport(g.get("lat"), g.get("lon")) or nearest_airport(obs.get("lat"), obs.get("lon"))
        blocks, text = alert_takeoff(obs, dep_ap)
        ok = post(blocks, text)
        st["status"] = "air"
        st["flight"] = {"dep_ts": now_iso,
                        "dep_label": airport_label(dep_ap),
                        "dep_code": (dep_ap or {}).get("iata") or (dep_ap or {}).get("ident")}
        log_event("TAKEOFF", dep_ap, obs)
        save_state(st)
        print(f"TAKEOFF posted={ok}")
        return

    if prev == "air" and cur == "ground":
        arr_ap = nearest_airport(obs.get("lat"), obs.get("lon"))
        blocks, text = alert_landing(obs, arr_ap, st.get("flight"))
        ok = post(blocks, text)
        st["status"] = "ground"
        st["flight"] = None
        log_event("LANDING", arr_ap, obs)
        save_state(st)
        print(f"LANDING posted={ok}")
        return

if __name__ == "__main__":
    main()
