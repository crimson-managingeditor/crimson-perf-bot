#!/usr/bin/env python3
"""
Crimson performance bot — daily & weekly Slack reports on article performance.

Data:   GA4 Data API (views + engagement)  +  Crimson GraphQL (title/section/byline/scoop)
Post:   Slack Incoming Webhook
Run:    python report.py daily     # yesterday's winners & duds
        python report.py weekly    # 7-day rollup (run this Mondays)
        add  --dry-run  to print to the console instead of posting to Slack

Config via environment variables (see SETUP.md):
    GA4_PROPERTY_ID          e.g. 123456789   (just the number)
    GA4_CREDENTIALS_JSON     path to the service-account .json  OR the JSON itself
    SLACK_WEBHOOK_URL        https://hooks.slack.com/services/...
    CRIMSON_TZ              (optional) default "America/New_York"
"""
import os, sys, json, re, datetime, urllib.request, urllib.parse, base64, html, hashlib, difflib, unicodedata, statistics, collections
from zoneinfo import ZoneInfo

# Load a .env sitting next to this script, if present (so secrets live in a file,
# not in your shell history). One KEY=value per line. Never commit this file.
_envf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_envf):
    for _l in open(_envf):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

# --- optional scoop detection (reuses the validated definition) ---
try:
    from scoopdef import scoop_markers, clean
except Exception:
    def clean(t): return re.sub(r"<[^>]+>", " ", re.sub(r"\{[^{}]*\}", " ", t or ""))
    def scoop_markers(t, u=None): return []

TZ = ZoneInfo(os.environ.get("CRIMSON_TZ", "America/New_York"))
DRY = "--dry-run" in sys.argv

def now_date():
    """Report date = REPORT_DATE (YYYY-MM-DD) if set, else today in TZ.
    Lets a manual run backfill/test any day; empty on scheduled runs."""
    ov = os.environ.get("REPORT_DATE", "").strip()
    if ov:
        try: return datetime.date.fromisoformat(ov)
        except Exception: pass
    return datetime.datetime.now(TZ).date()
HDR = {"Content-Type": "application/json",
       "User-Agent": "Mozilla/5.0 crimson-bot", "Origin": "https://www.thecrimson.com",
       "Referer": "https://www.thecrimson.com/"}

# ============================================================= GA4
def ga4_rows(property_id, creds, start, end, dims, mets, path_prefix="/article/"):
    """Return list of dicts: one row per page, with the requested dims+metrics."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, Filter, FilterExpression)
    from google.oauth2 import service_account
    info = json.loads(creds) if creds.strip().startswith("{") else json.load(open(creds))
    credentials = service_account.Credentials.from_service_account_info(info)
    client = BetaAnalyticsDataClient(credentials=credentials)
    req = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in mets],
        dimension_filter=FilterExpression(filter=Filter(
            field_name="pagePath",
            string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.BEGINS_WITH,
                                              value=path_prefix))),
        limit=100000)
    resp = client.run_report(req)
    out = []
    for r in resp.rows:
        row = {dims[i]: r.dimension_values[i].value for i in range(len(dims))}
        for i, m in enumerate(mets):
            row[m] = float(r.metric_values[i].value)
        out.append(row)
    return out

def ga4_scalar(pid, creds, start, end, metric, path_prefix="/article/"):
    """One properly-deduplicated total (no dimensions) — needed for totalUsers,
    which must NOT be summed across per-page rows (that double-counts people)."""
    r = ga4_rows(pid, creds, start, end, [], [metric], path_prefix)
    return r[0][metric] if r else 0.0

def fmt_hour(h):
    h = int(h); ap = "am" if h < 12 else "pm"; return f"{h % 12 or 12}{ap}"

def readers_line(pid, creds, day, peak=""):
    """'👤 12,345 readers yesterday · ▲8% vs 11,400 last year · peak 9am'."""
    try:
        now = ga4_scalar(pid, creds, day.isoformat(), day.isoformat(), "totalUsers")
        ly = year_ago(day)
        then = ga4_scalar(pid, creds, ly.isoformat(), ly.isoformat(), "totalUsers")
    except Exception:
        return ""
    if now <= 0:
        return ""
    s = f"👤 {fmt(now)} readers yesterday"
    if then > 0:
        pct = (now - then) / then * 100
        s += f" · {'▲' if pct >= 0 else '▼'}{abs(pct):.0f}% vs {fmt(then)} last year"
    if peak:
        s += f" · {peak}"
    return s

def peak_hour(pid, creds, day):
    """'peak 9am (3,204 views)' for the hour with the most article reading."""
    try:
        rows = ga4_rows(pid, creds, day.isoformat(), day.isoformat(), ["hour"], ["screenPageViews"])
    except Exception:
        return ""
    if not rows:
        return ""
    top = max(rows, key=lambda r: r["screenPageViews"])
    return f"peak {fmt_hour(top['hour'])} ({fmt(top['screenPageViews'])} views)"

def channels_line(pid, creds, day, end=None):
    """'🔗 Search 46% · Direct 27% · Social 18% · Referral 9%' — where readers came from."""
    try:
        rows = ga4_rows(pid, creds, day.isoformat(), (end or day).isoformat(),
                        ["sessionDefaultChannelGroup"], ["screenPageViews"])
    except Exception:
        return ""
    tot = sum(r["screenPageViews"] for r in rows)
    if tot <= 0:
        return ""
    rows.sort(key=lambda r: -r["screenPageViews"])
    parts = [f"{(r['sessionDefaultChannelGroup'] or 'Other')} {r['screenPageViews']/tot*100:.0f}%"
             for r in rows[:4]]
    return "🔗 " + " · ".join(parts)

# ============================================================= Crimson GraphQL
URLDATE = re.compile(r"^/article/(\d+)/(\d+)/(\d+)/([^/]+)/?$")

def pubday_of(path):
    """Publish date from an /article/ path, or None if it isn't a real date.
    Bot/scraper traffic produces junk like /article/20206/8/24/... — skip it,
    don't let datetime.date() raise."""
    m = URLDATE.match(path.rstrip("/") + "/")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None

def gql(q, tries=3):
    for t in range(tries):
        try:
            rq = urllib.request.Request("https://api.thecrimson.com/graphql",
                                        data=json.dumps({"query": q}).encode(), headers=HDR)
            return json.load(urllib.request.urlopen(rq, timeout=60))
        except Exception:
            if t == tries - 1: return {}
    return {}

