#!/usr/bin/env python3
"""
Crimson Watch v2 — a single always-on daemon that both:
  • serves the Slack /link /unlink /links slash commands (HTTP), and
  • runs the change-detection scheduler,
backed by ONE SQLite database (per-watch rows). Built to scale to thousands of
watches across many reporters — no giant-JSON parse, no repo write-contention,
private by default. Reuses the same extraction pipeline as the MVP.

Run:   SLACK_SIGNING_SECRET=… SLACK_BOT_TOKEN=… python app.py
Deploy: behind a Cloudflare Tunnel (free HTTPS) + systemd. See README.md.

Env:
  SLACK_SIGNING_SECRET   verify slash-command requests            (required)
  SLACK_BOT_TOKEN        xoxb-… for channel/DM alerts             (required)
  SLACK_WEBHOOK_URL      fallback channel if a post fails         (optional)
  DB_PATH                sqlite file (default watcher.db)
  PORT                   HTTP port for Slack (default 8787)
  WORKERS                global concurrent fetches (default 40)
  PER_DOMAIN             max concurrent fetches per domain (default 3)
  RENDER_WORKERS         concurrent headless renders (default 2)
  TICK_SECONDS           scheduler cycle (default 15)
"""
import os, re, json, time, html, hashlib, difflib, base64, sqlite3, threading, urllib.request, urllib.parse, unicodedata, datetime, collections
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify

# ----------------------------------------------------------------- config
SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
WEBHOOK_URL    = os.environ.get("SLACK_WEBHOOK_URL", "")
DB_PATH        = os.environ.get("DB_PATH", "watcher.db")
PORT           = int(os.environ.get("PORT", "8787"))
WORKERS        = int(os.environ.get("WORKERS", "40"))
PER_DOMAIN     = int(os.environ.get("PER_DOMAIN", "3"))
RENDER_WORKERS = int(os.environ.get("RENDER_WORKERS", "2"))
TICK_SECONDS   = int(os.environ.get("TICK_SECONDS", "15"))
INTERVALS      = (5, 30, 60, 120)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
OPT_COLS = ("css", "xpath", "json", "subtract", "extract", "ignore", "trigger")
FLAG_COLS = ("sort", "dedupe", "render")

def now(): return datetime.datetime.now(datetime.timezone.utc)
def iso(dt): return dt.isoformat()

# ----------------------------------------------------------------- database
_local = threading.local()
def db():
    if not getattr(_local, "conn", None):
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return _local.conn

