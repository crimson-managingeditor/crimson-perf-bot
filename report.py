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
                 f'... on ArticleGQL {{ url title section{{name}} contributors{{name}} text }} }}'
                 for j, (p, y, mo, d, s) in enumerate(ch)]
        data = (gql("{\n" + "\n".join(parts) + "\n}").get("data") or {})
        for j, (p, *_ ) in enumerate(ch):
            v = data.get(f"a{j}")
            if not v: continue
            meta[p] = dict(
                title=v.get("title") or p,
                section=(v.get("section") or {}).get("name") or "?",
                byline=", ".join(c["name"] for c in (v.get("contributors") or []) if c.get("name"))[:80] or "—",
                scoop=bool(scoop_markers(v.get("text"), p)))
    return meta

# ============================================================= helpers
def fmt(n): return f"{int(n):,}"
def secs(n): return f"{int(n//60)}m{int(n%60):02d}s" if n >= 60 else f"{int(n)}s"

def slack_post(blocks, text):
    payload = {"text": text, "blocks": blocks}
    if DRY:
        print("── DRY RUN — would post to Slack ──")
        print(text); print(json.dumps(blocks, indent=1)[:4000]); return
    url = os.environ["SLACK_WEBHOOK_URL"]
    rq = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"})
    urllib.request.urlopen(rq, timeout=30)
    print("posted to Slack")

def url_of(p): return f"https://www.thecrimson.com{p}"

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
                 byline=md.get("byline", "—"), scoop=md.get("scoop", False))
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
    def line(r, tag=""):
        rel = r["screenPageViews"] / med if med else 1
        badge = "🔥" if rel >= 2 else ("✅" if rel >= 1 else "▪️")
        sc = " 🗞️scoop" if r.get("scoop") else ""
        return (f"{badge} <{url_of(r['pagePath'])}|{r['title'][:70]}>{sc}\n"
                f"     {fmt(r['screenPageViews'])} views · {secs(r['eng_per_user'])} read · {r['byline']}")
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Top performers*\n" + "\n".join(line(r) for r in winners)}})
    if duds:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*Underperformed (fresh, well below median)*\n" +
                    "\n".join(line(r) for r in duds)}})
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
    meta = enrich([r["pagePath"] for r in fresh])
    for r in fresh:
        md = meta.get(r["pagePath"], {})
        r.update(section=md.get("section", "?"), title=md.get("title", r["pagePath"]),
                 byline=md.get("byline", "—"), scoop=md.get("scoop", False))
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
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Top stories of the week*\n" + "\n".join(
            f"{i+1}. <{url_of(r['pagePath'])}|{r['title'][:66]}>{' 🗞️' if r['scoop'] else ''} — "
            f"{fmt(r['screenPageViews'])} views, {secs(r['eng_per_user'])} · {r['byline']}"
            for i, r in enumerate(news[:7]))}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "*Sections by readership*\n" + "\n".join(
            f"• {s}: {fmt(v)} views ({n} stories, {fmt(v/max(n,1))}/story)" for s, v, n in sec_rank[:6])}})
    slack_post(blocks, f"Crimson Weekly {start}–{end}")

# =============================================================
if __name__ == "__main__":
    mode = next((a for a in sys.argv[1:] if a in ("daily", "weekly")), "daily")
    (weekly if mode == "weekly" else daily)()