def enrich(paths):
    """path -> {title, section, byline, day, scoop}. Batched GraphQL."""
    parsed = []
    for p in paths:
        m = URLDATE.match(p.rstrip("/") + "/")
        if m: parsed.append((p,) + m.groups())
    meta = {}
    for i in range(0, len(parsed), 20):
        ch = parsed[i:i+20]
        parts = [f'a{j}: content(year:{y}, month:{mo}, day:{d}, slug:{json.dumps(s)}){{ '
                 f'... on ArticleGQL {{ url title createdOn section{{name}} contributors{{name}} text }} }}'
                 for j, (p, y, mo, d, s) in enumerate(ch)]
        data = (gql("{\n" + "\n".join(parts) + "\n}").get("data") or {})
        for j, (p, *_ ) in enumerate(ch):
            v = data.get(f"a{j}")
            if not v: continue
            meta[p] = dict(
                title=v.get("title") or p,
                section=(v.get("section") or {}).get("name") or "?",
                byline=", ".join(c["name"] for c in (v.get("contributors") or []) if c.get("name"))[:80] or "—",
                posted=pub_time(v.get("createdOn")),
                lede=clean(v.get("text"))[:300],
                scoop=bool(scoop_markers(v.get("text"), p)))
    return meta

def pub_time(created_on):
    """ISO timestamp -> local wall-clock like '8:12 AM', or '' if unknown."""
    if not created_on:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(created_on.replace("Z", "+00:00")).astimezone(TZ)
        return dt.strftime("%-I:%M %p")
    except Exception:
        return ""

# ============================================================= corrections
_MON = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
        r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
CORR_HDR = re.compile(r"\b(Correction|Clarification)s?\s*:\s*(" + _MON + r"\s+\d{1,2},?\s*\d{4})", re.I)
PREV_VER = re.compile(r"\b(?:a previous version|an earlier version|a prior version)\s+of\s+th", re.I)

def iso_et_date(s):
    """ISO timestamp -> 'YYYY-MM-DD' in the report timezone (ET), or '' if unparseable."""
    if not s:
        return ""
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
    except Exception:
        return s[:10]

_SITEMAP_CACHE = {}
def _sitemap_year(year):
    if year in _SITEMAP_CACHE:
        return _SITEMAP_CACHE[year]
    # Date each article by createdOn (actual web publish, in ET). publishAt is null
    # for ~99% of articles, and issueDate is the PRINT date — an evening-ET web story
    # gets a next-morning print date and lands on the wrong day. createdOn is right.
    q = ('{ sitemap(year:%d){ issues{ issue{ issueDate } articles{ title url '
         'section{name} createdOn publishAt } } } }' % year)
    d = gql(q)
    out = []
    for i in (((d.get("data") or {}).get("sitemap") or {}).get("issues") or []):
        iss = i.get("issue") or {}
        for a in (i.get("articles") or []):
            day = (iso_et_date(a.get("createdOn")) or iso_et_date(a.get("publishAt"))
                   or (iss.get("issueDate") or "")[:10])
            out.append(dict(day=day, title=a.get("title") or "", url=a.get("url") or "",
                            section=(a.get("section") or {}).get("name")))
    _SITEMAP_CACHE[year] = out
    return out

def published_by_day(years):
    """Counter of News stories published per YYYY-MM-DD across the given years."""
    c = collections.Counter()
    for y in years:
        for r in _sitemap_year(y):
            if r.get("section") == "News" and r.get("day"):
                c[r["day"]] += 1
    return c

def published_yoy_daily(day):
    """'📝 4 News stories published yesterday · vs 6 a year ago' (or '' if no data)."""
    pub = published_by_day({day.year, day.year - 1})
    ly = year_ago(day)
    n_now, n_then = pub.get(day.isoformat(), 0), pub.get(ly.isoformat(), 0)
    if n_now == 0 and n_then == 0:
        return ""
    return (f"📝 {n_now} News {'story' if n_now == 1 else 'stories'} published yesterday · "
            f"vs {n_then} on {ly.strftime('%b %-d, %Y')}")

def published_yoy_week(start, end):
    """Weekly stories-published count vs the same week last year."""
    pub = published_by_day({start.year, end.year, start.year - 1, end.year - 1})
    def rng(a, b):
        d, tot = a, 0
        while d <= b:
            tot += pub.get(d.isoformat(), 0); d += datetime.timedelta(days=1)
        return tot
    now = rng(start, end)
    then = rng(year_ago(start), year_ago(end))
    if now == 0 and then == 0:
        return ""
    tail = ""
    if then:
        pct = (now - then) / then * 100
        tail = f" · {'▲' if pct >= 0 else '▼'}{abs(pct):.0f}%"
    return f"📝 {now} News stories published this week · vs {then} the same week last year{tail}"

def season_news_urls(start_day):
    """All News article stubs (day/title/url) published on/after start_day."""
    years = sorted({int(start_day[:4]), datetime.datetime.now(TZ).year})
    seen = {}
    for y in years:
        for r in _sitemap_year(y):
            seen[r["url"]] = r
    return [r for r in seen.values()
            if r.get("day", "") >= start_day and r.get("section") == "News"]

def scan_corrections(start_day):
    """Scan this season's News stories for correction/clarification notices.
    Returns list of {url,title,day,corr_date,kind} sorted newest-correction first."""
    arts = season_news_urls(start_day)
    parsed = []
    for r in arts:
        m = URLDATE.match(r["url"].rstrip("/") + "/")
        if m: parsed.append((r,) + m.groups())
    found = []
    for i in range(0, len(parsed), 20):
        ch = parsed[i:i+20]
        parts = [f'a{j}: content(year:{y}, month:{mo}, day:{d}, slug:{json.dumps(s)}){{ '
                 f'... on ArticleGQL {{ text }} }}'
                 for j, (r, y, mo, d, s) in enumerate(ch)]
        data = (gql("{\n" + "\n".join(parts) + "\n}").get("data") or {})
        for j, (r, *_ ) in enumerate(ch):
            v = data.get(f"a{j}")
            if not v: continue
            txt = clean(v.get("text") or "")
            h = CORR_HDR.search(txt)
            if h:
                found.append(dict(url=r["url"], title=r["title"], day=r["day"],
                                  kind=h.group(1).lower(), corr_date=_corr_date(h.group(2))))
            elif PREV_VER.search(txt):
                found.append(dict(url=r["url"], title=r["title"], day=r["day"],
                                  kind="clarification", corr_date=None))
    found.sort(key=lambda x: (x["corr_date"].isoformat() if x["corr_date"] else (x["day"] or "")),
               reverse=True)
    return found

def _corr_date(s):
    """'April 5, 2026' -> date(2026,4,5), or None."""
    for f in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try: return datetime.datetime.strptime(s.replace(",", ", ").replace("  ", " ").strip(), f).date()
        except Exception: pass
    try:
        return datetime.datetime.strptime(re.sub(r"\s+", " ", s.replace(",", "")).strip(), "%B %d %Y").date()
    except Exception:
        return None

# ============================================================= day-over-year
def ga4_total(pid, creds, date_str):
    """Total /article/ views on a single date."""
    rows = ga4_rows(pid, creds, date_str, date_str, ["pagePath"], ["screenPageViews"])
    return sum(r["screenPageViews"] for r in rows)

def year_ago(d):
    """Same calendar date one year earlier (Feb 29 -> Feb 28)."""
    try: return d.replace(year=d.year - 1)
    except ValueError: return d.replace(year=d.year - 1, day=28)

