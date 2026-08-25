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
import os, sys, json, re, datetime, urllib.request, statistics, collections
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

def _sitemap_year(year):
    q = ('{ sitemap(year:%d){ issues{ issue{ issueDate } articles{ title url '
         'section{name} publishAt } } } }' % year)
    d = gql(q)
    out = []
    for i in (((d.get("data") or {}).get("sitemap") or {}).get("issues") or []):
        iss = i.get("issue") or {}
        for a in (i.get("articles") or []):
            pa = (a.get("publishAt") or iss.get("issueDate") or "")[:10]
            out.append(dict(day=pa, title=a.get("title") or "", url=a.get("url") or "",
                            section=(a.get("section") or {}).get("name")))
    return out

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
    today = datetime.datetime.now(TZ).date()
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
    meta = enrich([r["pagePath"] for r in fresh])
    for r in fresh:
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
    # site-wide day-over-year (reuse yesterday's rows; only last year needs a fetch)
    yday_total = sum(r["screenPageViews"] for r in rows)
    yl = yoy_line(pid, creds, y, "yesterday", now=yday_total)
    if yl:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": yl}]})
    def line(r, tag=""):
        rel = r["screenPageViews"] / med if med else 1
        badge = "🔥" if rel >= 2 else ("✅" if rel >= 1 else "▪️")
        sc = " 🗞️scoop" if r.get("scoop") else ""
        pt = f" · 🕒{r['posted']}" if r.get("posted") else ""
        return (f"{badge} <{url_of(r['pagePath'])}|{r['title'][:70]}>{sc}\n"
                f"     {fmt(r['screenPageViews'])} views · {secs(r['eng_per_user'])} read{pt} · {r['byline']}")
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Top performers*\n" + "\n".join(line(r) for r in winners)}})
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
    today = datetime.datetime.now(TZ).date()
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
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Top stories of the week*\n" + "\n".join(
            f"{i+1}. <{url_of(r['pagePath'])}|{r['title'][:66]}>{' 🗞️' if r['scoop'] else ''} — "
            f"{fmt(r['screenPageViews'])} views, {secs(r['eng_per_user'])}"
            f"{' · 🕒'+r['posted'] if r.get('posted') else ''} · {r['byline']}"
            for i, r in enumerate(news[:7]))}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Sections by readership*\n" + "\n".join(
            f"• {s}: {fmt(v)} views ({n} stories, {fmt(v/max(n,1))}/story)" for s, v, n in sec_rank[:6])}})
    cb = corrections_block(today, start.isoformat(), window_days=7)
    if cb:
        blocks.append(cb)
    slack_post(blocks, f"Crimson Weekly {start}–{end}")

# =============================================================
if __name__ == "__main__":
    mode = next((a for a in sys.argv[1:] if a in ("daily", "weekly")), "daily")
    (weekly if mode == "weekly" else daily)()