def init_db():
    c = sqlite3.connect(DB_PATH); c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS watches (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      url TEXT NOT NULL, channel TEXT DEFAULT '', channel_name TEXT DEFAULT '',
      added_by TEXT DEFAULT '', added_by_id TEXT DEFAULT '',
      interval_min INTEGER DEFAULT 60,
      css TEXT DEFAULT '', xpath TEXT DEFAULT '', json TEXT DEFAULT '',
      subtract TEXT DEFAULT '', extract TEXT DEFAULT '', ignore TEXT DEFAULT '',
      trigger TEXT DEFAULT '', sort INTEGER DEFAULT 0, dedupe INTEGER DEFAULT 0,
      render INTEGER DEFAULT 0,
      hash TEXT, snapshot TEXT, last_check TEXT, next_check TEXT,
      created_at TEXT,
      UNIQUE(url, channel, css, xpath, json, subtract, extract, ignore)
    );
    CREATE INDEX IF NOT EXISTS idx_next ON watches(next_check);
    CREATE INDEX IF NOT EXISTS idx_channel ON watches(channel);
    CREATE TABLE IF NOT EXISTS changelog (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event TEXT, url TEXT, detail TEXT
    );
    """)
    c.commit(); c.close()

def log_event(event, url, detail=""):
    try:
        db().execute("INSERT INTO changelog(ts,event,url,detail) VALUES(?,?,?,?)",
                     (iso(now()), event, url, detail)); db().commit()
    except Exception as e:
        print("log_event error:", e)

# ----------------------------------------------------------------- pipeline
def fetch_target(url):
    m = re.match(r"https?://docs\.google\.com/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]+)", url)
    if m:
        kind = m.group(1)
        return f"https://docs.google.com/{kind}/d/{m.group(2)}/export?format=" + ("csv" if kind == "spreadsheets" else "txt")
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    if "dropbox.com/" in url:
        if "dl=0" in url: return url.replace("dl=0", "dl=1")
        if "dl=1" not in url and "raw=1" not in url:
            return url + ("&dl=1" if "?" in url else "?dl=1")
    return url

def fetch(url, timeout=25, depth=0):
    import gzip, zlib, ssl
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        else:
            raise
    with r:
        raw = r.read(5_000_000)
        cenc = (r.headers.get("Content-Encoding") or "").lower()
        ctype = (r.headers.get("Content-Type") or "").lower()
    if "gzip" in cenc:
        try: raw = gzip.decompress(raw)
        except Exception: pass
    elif "deflate" in cenc:
        try: raw = zlib.decompress(raw)
        except Exception:
            try: raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception: pass
    binary = any(t in ctype for t in ("pdf", "octet-stream", "image/", "zip", "officedocument",
                                      "msword", "ms-excel", "spreadsheetml", "font", "audio/", "video/"))
    textual = (ctype == "") or any(t in ctype for t in ("html", "xml", "json", "csv", "javascript", "text/", "plain"))
    if binary or not textual:
        return "bytes", raw
    charset = None
    mc = re.search(r"charset=([\w-]+)", ctype)
    if mc: charset = mc.group(1)
    if not charset:
        mm = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
        if mm:
            try: charset = mm.group(1).decode("ascii", "ignore")
            except Exception: charset = None
    text = raw.decode(charset or "utf-8", "replace")
    if depth < 2:
        mr = re.search(r'(?is)<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']?\s*\d+\s*;\s*url=([^"\'>\s]+)', text[:4096])
        if mr:
            return fetch(urllib.parse.urljoin(url, html.unescape(mr.group(1))), timeout, depth + 1)
    return "text", text

def fetch_rendered(url, timeout=35):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            pg = b.new_page(user_agent=UA); pg.set_default_timeout(timeout * 1000)
            pg.goto(url, wait_until="networkidle"); html_out = pg.content()
        finally:
            b.close()
    return "text", html_out

def page_text(h):
    if not h: return ""
    def _feed(s):
        if not re.search(r"<(rss|feed|rdf:RDF|urlset|sitemapindex)\b", s[:3000], re.I): return None
        out = []
        for it in re.findall(r"(?is)<(?:item|entry|url)\b.*?</(?:item|entry|url)>", s)[:200]:
            t = re.search(r"(?is)<title[^>]*>(.*?)</title>", it)
            l = re.search(r"(?is)<(?:loc|link)[^>]*>(.*?)</(?:loc|link)>", it) or re.search(r'(?is)<link[^>]+href=["\']([^"\']+)', it)
            t = _clean(t.group(1)) if t else ""; l = _clean(l.group(1)) if l else ""
            if t or l: out.append(f"• {t} {l}".strip())
        return "\n".join(out) if out else None
    t = h.lstrip("﻿")
    if t.lstrip()[:1] in "{[":
        try: return "JSON:\n" + json.dumps(json.loads(t), indent=2, sort_keys=True, ensure_ascii=False)
        except Exception: pass
    fd = _feed(t)
    if fd is not None: return fd
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", t); title = _clean(tm.group(1)) if tm else ""
    s = t
    s = re.sub(r"(?is)<(script|style|noscript|svg|template|iframe|object|embed|canvas|form|button|select)\b.*?</\1>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?is)<(nav|header|footer|aside)\b.*?</\1>", " ", s)
    mm = re.search(r"(?is)<main\b[^>]*>(.*?)</main>", s) or re.search(r"(?is)<article\b[^>]*>(.*?)</article>", s)
    if mm: s = mm.group(1)
    s = re.sub(r"(?is)<(th|td)\b[^>]*>", " | ", s)
    s = re.sub(r"(?is)<tr\b[^>]*>", "\n", s)
    s = re.sub(r"(?is)<li\b[^>]*>", "\n• ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)<h[1-6]\b[^>]*>", "\n\n", s)
    s = re.sub(r"(?is)</(p|div|h[1-6]|section|article|tr|ul|ol|dl|dd|dt|blockquote|pre|table|thead|tbody|caption)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = unicodedata.normalize("NFC", html.unescape(s))
    s = re.sub(r"[ \t\f\v]+", " ", s); s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"(?m)^\|\s*", "", s); s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return (f"[title] {title}\n{s}" if title else s)

def _clean(t): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()
def _as_list(v): return [x for x in str(v or "").split("\n") if x.strip()] if v else []

def extract(kind, payload, w):
    if kind != "text":
        return f"[binary file] sha256:{hashlib.sha256(payload).hexdigest()}"
    doc = payload
    if w["json"] or (doc.lstrip()[:1] in "{[" and not w["css"] and not w["xpath"]):
        try:
            obj = json.loads(doc)
            if w["json"]:
                from jsonpath_ng.ext import parse as jp
                doc2 = json.dumps([m.value for m in jp(w["json"]).find(obj)], indent=2, sort_keys=True, ensure_ascii=False)
            else:
                doc2 = "JSON:\n" + json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
            return _post(doc2, w)
        except Exception as e:
            print("json extract:", e)
    if w["subtract"]:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(doc, "html.parser")
            for el in soup.select(w["subtract"]): el.decompose()
            doc = str(soup)
        except Exception as e: print("subtract:", e)
    if w["css"]:
        try:
            from bs4 import BeautifulSoup
            sel = BeautifulSoup(doc, "html.parser").select(w["css"])
            doc = "\n".join(str(x) for x in sel) if sel else "[css matched nothing]"
        except Exception as e: print("css:", e)
    if w["xpath"]:
        try:
            import lxml.html
            nodes = lxml.html.fromstring(doc).xpath(w["xpath"])
            doc = "\n".join(n if isinstance(n, str) else lxml.html.tostring(n, encoding="unicode") for n in nodes) or "[xpath matched nothing]"
        except Exception as e: print("xpath:", e)
    return _post(page_text(doc), w)

def _post(text, w):
    if w["extract"]:
        try:
            rx = re.compile(w["extract"], re.I | re.M)
            text = "\n".join(m.group(0) for m in rx.finditer(text))
        except Exception: pass
    for pat in _as_list(w["ignore"]):
        try:
            rx = re.compile(pat, re.I)
            text = "\n".join(l for l in text.split("\n") if not rx.search(l))
        except Exception: pass
    lines = text.split("\n")
    if w["dedupe"]:
        seen = set(); lines = [l for l in lines if not (l in seen or seen.add(l))]
    if w["sort"]: lines = sorted(lines)
    return "\n".join(lines).strip()

def text_diff(old, new, n=40):
    d = [l for l in difflib.unified_diff((old or "").split("\n"), (new or "").split("\n"), lineterm="")
         if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return d[:n]

BLOCKED = re.compile(r"(enable javascript|checking your browser|verify you are (a )?human|"
                     r"attention required|just a moment|cf-browser-verification|are you a robot)", re.I)

# ----------------------------------------------------------------- slack
def _slack_api(method, payload):
    rq = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(rq, timeout=20))

def alert(w, diff):
    label = w["url"]
    meta = []
    if w["css"]: meta.append(f"`{w['css']}`")
    if w["xpath"]: meta.append(f"`{w['xpath']}`")
    if w["added_by"]: meta.append(f"flagged by {w['added_by']}")
    sub = ("  ·  " + " · ".join(meta)) if meta else ""
    body = "\n".join(diff)[:2800] or "(content changed — no line diff)"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"🔎 *Page changed* — <{w['url']}|{label[:80]}>{sub}"}},
              {"type": "section", "text": {"type": "mrkdwn", "text": "```\n" + body + "\n```"}}]
    text = f"Page changed: {label[:60]}"
    if w["channel"]:
        try:
            if _slack_api("chat.postMessage", {"channel": w["channel"], "text": text, "blocks": blocks}).get("ok"):
                return f"channel:{w['channel_name'] or w['channel']}"
        except Exception as e: print("channel post:", e)
    if w["added_by_id"]:
        try:
            ch = (_slack_api("conversations.open", {"users": w["added_by_id"]}).get("channel") or {}).get("id")
            if ch and _slack_api("chat.postMessage", {"channel": ch, "text": text, "blocks": blocks}).get("ok"):
                return f"DM->{w['added_by']}"
        except Exception as e: print("dm:", e)
    if WEBHOOK_URL:
        try:
            urllib.request.urlopen(urllib.request.Request(WEBHOOK_URL,
                data=json.dumps({"text": text, "blocks": blocks}).encode(),
                headers={"Content-Type": "application/json"}), timeout=20)
            return "webhook"
        except Exception as e: print("webhook:", e)
    return "undelivered"

def verify_slack(ts, body, sig):
    if not (SIGNING_SECRET and ts and sig): return False
    if abs(time.time() - int(ts)) > 300: return False
    import hmac
    mine = "v0=" + hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig)

# ----------------------------------------------------------------- command parsing
def clean_url(tok):
    if not tok: return ""
    m = re.match(r"^<(.+?)(?:\|.*)?>$", tok)
    u = (m.group(1) if m else tok).strip()
    if u and not re.match(r"^https?://", u, re.I) and re.match(r"^[^\s/]+\.[^\s/]+", u):
        u = "https://" + u
    return u

def parse_command(text):
    tokens = [t for t in text.strip().split() if t]
    url = clean_url(tokens[0]) if tokens else ""
    iv_tok = next((t for t in tokens[1:] if re.match(r"^\d+m?$", t, re.I)), None)
    interval = int(re.sub(r"m$", "", iv_tok, flags=re.I)) if iv_tok else None
    opts = {}
    for k in OPT_COLS:
        # value runs until the next key= OR a bare flag word (sort/dedupe/render) OR end
        m = re.search(r"\b" + k + r"=(.+?)(?=\s+[a-z]+=|\s+(?:sort|dedupe|render)\b|$)", text, re.I)
        opts[k] = m.group(1).strip() if m else ""
    flags = {t.lower() for t in tokens}
    return url, interval, opts, {f: (1 if f in flags else 0) for f in FLAG_COLS}

# ----------------------------------------------------------------- flask (slack)
app = Flask(__name__)

@app.get("/")
def health(): return "Crimson Watch v2 up"

@app.post("/slack")
def slack():
    body = request.get_data(as_text=True)
    if not verify_slack(request.headers.get("X-Slack-Request-Timestamp"), body, request.headers.get("X-Slack-Signature")):
        return jsonify(response_type="ephemeral", text="⛔ signature check failed")
    p = request.form
    cmd = p.get("command", ""); text = p.get("text", "")
    channel = p.get("channel_id", ""); channel_name = p.get("channel_name", "")
    user = p.get("user_name") or p.get("user_id") or "someone"; uid = p.get("user_id", "")
    try:
        if cmd == "/links":
            rows = db().execute("SELECT * FROM watches WHERE channel=? ORDER BY id", (channel,)).fetchall()
            if not rows:
                return jsonify(response_type="ephemeral", text="Nothing watched in this channel yet. `/link <url> <interval>`")
            lines = []
            for r in rows[:60]:
                f = "".join(f"  {k}=`{r[k]}`" for k in OPT_COLS if r[k]) + "".join(f"  {k}" for k in FLAG_COLS if r[k])
                lines.append(f"• {r['url']} — every {r['interval_min']}m{f}")
            where = f"#{channel_name}" if channel_name and channel_name != "directmessage" else "this channel"
            return jsonify(response_type="ephemeral", text=f"*Watching {len(rows)} page(s) in {where}:*\n" + "\n".join(lines))

        url, interval, opts, flags = parse_command(text)
        if not re.match(r"^https?://", url, re.I):
            return jsonify(response_type="ephemeral",
                text="Usage: `/link <url> <5|30|60|120>m [css=… xpath=… json=… subtract=… extract=… ignore=… trigger=… sort dedupe render]`")

        if cmd == "/unlink":
            n = db().execute("DELETE FROM watches WHERE url=? AND channel=?", (url, channel)).rowcount
            db().commit()
            return jsonify(response_type="ephemeral", text=(f"🗑️ Stopped watching {url} here." if n else f"Not watched here: {url}"))

        # /link
        if interval is None: interval = 60
        if interval not in INTERVALS:
            return jsonify(response_type="ephemeral", text="Interval must be 5m, 30m, 60m, or 120m.")
        cols = dict(url=url, channel=channel, channel_name=channel_name, added_by=user, added_by_id=uid,
                    interval_min=interval, created_at=iso(now()), next_check=iso(now()),
                    **{k: opts[k] for k in OPT_COLS}, **{k: flags[k] for k in FLAG_COLS})
        # upsert by (url, channel) — re-/link updates filters/interval
        existing = db().execute("SELECT id, added_by, added_by_id FROM watches WHERE url=? AND channel=?",
                                (url, channel)).fetchone()
        if existing:
            cols["added_by"] = existing["added_by"] or user; cols["added_by_id"] = existing["added_by_id"] or uid
            sets = ",".join(f"{k}=:{k}" for k in cols)
            db().execute(f"UPDATE watches SET {sets}, hash=NULL, snapshot=NULL WHERE id=:id", {**cols, "id": existing["id"]})
            verb = "🔁 Updated"
        else:
            keys = ",".join(cols); ph = ",".join(f":{k}" for k in cols)
            db().execute(f"INSERT INTO watches({keys}) VALUES({ph})", cols)
            verb = "✅ Watching"
        db().commit()
        where = f"#{channel_name}" if channel_name and channel_name != "directmessage" else "here"
        fbits = [f"{k}=`{opts[k]}`" for k in OPT_COLS if opts[k]] + [k for k in FLAG_COLS if flags[k]]
        extra = ("  ·  " + " ".join(fbits)) if fbits else ""
        return jsonify(response_type="ephemeral", text=f"{verb} <{url}> — every {interval}m, alerts {where}{extra}.")
    except Exception as e:
        return jsonify(response_type="ephemeral", text=f"⚠️ {e}")

# ----------------------------------------------------------------- scheduler
_domain_sems = collections.defaultdict(lambda: threading.Semaphore(PER_DOMAIN))
_domain_lock = threading.Lock()
_render_sem = threading.Semaphore(RENDER_WORKERS)

def _sem_for(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    with _domain_lock:
        return _domain_sems[host]

def check_one(w):
    """Fetch+extract+diff a single watch. Returns (new_hash, new_snapshot, status) or None."""
    url = w["url"]
    try:
        if w["render"]:
            with _render_sem:
                kind, payload = fetch_rendered(url)
        else:
            sem = _sem_for(url)
            with sem:
                kind, payload = fetch(fetch_target(url))
    except Exception as e:
        log_event("ERROR", url, str(e)[:120]); return None
    text = extract(kind, payload, w)
    if kind == "text" and len(text) < 800 and BLOCKED.search(text):
        log_event("BLOCKED", url, ""); return None
    h = hashlib.sha256(text.encode()).hexdigest()
    prev = w["hash"]
    if prev and prev != h:
        # stability re-check
        try:
            if w["render"]:
                with _render_sem: k2, p2 = fetch_rendered(url)
            else:
                with _sem_for(url): k2, p2 = fetch(fetch_target(url))
            if hashlib.sha256(extract(k2, p2, w).encode()).hexdigest() != h:
                log_event("FLAPPED", url, ""); return ("keep", None, None)
        except Exception:
            return ("keep", None, None)
        diff = text_diff(w["snapshot"] or "", text)
        trig = w["trigger"]
        if trig and not any(_re_ok(t, "\n".join(diff)) for t in _as_list(trig)):
            log_event("NOTRIGGER", url, ""); return (h, text, "silent")
        status = alert(dict(w), diff)
        log_event("CHANGED", url, f"by={w['added_by']} ch={w['channel_name'] or w['channel']} via={status}")
        return (h, text, status)
    if not prev:
        log_event("BASELINE", url, f"ch={w['channel_name'] or w['channel']}")
    return (h, text, "baseline")

def _re_ok(pat, s):
    try: return bool(re.search(pat, s, re.I))
    except Exception: return pat.lower() in s.lower()

def scheduler():
    print(f"scheduler up — {WORKERS} workers, {PER_DOMAIN}/domain, {RENDER_WORKERS} renderers")
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    while True:
        try:
            cur = db().execute("SELECT * FROM watches WHERE next_check IS NULL OR next_check<=? "
                               "ORDER BY next_check LIMIT 1000", (iso(now()),)).fetchall()
        except Exception as e:
            print("scheduler query:", e); time.sleep(TICK_SECONDS); continue
        if cur:
            results = list(pool.map(lambda w: (w["id"], w["interval_min"], check_one(dict(w))), cur))
            wconn = db()
            for wid, ivmin, res in results:
                nxt = iso(now() + datetime.timedelta(minutes=ivmin))
                if res is None:                       # fetch error -> back off one interval
                    wconn.execute("UPDATE watches SET last_check=?, next_check=? WHERE id=?", (iso(now()), nxt, wid))
                elif res[0] == "keep":                # flapping -> keep baseline, back off
                    wconn.execute("UPDATE watches SET last_check=?, next_check=? WHERE id=?", (iso(now()), nxt, wid))
                else:
                    h, snap, _ = res
                    wconn.execute("UPDATE watches SET hash=?, snapshot=?, last_check=?, next_check=? WHERE id=?",
                                  (h, (snap or "")[:60000], iso(now()), nxt, wid))
            wconn.commit()
        time.sleep(TICK_SECONDS)

# ----------------------------------------------------------------- main
if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