def yoy_line(pid, creds, day, label, now=None):
    """'📈 45,231 article views {label} · ▲12% vs 40,382 a year ago' (or '' on error).
    Pass `now` to reuse an already-computed total for `day` and skip a GA4 round-trip."""
    try:
        if now is None:
            now = ga4_total(pid, creds, day.isoformat())
        ly_day = year_ago(day)
        then = ga4_total(pid, creds, ly_day.isoformat())
        if then <= 0:
            return f"📈 {fmt(now)} article views {label}"
        pct = (now - then) / then * 100
        arrow = "▲" if pct >= 0 else "▼"
        return (f"📈 {fmt(now)} article views {label} · {arrow}{abs(pct):.0f}% "
                f"vs {fmt(then)} on {ly_day.strftime('%b %-d, %Y')}")
    except Exception:
        return ""

# ============================================================= helpers
def fmt(n): return f"{int(n):,}"
def secs(n): return f"{int(n//60)}m{int(n%60):02d}s" if n >= 60 else f"{int(n)}s"

def slack_post(blocks, text):
    payload = {"text": text, "blocks": blocks}
    if DRY:
        print("── DRY RUN — would post to Slack ──")
        print(text); print(json.dumps(blocks, indent=1)); return
    url = os.environ["SLACK_WEBHOOK_URL"]
    rq = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"})
    urllib.request.urlopen(rq, timeout=30)
    print("posted to Slack")

def slack_dm(user_id, blocks, text):
    """DM a Slack user via the bot token (opens the IM, then posts). Returns True
    on success. Needs SLACK_BOT_TOKEN with chat:write + im:write."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not user_id:
        return False
    if DRY:
        print(f"── DRY RUN — would DM {user_id} ──"); print(text); return True
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    def api(method, payload):
        rq = urllib.request.Request(f"https://slack.com/api/{method}",
                                    data=json.dumps(payload).encode(), headers=hdr)
        return json.load(urllib.request.urlopen(rq, timeout=20))
    try:
        opened = api("conversations.open", {"users": user_id})
        ch = (opened.get("channel") or {}).get("id")
        if not ch:
            print("conversations.open failed:", opened.get("error")); return False
        r = api("chat.postMessage", {"channel": ch, "text": text, "blocks": blocks})
        if not r.get("ok"):
            print("chat.postMessage failed:", r.get("error"))
        return bool(r.get("ok"))
    except Exception as e:
        print("slack_dm error:", e); return False

def url_of(p): return f"https://www.thecrimson.com{p}"

def season_start(today):
    """Most recent Dec 13 (masthead turnover) on or before `today`."""
    yr = today.year if (today.month, today.day) >= (12, 13) else today.year - 1
    return datetime.date(yr, 12, 13)

def corrections_block(today, since, window_days):
    """Season-to-date corrections log; highlights any issued in the last window_days."""
    start = season_start(today)
    try:
        corr = scan_corrections(start.isoformat())
    except Exception:
        return None
    recent = [c for c in corr if c.get("corr_date") and (today - c["corr_date"]).days <= window_days]
    head = (f"🛠 *Corrections this season* (since {start.strftime('%b %-d')}): "
            f"*{len(corr)}*" + (f" · {len(recent)} new in the last {window_days}d" if recent else ""))
    lines = [head]
    for c in recent[:6]:
        when = c["corr_date"].strftime("%b %-d") if c.get("corr_date") else "—"
        lines.append(f"   ▪️ {when}: <{url_of(c['url'])}|{c['title'][:64]}> ({c['kind']})")
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}

# ============================================================= DAILY
def daily():
    pid = os.environ["GA4_PROPERTY_ID"]; creds = os.environ["GA4_CREDENTIALS_JSON"]
    today = now_date()
    y = today - datetime.timedelta(days=1)
    ystr = y.isoformat()
    # metrics for yesterday
    rows = ga4_rows(pid, creds, ystr, ystr, ["pagePath"],
                    ["screenPageViews", "userEngagementDuration", "totalUsers"])
    # keep articles PUBLISHED in the last 2 days (fresh work), News only after enrich
    fresh = []
    for r in rows:
        pubday = pubday_of(r["pagePath"])
        if pubday is None: continue
        if (today - pubday).days <= 2 and r["screenPageViews"] >= 25:
            r["eng_per_user"] = r["userEngagementDuration"] / max(r["totalUsers"], 1)
            fresh.append(r)
    if not fresh:
        slack_post([], f"*Crimson Daily* ({ystr}) — no fresh articles with traffic yet."); return
    # #5 still-hot: older stories (published >7d ago) still pulling real traffic yesterday
    still_hot = sorted([r for r in rows
                        if (pubday_of(r["pagePath"]) and (today - pubday_of(r["pagePath"])).days > 7
                            and r["screenPageViews"] >= 300)],
                       key=lambda r: -r["screenPageViews"])[:3]
    meta = enrich([r["pagePath"] for r in fresh] + [r["pagePath"] for r in still_hot])
    for r in fresh + still_hot:
        md = meta.get(r["pagePath"], {})
        r.update(section=md.get("section", "?"), title=md.get("title", r["pagePath"]),
                 byline=md.get("byline", "—"), scoop=md.get("scoop", False),
                 posted=md.get("posted", ""))
    news = [r for r in fresh if r["section"] == "News"] or fresh
    news.sort(key=lambda r: -r["screenPageViews"])
    med = statistics.median([r["screenPageViews"] for r in news])
    winners = news[:5]
    duds = [r for r in news if r["screenPageViews"] < 0.4 * med][-3:]

    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"📊 Crimson Daily — {y.strftime('%A, %b %-d')}"}}]
    tot = sum(r["screenPageViews"] for r in news)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"{len(news)} fresh News stories · {fmt(tot)} views · median {fmt(med)}/story"}]})
    # --- audience context lines ---
    yday_total = sum(r["screenPageViews"] for r in rows)   # reuse yesterday's rows
    for txt in (yoy_line(pid, creds, y, "yesterday", now=yday_total),
                published_yoy_daily(y),
                readers_line(pid, creds, y, peak=peak_hour(pid, creds, y)),   # #3 + #12
                channels_line(pid, creds, y)):               # #2
        if txt:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": txt}]})

    def line(r, tag=""):
        rel = r["screenPageViews"] / med if med else 1
        badge = "🔥" if rel >= 2 else ("✅" if rel >= 1 else "▪️")
        sc = " 🗞️scoop" if r.get("scoop") else ""
        pt = f" · 🕒{r['posted']}" if r.get("posted") else ""
        return (f"{badge} <{url_of(r['pagePath'])}|{r['title'][:70]}>{sc}\n"
                f"     {fmt(r['screenPageViews'])} views · {secs(r['eng_per_user'])} read{pt} · {r['byline']}")
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Top performers*\n" + "\n".join(line(r) for r in winners)}})

    # --- Also notable: milestone (#9), concentration (#4), still-hot (#5) ---
    notable = []
    if news:
        top = news[0]
        tier = next((t for t in (100000, 50000, 25000, 10000) if top["screenPageViews"] >= t), None)
        if tier:
            flag = "🚨" if tier >= 50000 else "🏆"
            notable.append(f"{flag} Story of the day: <{url_of(top['pagePath'])}|{top['title'][:60]}> "
                           f"— {fmt(top['screenPageViews'])} views"
                           + (f" (crossed {tier//1000}k)" if tier >= 50000 else ""))
    if yday_total > 0:
        top3 = sum(r["screenPageViews"] for r in sorted(rows, key=lambda r: -r["screenPageViews"])[:3])
        notable.append(f"🎯 Top 3 stories = {top3/yday_total*100:.0f}% of yesterday's article views")
    for r in still_hot:
        pd = pubday_of(r["pagePath"])
        notable.append(f"♻️ Still hot: <{url_of(r['pagePath'])}|{r['title'][:52]}> "
                       f"({pd.strftime('%b %-d')}) — {fmt(r['screenPageViews'])} views")
    if notable:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Also notable*\n" + "\n".join(notable)}})

    if duds:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*Underperformed (fresh, well below median)*\n" +
                    "\n".join(line(r) for r in duds)}})
    cb = corrections_block(today, ystr, window_days=2)
    if cb:
        blocks.append(cb)
    slack_post(blocks, f"Crimson Daily {ystr}")

# ============================================================= WEEKLY
def weekly():
    pid = os.environ["GA4_PROPERTY_ID"]; creds = os.environ["GA4_CREDENTIALS_JSON"]
    today = now_date()
    start = today - datetime.timedelta(days=7); end = today - datetime.timedelta(days=1)
    rows = ga4_rows(pid, creds, start.isoformat(), end.isoformat(), ["pagePath"],
                    ["screenPageViews", "userEngagementDuration", "totalUsers"])
    fresh = []
    for r in rows:
        pubday = pubday_of(r["pagePath"])
        if pubday is None: continue
        if start <= pubday <= end and r["screenPageViews"] >= 25:
            r["eng_per_user"] = r["userEngagementDuration"] / max(r["totalUsers"], 1)
            fresh.append(r)
    if not fresh:
        slack_post([], "*Crimson Weekly* — no data."); return
    week_total = sum(r["screenPageViews"] for r in rows)   # all article views this week
    meta = enrich([r["pagePath"] for r in fresh])
    for r in fresh:
        md = meta.get(r["pagePath"], {})
        r.update(section=md.get("section", "?"), title=md.get("title", r["pagePath"]),
                 byline=md.get("byline", "—"), scoop=md.get("scoop", False),
                 posted=md.get("posted", ""))
    news = [r for r in fresh if r["section"] == "News"]
    news.sort(key=lambda r: -r["screenPageViews"])
    tot = sum(r["screenPageViews"] for r in news)
    scoops = [r for r in news if r["scoop"]]
    bysec = collections.defaultdict(list)
    for r in fresh: bysec[r["section"]].append(r["screenPageViews"])
    sec_rank = sorted(((s, sum(v), len(v)) for s, v in bysec.items()), key=lambda x: -x[1])

    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"🗞️ Crimson Weekly — {start.strftime('%b %-d')}–{end.strftime('%b %-d')}"}}]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"{len(news)} News stories · {fmt(tot)} views · "
                f"{len(scoops)} scoops (median {fmt(statistics.median([r['screenPageViews'] for r in scoops])) if scoops else 0} views)"}]})
    # week-over-year across all article traffic
    try:
        ly_rows = ga4_rows(pid, creds, year_ago(start).isoformat(), year_ago(end).isoformat(),
                           ["pagePath"], ["screenPageViews"])
        ly_total = sum(r["screenPageViews"] for r in ly_rows)
        if ly_total > 0:
            pct = (week_total - ly_total) / ly_total * 100
            arrow = "▲" if pct >= 0 else "▼"
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f"📈 {fmt(week_total)} article views this week · {arrow}{abs(pct):.0f}% "
                        f"vs {fmt(ly_total)} the same week last year"}]})
    except Exception:
        pass
    pl = published_yoy_week(start, end)
    if pl:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": pl}]})
    # #3 unique readers this week (deduplicated) vs same week last year
    try:
        u_now = ga4_scalar(pid, creds, start.isoformat(), end.isoformat(), "totalUsers")
        u_then = ga4_scalar(pid, creds, year_ago(start).isoformat(), year_ago(end).isoformat(), "totalUsers")
        if u_now > 0:
            s = f"👤 {fmt(u_now)} readers this week"
            if u_then > 0:
                p = (u_now - u_then) / u_then * 100
                s += f" · {'▲' if p >= 0 else '▼'}{abs(p):.0f}% vs {fmt(u_then)} last year"
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": s}]})
    except Exception:
        pass
    ch = channels_line(pid, creds, start, end)               # #2 over the week
    if ch:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ch}]})
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Top stories of the week*\n" + "\n".join(
            f"{i+1}. <{url_of(r['pagePath'])}|{r['title'][:66]}>{' 🗞️' if r['scoop'] else ''} — "
            f"{fmt(r['screenPageViews'])} views, {secs(r['eng_per_user'])}"
            f"{' · 🕒'+r['posted'] if r.get('posted') else ''} · {r['byline']}"
            for i, r in enumerate(news[:7]))}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Sections by readership*\n" + "\n".join(
            f"• {s}: {fmt(v)} views ({n} stories, {fmt(v/max(n,1))}/story)" for s, v, n in sec_rank[:6])}})
    # --- Also notable: milestone (#9), concentration (#4), still-hot (#5) ---
    notable = []
    if news:
        top = news[0]
        tier = next((t for t in (100000, 50000, 25000) if top["screenPageViews"] >= t), None)
        if tier:
            flag = "🚨" if tier >= 50000 else "🏆"
            notable.append(f"{flag} Biggest story: <{url_of(top['pagePath'])}|{top['title'][:58]}> "
                           f"— {fmt(top['screenPageViews'])} views")
    if week_total > 0:
        top3 = sum(r["screenPageViews"] for r in sorted(rows, key=lambda r: -r["screenPageViews"])[:3])
        notable.append(f"🎯 Top 3 stories = {top3/week_total*100:.0f}% of the week's article views")
    sh = sorted([r for r in rows
                 if (pubday_of(r["pagePath"]) and pubday_of(r["pagePath"]) < start
                     and r["screenPageViews"] >= 1000)],
                key=lambda r: -r["screenPageViews"])[:3]
    if sh:
        shmeta = enrich([r["pagePath"] for r in sh])
        for r in sh:
            pd = pubday_of(r["pagePath"]); t = shmeta.get(r["pagePath"], {}).get("title", r["pagePath"])
            notable.append(f"♻️ Still hot: <{url_of(r['pagePath'])}|{t[:50]}> "
                           f"({pd.strftime('%b %-d')}) — {fmt(r['screenPageViews'])} views")
    if notable:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Also notable*\n" + "\n".join(notable)}})
    cb = corrections_block(today, start.isoformat(), window_days=7)
    if cb:
        blocks.append(cb)
    slack_post(blocks, f"Crimson Weekly {start}–{end}")

# ============================================================= INSTAGRAM
# Instagram Graph API (Meta). Auth = a Business-Manager System User token that
# never expires. Reads @theharvardcrimson insights via the linked Facebook Page.
#   IG_ACCESS_TOKEN   system-user token with instagram_manage_insights + pages_*
#   IG_USER_ID        the IG Business Account id (numeric)
IG_BASE = "https://graph.facebook.com/v21.0"

def ig_get(path, **p):
    p["access_token"] = os.environ["IG_ACCESS_TOKEN"]
    url = f"{IG_BASE}/{path}?" + urllib.parse.urlencode(p)
    try:
        return json.load(urllib.request.urlopen(url, timeout=90))
    except urllib.error.HTTPError as e:
        try: return {"error": json.loads(e.read().decode()).get("error", {}).get("message", "")}
        except Exception: return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}

def ig_total(igid, metric, since, until):
    """One total_value account metric summed over [since, until] (unix). None on error."""
    r = ig_get(f"{igid}/insights", metric=metric, period="day",
               metric_type="total_value", since=since, until=until)
    try: return int(r["data"][0]["total_value"]["value"])
    except Exception: return None

def ig_totals(igid, metrics, since, until):
    return {m: ig_total(igid, m, since, until) for m in metrics}

def ig_follower_delta(igid, since, until):
    """Net new followers over the window (sum of the daily follower_count series)."""
    r = ig_get(f"{igid}/insights", metric="follower_count", period="day", since=since, until=until)
    try: return sum(v["value"] for v in r["data"][0]["values"])
    except Exception: return None

def _ts(iso):
    try: return int(datetime.datetime.fromisoformat(iso.replace("+0000", "+00:00")).timestamp())
    except Exception: return 0

def ig_media_between(igid, since, until):
    """Posts whose timestamp is in [since, until) (unix), newest first, with per-post insights."""
    out, url = [], f"{IG_BASE}/{igid}/media"
    params = dict(fields="id,caption,timestamp,media_type,media_product_type,permalink,"
                         "like_count,comments_count", limit=50, access_token=os.environ["IG_ACCESS_TOKEN"])
    for _ in range(20):  # page guard
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        try: r = json.load(urllib.request.urlopen(full, timeout=90))
        except Exception: break
        data = r.get("data", [])
        for m in data:
            m["_ts"] = _ts(m.get("timestamp", ""))
        out += data
        if data and data[-1]["_ts"] < since:  # gone past the window
            break
        nxt = (r.get("paging") or {}).get("next")
        if not nxt: break
        url, params = nxt, None
    inwin = [m for m in out if since <= m["_ts"] < until]
    for m in inwin:
        reel = m.get("media_product_type") == "REELS"
        mset = "reach,saved,shares,total_interactions" + (",views" if reel else "")
        d = ig_get(f"{m['id']}/insights", metric=mset)
        m["_ins"] = {x["name"]: (x.get("values") or [{}])[0].get("value") for x in d.get("data", [])}
    return inwin

def _ig_flag(reach, avg):
    """🔥 well above the 30-day norm · ⚠️ below it (low performer) · ✅ in range."""
    if avg and reach is not None:
        if reach >= 1.5 * avg: return "🔥"
        if reach < avg:        return "⚠️"
    return "✅"

def _ig_post_line(m, avg=None, rank=None):
    cap = clean(m.get("caption") or "").strip().replace("\n", " ")[:56] or "(no caption)"
    ins = m.get("_ins", {})
    reach = ins.get("reach"); saves = ins.get("saved"); shares = ins.get("shares")
    tag = f"{rank}." if rank else _ig_flag(reach, avg)
    d = datetime.datetime.fromtimestamp(m["_ts"], TZ).strftime("%b %-d") if m.get("_ts") else ""
    parts = [f"{fmt(reach)} reached"] if reach is not None else []
    parts.append(f"❤️{fmt(m.get('like_count') or 0)}")
    if m.get("comments_count"): parts.append(f"💬{fmt(m['comments_count'])}")
    if saves: parts.append(f"🔖{fmt(saves)}")
    if shares: parts.append(f"↗️{fmt(shares)}")
    return f"{tag} <{m.get('permalink','')}|{cap}> · {d}\n     " + " · ".join(parts)

def _ig_month(igid, until):
    """Last 30 days of posts (with per-post insights) + the average reach/post."""
    month = ig_media_between(igid, until - 30 * 86400, until)
    reaches = [m["_ins"]["reach"] for m in month
               if m.get("_ins", {}).get("reach") is not None]
    avg = int(statistics.mean(reaches)) if reaches else 0
    return month, avg, len(reaches)

def _ig_header_stats(igid):
    a = ig_get(igid, fields="username,followers_count,media_count")
    return a.get("username", "theharvardcrimson"), a.get("followers_count") or 0

def instagram_daily():
    igid = os.environ["IG_USER_ID"]
    today = now_date()
    y = today - datetime.timedelta(days=1)
    since = int(datetime.datetime.combine(y, datetime.time(), TZ).timestamp())
    until = int(datetime.datetime.combine(today, datetime.time(), TZ).timestamp())
    user, followers = _ig_header_stats(igid)
    delta = ig_follower_delta(igid, since, until)
    t = ig_totals(igid, ["reach", "views", "profile_views", "website_clicks"], since, until)
    month, avg, npost = _ig_month(igid, until)

    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"📸 Instagram — {y.strftime('%A, %b %-d')}"}}]
    fol = f"👥 {fmt(followers)} followers"
    if delta is not None:
        fol += f" · {'▲' if delta >= 0 else '▼'}{fmt(abs(delta))} yesterday"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": fol}]})
    reach_bits = []
    if t.get("reach") is not None: reach_bits.append(f"👁 {fmt(t['reach'])} reached")
    if t.get("views") is not None: reach_bits.append(f"👀 {fmt(t['views'])} views")
    if t.get("profile_views"): reach_bits.append(f"🪧 {fmt(t['profile_views'])} profile visits")
    if t.get("website_clicks"): reach_bits.append(f"🔗 {fmt(t['website_clicks'])} link taps")
    if reach_bits:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(reach_bits)}]})

    # post-by-post: yesterday's posts only, flagged vs the 30-day average;
    # count compared to the same weekday one week earlier.
    yposts = sorted([m for m in month if since <= m["_ts"] < until],
                    key=lambda m: -(m.get("_ins", {}).get("reach") or 0))
    lw = y - datetime.timedelta(days=7)
    lw_since, lw_until = since - 7 * 86400, until - 7 * 86400
    lw_count = sum(1 for m in month if lw_since <= m["_ts"] < lw_until)
    hdr = f"*Posts yesterday: {len(yposts)}* (vs {lw_count} on {lw.strftime('%a %b %-d')})"
    if yposts:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": hdr + "\n" + "\n".join(_ig_post_line(m, avg) for m in yposts)}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"📅 30-day average: {fmt(avg)} reach/post across {npost} posts · "
                    f"🔥 ≥1.5× avg · ⚠️ below avg (low performer)"}]})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": hdr}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"📅 30-day average: {fmt(avg)} reach/post across {npost} posts"}]})
    slack_post(blocks, f"Instagram daily {y}")

def instagram_weekly():
    igid = os.environ["IG_USER_ID"]
    today = now_date()
    start = today - datetime.timedelta(days=7); end = today - datetime.timedelta(days=1)
    since = int(datetime.datetime.combine(start, datetime.time(), TZ).timestamp())
    until = int(datetime.datetime.combine(today, datetime.time(), TZ).timestamp())
    user, followers = _ig_header_stats(igid)
    delta = ig_follower_delta(igid, since, until)
    prev = ig_follower_delta(igid, since - 7 * 86400, since)  # week before
    t = ig_totals(igid, ["reach", "views", "accounts_engaged", "total_interactions",
                         "likes", "comments", "saves", "shares", "website_clicks"], since, until)
    preach = ig_total(igid, "reach", since - 7 * 86400, since)
    month, avg, npost = _ig_month(igid, until)

    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"📸 Instagram Weekly — {start.strftime('%b %-d')}–{end.strftime('%b %-d')}"}}]
    fol = f"👥 {fmt(followers)} followers"
    if delta is not None:
        fol += f" · {'▲' if delta >= 0 else '▼'}{fmt(abs(delta))} this week"
        if prev:
            fol += f" (vs {'▲' if prev >= 0 else '▼'}{fmt(abs(prev))} last week)"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": fol}]})
    rb = []
    if t.get("reach") is not None:
        s = f"👁 {fmt(t['reach'])} reached"
        if preach:
            p = (t["reach"] - preach) / preach * 100
            s += f" ({'▲' if p >= 0 else '▼'}{abs(p):.0f}% wow)"
        rb.append(s)
    if t.get("views") is not None: rb.append(f"👀 {fmt(t['views'])} views")
    if t.get("accounts_engaged"): rb.append(f"🤝 {fmt(t['accounts_engaged'])} engaged")
    if rb:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(rb)}]})
    if t.get("total_interactions") is not None:
        eng = (f"📊 {fmt(t['total_interactions'])} interactions — ❤️{fmt(t.get('likes') or 0)} "
               f"💬{fmt(t.get('comments') or 0)} 🔖{fmt(t.get('saves') or 0)} ↗️{fmt(t.get('shares') or 0)}")
        if t.get("website_clicks"): eng += f" · 🔗{fmt(t['website_clicks'])} link taps"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": eng}]})

    # posts this week vs last week + full post-by-post breakdown (best reach first)
    this_week = sorted([m for m in month if since <= m["_ts"] < until],
                       key=lambda m: -(m.get("_ins", {}).get("reach") or 0))
    last_week_n = sum(1 for m in month if since - 7 * 86400 <= m["_ts"] < since)
    if this_week:
        wow = f" (vs {last_week_n} last week)"
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Posts this week: {len(this_week)}{wow}*\n" +
                    "\n".join(_ig_post_line(m, avg) for m in this_week[:12])}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"median {fmt(statistics.median([m.get('_ins',{}).get('reach') or 0 for m in this_week]))} "
                    f"reach/post · 30-day avg {fmt(avg)} across {npost} posts · 🔥 ≥1.5× · ⚠️ below avg"}]})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Posts this week: 0* (vs {last_week_n} last week)"}})
    slack_post(blocks, f"Instagram weekly {start}–{end}")

# ============================================================= MAILCHIMP
# READ-ONLY. This integration must NEVER send, create, or modify anything — only
# GET. The API key is full-access (Mailchimp has no read-only keys), so the code
# itself is the guardrail: mc_get() issues GET requests and nothing else.
#   MAILCHIMP_API_KEY   e.g. "…-us6"  (the suffix after '-' is the data center)
def mc_get(path):
    """GET only. Returns parsed JSON, or {} on any error."""
    key = os.environ["MAILCHIMP_API_KEY"]; dc = key.split("-")[-1]
    h = {"Authorization": "Basic " + base64.b64encode(f"crimson:{key}".encode()).decode()}
    req = urllib.request.Request(f"https://{dc}.api.mailchimp.com/3.0{path}", headers=h)  # GET
    try: return json.load(urllib.request.urlopen(req, timeout=60))
    except Exception: return {}

def _mc_et(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)

def mailchimp_daily():
    today = now_date()
    y = today - datetime.timedelta(days=1)
    since = datetime.datetime.combine(y, datetime.time(), TZ).isoformat()
    before = datetime.datetime.combine(today, datetime.time(), TZ).isoformat()
    q = ("/campaigns?status=sent&count=100&sort_field=send_time&sort_dir=DESC"
         f"&since_send_time={urllib.parse.quote(since)}&before_send_time={urllib.parse.quote(before)}"
         "&fields=campaigns.id,campaigns.send_time,campaigns.emails_sent,"
         "campaigns.settings.title,campaigns.settings.subject_line")
    camps = [c for c in mc_get(q).get("campaigns", [])
             if c.get("send_time") and _mc_et(c["send_time"]).date() == y]

    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"📧 Newsletters — {y.strftime('%A, %b %-d')}"}}]
    if not camps:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": "No newsletter sent yesterday."}]})
        slack_post(blocks, f"Mailchimp daily {y}"); return

    camps.sort(key=lambda c: c["send_time"])  # chronological
    for c in camps:
        rep = mc_get(f"/reports/{c['id']}?fields=opens,clicks,emails_sent")
        op = rep.get("opens", {}); cl = rep.get("clicks", {}); s = c.get("settings", {})
        name = s.get("subject_line") or s.get("title") or "(untitled)"
        sent = _mc_et(c["send_time"]).strftime("%-I:%M %p").lstrip("0")
        recips = c.get("emails_sent") or rep.get("emails_sent") or 0
        opens = op.get("proxy_excluded_unique_opens", op.get("unique_opens")) or 0
        orate = op.get("proxy_excluded_open_rate", op.get("open_rate")) or 0
        clicks = cl.get("unique_subscriber_clicks") or 0
        crate = cl.get("click_rate") or 0
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": (f"*{name[:90]}*\n"
                     f"🕐 Sent {sent} ET · 👥 {fmt(recips)} recipients\n"
                     f"📬 {fmt(opens)} opens ({orate*100:.0f}%) · "
                     f"🖱️ {fmt(clicks)} clicks ({crate*100:.1f}%)")}})
    slack_post(blocks, f"Mailchimp daily {y}")

# ============================================================= STATE HELPERS
# Tiny JSON state persisted between runs via the GitHub Actions cache (path set
# by the STATE_FILE env). Used by the page watcher for its per-URL snapshots.
def _state_path(): return os.environ.get("STATE_FILE", "state/state.json")
def _load_state():
    try: return json.load(open(_state_path()))
    except Exception: return {}
def _save_state(st):
    p = _state_path(); os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    try: json.dump(st, open(p, "w"))
    except Exception: pass

# ============================================================= SOCIAL GAP
# Stories crushing it on-site that never made it to Instagram → "share these".
# Match is by content, not URL (IG captions carry no link): a story counts as
# "already posted" if a recent IG caption's words are mostly found in the story's
# headline+lede (captions are usually drawn from the lede).
_STOP = set(("the a an and or of to in on for with at by from as is are was were be "
             "been this that these those it its into over after before amid about his "
             "her their they them has have had will would could new news student students "
             "university college harvard crimson said says year years first").split())

def _tok(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 4 and w not in _STOP}

def social_gap():
    pid = os.environ["GA4_PROPERTY_ID"]; creds = os.environ["GA4_CREDENTIALS_JSON"]
    igid = os.environ["IG_USER_ID"]
    floor = int(os.environ.get("SOCIAL_GAP_MIN_VIEWS", "1500"))
    today = now_date()
    start = today - datetime.timedelta(days=3); end = today - datetime.timedelta(days=1)
    rows = ga4_rows(pid, creds, start.isoformat(), end.isoformat(), ["pagePath"], ["screenPageViews"])
    cand = [r for r in rows
            if pubday_of(r["pagePath"]) and (today - pubday_of(r["pagePath"])).days <= 4
            and r["screenPageViews"] >= floor]
    if not cand:
        print("no candidate stories"); return
    cand.sort(key=lambda r: -r["screenPageViews"]); cand = cand[:15]
    meta = enrich([r["pagePath"] for r in cand])
    # recent IG captions -> token sets
    until = int(datetime.datetime.combine(today, datetime.time(), TZ).timestamp())
    try:
        media = ig_media_between(igid, until - 7 * 86400, until)
    except Exception as e:
        print("IG fetch failed:", e); return
    caps = [_tok(m.get("caption")) for m in media if m.get("caption")]

    def posted(fingerprint):
        for ct in caps:
            if ct and len(ct & fingerprint) / len(ct) >= 0.6:  # caption words mostly in the story
                return True
        return False

    gaps = []
    for r in cand:
        md = meta.get(r["pagePath"], {})
        fp = _tok((md.get("title") or "") + " " + (md.get("lede") or ""))
        if fp and not posted(fp):
            gaps.append((r, md))
    gaps = gaps[:5]
    if not gaps:
        print("no social gaps — top stories are on IG"); return

    lines = [f"• <{url_of(r['pagePath'])}|{(md.get('title') or r['pagePath'])[:66]}> — "
             f"{fmt(r['screenPageViews'])} views · {md.get('byline','—')}" for r, md in gaps]
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "🔗 Share these on Instagram"}},
              {"type": "context", "elements": [{"type": "mrkdwn",
               "text": "Crushing it on-site, not yet on @theharvardcrimson (last 3 days)"}]},
              {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    slack_post(blocks, "Social gap — share these on Instagram")

# ============================================================= PAGE WATCH
# Watches a curated list of URLs and alerts Slack when a page's MAIN TEXT changes
# (scripts/nav/boilerplate stripped, so ad/timestamp churn doesn't fire). Reporters
# add links via the /watch slash command (writes watch/watchlist.json); this engine
# reads that list. Snapshots persist between runs in the Actions cache.
#   WATCHLIST_FILE   default watch/watchlist.json  — [ "url" | {url,label,added_by} ]
#   STATE_FILE       default watch/state.json      — {url: {hash, text}}
def _load_watchlist():
    p = os.environ.get("WATCHLIST_FILE", "watch/watchlist.json")
    try: data = json.load(open(p))
    except Exception: return []
    out = []
    for e in (data if isinstance(data, list) else []):
        if isinstance(e, str): out.append({"url": e})
        elif isinstance(e, dict) and e.get("url"): out.append(e)
    return out

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def _fetch_target(url):
    """Rewrite common 'view' URLs to a server-rendered form a plain fetch can read.
    All are link-shared / public only — no authentication is performed."""
    # Google Docs / Sheets / Slides -> text/csv export
    m = re.match(r"https?://docs\.google\.com/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]+)", url)
    if m:
        kind = m.group(1)
        return f"https://docs.google.com/{kind}/d/{m.group(2)}/export?format=" + ("csv" if kind == "spreadsheets" else "txt")
    # GitHub blob page -> raw file
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    # Dropbox share -> direct content
    if "dropbox.com/" in url:
        if "dl=0" in url: return url.replace("dl=0", "dl=1")
        if "dl=1" not in url and "raw=1" not in url:
            return url + ("&dl=1" if "?" in url else "?dl=1")
    return url

def _fetch(url, timeout=25, depth=0):
    """Fetch robustly -> ('text', str) or ('bytes', bytes). Handles a browser UA,
    gzip/deflate, charset detection, non-HTML content, meta-refresh redirects, and
    (only if the cert fails) a relaxed-TLS retry — acceptable since we only READ."""
    import gzip, zlib, ssl
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate"})
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
    if depth < 2:   # follow a <meta http-equiv=refresh> bounce (common on gov sites)
        mr = re.search(r'(?is)<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']?\s*\d+\s*;\s*url=([^"\'>\s]+)', text[:4096])
        if mr:
            return _fetch(urllib.parse.urljoin(url, html.unescape(mr.group(1))), timeout, depth + 1)
    return "text", text

_BLOCKED = re.compile(r"(enable javascript|checking your browser|verify you are (a )?human|"
                      r"attention required|access denied|just a moment|cf-browser-verification|"
                      r"are you a robot|complete the security check|unusual traffic)", re.I)

def _normalize_json(s):
    try:
        return "JSON:\n" + json.dumps(json.loads(s), indent=2, sort_keys=True, ensure_ascii=False)
    except Exception:
        return None

def _feed_text(s):
    """RSS/Atom/sitemap -> one line per item (title + link), so NEW items diff cleanly."""
    if not re.search(r"<(rss|feed|rdf:RDF|urlset|sitemapindex)\b", s[:3000], re.I):
        return None
    out = []
    for it in re.findall(r"(?is)<(?:item|entry|url)\b.*?</(?:item|entry|url)>", s)[:200]:
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", it)
        loc = re.search(r"(?is)<(?:loc|link)[^>]*>(.*?)</(?:loc|link)>", it) \
              or re.search(r'(?is)<link[^>]+href=["\']([^"\']+)', it)
        t = clean(title.group(1)) if title else ""
        l = clean(loc.group(1)) if loc else ""
        if t or l: out.append(f"• {t} {l}".strip())
    return "\n".join(out) if out else None

def _page_text(h):
    """Extract readable, STRUCTURE-PRESERVING text so diffs are legible and
    change-sensitive: JSON is normalized, feeds list their items, and HTML keeps
    tables (rows/cells), lists, and headings as separate lines instead of one blob."""
    if not h:
        return ""
    t = h.lstrip("﻿")                       # strip BOM
    if t.lstrip()[:1] in "{[":                   # JSON API response
        j = _normalize_json(t)
        if j is not None: return j
    feed = _feed_text(t)                          # RSS / Atom / sitemap
    if feed is not None: return feed

    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", t)
    title = clean(title_m.group(1)) if title_m else ""
    s = t
    # drop non-content blocks entirely
    s = re.sub(r"(?is)<(script|style|noscript|svg|template|iframe|object|embed|canvas|form|button|select)\b.*?</\1>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?is)<(nav|header|footer|aside)\b.*?</\1>", " ", s)
    # focus on the real content if the page marks it
    mm = re.search(r"(?is)<main\b[^>]*>(.*?)</main>", s) or re.search(r"(?is)<article\b[^>]*>(.*?)</article>", s)
    if mm: s = mm.group(1)
    # structure -> newlines (tables become rows of "cell | cell", lists get bullets)
    s = re.sub(r"(?is)<(th|td)\b[^>]*>", " | ", s)
    s = re.sub(r"(?is)<tr\b[^>]*>", "\n", s)
    s = re.sub(r"(?is)<li\b[^>]*>", "\n• ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)<h[1-6]\b[^>]*>", "\n\n", s)
    s = re.sub(r"(?is)</(p|div|h[1-6]|section|article|tr|ul|ol|dl|dd|dt|blockquote|pre|table|thead|tbody|caption)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)            # drop any remaining tags
    s = unicodedata.normalize("NFC", html.unescape(s))
    s = re.sub(r"[ \t\f\v]+", " ", s)             # collapse intra-line spaces (keep newlines)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"(?m)^\|\s*", "", s)              # drop the leading cell separator on table rows
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return (f"[title] {title}\n{s}" if title else s)

def _text_diff(old, new, n=14):
    o = re.split(r"(?<=[.!?])\s+", old); m = re.split(r"(?<=[.!?])\s+", new)
    d = [l for l in difflib.unified_diff(o, m, lineterm="")
         if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return d[:n]

def _watch_log(line):
    """Append an audit line to the repo-committed change log (if WATCH_LOG is set)."""
    p = os.environ.get("WATCH_LOG", "")
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _watch_alert(entry, diff):
    label = entry.get("label") or entry["url"]
    who = f"  ·  _flagged by {entry['added_by']}_" if entry.get("added_by") else ""
    body = "\n".join(diff)[:2600] or "(main text changed — no line-level diff)"
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
               "text": f"🔎 *Page changed* — <{entry['url']}|{label[:80]}>{who}"}},
              {"type": "section", "text": {"type": "mrkdwn", "text": "```\n" + body + "\n```"}}]
    text = f"Page changed: {label[:60]}"
    # DM whoever flagged the page (private); fall back to the webhook channel if we
    # don't have their id or no bot token is configured. Returns the delivery result.
    if entry.get("added_by_id") and slack_dm(entry["added_by_id"], blocks, text):
        print(f"DMed {entry.get('added_by','?')} about {entry['url']}")
        return f"DM->{entry.get('added_by','?')}"
    slack_post(blocks, text)
    return "channel"

WATCH_INTERVALS = (5, 30, 60, 120)   # allowed per-URL check cadences (minutes)

def _interval_min(entry):
    try: iv = int(entry.get("interval", 120))
    except Exception: iv = 120
    return iv if iv in WATCH_INTERVALS else 120

def _due(prev, interval_min, now):
    """Is this URL due for a check? True if never seen, or its interval has elapsed
    (90s grace so a slightly-late run doesn't skip a tier)."""
    if not prev or not prev.get("last"): return True
    try:
        last = datetime.datetime.fromisoformat(prev["last"])
    except Exception:
        return True
    return (now - last).total_seconds() >= interval_min * 60 - 90

def watch():
    import concurrent.futures as cf
    wl = _load_watchlist()
    if not wl:
        print("watchlist empty — nothing to check"); return
    snaps = _load_state()  # {url: {hash, text, last}}
    now = datetime.datetime.now(datetime.timezone.utc); nowiso = now.isoformat()
    # only fetch the URLs whose cadence is due this run (5-min tier every run, etc.)
    due = [e for e in wl if _due(snaps.get(e["url"]), _interval_min(e), now)]
    if not due:
        print(f"0 of {len(wl)} due this run"); return

    def check(entry):
        url = entry["url"]
        try:
            kind, payload = _fetch(_fetch_target(url))
            if kind == "text":
                text = _page_text(payload)
                if len(text) < 800 and _BLOCKED.search(text):   # bot wall / JS challenge
                    return url, "error", "blocked/challenge page (not real content)"
            else:
                text = f"[binary file] sha256:{hashlib.sha256(payload).hexdigest()}"
            return url, "ok", text
        except Exception as e:
            return url, "error", str(e)

    workers = int(os.environ.get("WATCH_WORKERS", "20"))  # concurrency (politeness cap)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        fetched = list(ex.map(check, due))
    results = {u: (st, payload) for (u, st, payload) in fetched}

    changed = 0
    for entry in due:
        url = entry["url"]; st, payload = results.get(url, ("error", "no result"))
        if st != "ok":                       # back off failures until next interval
            print(f"skip {url}: {payload}")
            _watch_log(f"{nowiso}  ERROR    {url}  {str(payload)[:90]}")
            snaps[url] = {**(snaps.get(url) or {}), "last": nowiso}; continue
        text = payload; h = hashlib.sha256(text.encode()).hexdigest()
        prev = snaps.get(url)
        if prev and prev.get("hash") and prev["hash"] != h:
            # confirm the change is stable — one re-fetch kills A/B-test flapping and
            # transient blips; only alert if the same new content comes back.
            try:
                k2, p2 = _fetch(_fetch_target(url))
                t2 = _page_text(p2) if k2 == "text" else f"[binary file] sha256:{hashlib.sha256(p2).hexdigest()}"
                stable = hashlib.sha256(t2.encode()).hexdigest() == h
            except Exception:
                stable = False
            if not stable:
                print(f"unstable/flapping change on {url} — not alerting")
                _watch_log(f"{nowiso}  FLAPPED  {url}  (change not stable — held, not alerted)")
                snaps[url] = {**prev, "last": nowiso}   # keep baseline, back off to next interval
                continue
            status = _watch_alert(entry, _text_diff(prev.get("text", ""), text)); changed += 1
            _watch_log(f"{nowiso}  CHANGED  {url}  flagged_by={entry.get('added_by','?')}  alert={status}")
        elif not prev:
            print(f"baseline set: {url}")     # first sighting, no alert
            _watch_log(f"{nowiso}  BASELINE {url}")
        snaps[url] = {"hash": h, "text": text[:40000], "last": nowiso}
    _save_state(snaps)
    print(f"due {len(due)}/{len(wl)} pages, {changed} changed")

# =============================================================
if __name__ == "__main__":
    modes = {"daily": daily, "weekly": weekly,
             "ig-daily": instagram_daily, "ig-weekly": instagram_weekly,
             "mc-daily": mailchimp_daily, "social-gap": social_gap, "watch": watch}
    mode = next((a for a in sys.argv[1:] if a in modes), "daily")
    modes[mode]()
