#!/usr/bin/env python3
"""Harvard jobs firehose — polls for ALL new Harvard job postings and pings Slack.
(Reporters also get /jobs <keyword> to search and /job <url> to watch a single posting.)

Two sources, because Harvard runs two separate systems and neither knows about the other:
  staff/admin  SmartRecruiters public API  (~313 open)
  academic     academicpositions.harvard.edu Atom feed — faculty, postdocs, research
               fellows, lecturers (~235 open). No key, no pagination, whole list in one
               request; the entry's <author><name> is the school or department.

env:   SLACK_BOT_TOKEN, JOBS_SLACK_CHANNEL (default #job-postings)
state: jobs/seen.json — {"ids": [...]}

  python3 jobsbot.py          # poll + alert on new postings
  python3 jobsbot.py ping     # post a one-line routing test to the jobs channel
"""
import json, os, urllib.request

SR_API = "https://api.smartrecruiters.com/v1/companies/HarvardUniversity/postings"
ACADEMIC_ATOM = "https://academicpositions.harvard.edu/postings/search.atom"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# Academic ids are small integers and SmartRecruiters ids are long opaque strings, but
# they share one state file, so academic ones are namespaced. Staff ids stay bare: a
# change there would make all ~313 look new and blast the channel.
ACADEMIC_PREFIX = "ap:"
UA = "CrimsonNewsroom/1.0 (dhruv.patel@thecrimson.com)"
STATE = os.environ.get("JOBS_STATE", "jobs/seen.json")

# Job postings are their own feed, not an analytics report. Deliberately
# chat.postMessage and not SLACK_WEBHOOK_URL: an incoming webhook is welded to
# the one channel it was created for, so anything using it lands in analytics
# no matter what.
CHANNEL = os.environ.get("JOBS_SLACK_CHANNEL", "#job-postings")

def get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def staff_postings():
    """Every open staff/admin posting, normalised."""
    out, offset = [], 0
    while True:
        d = json.loads(get(f"{SR_API}?limit=100&offset={offset}"))
        c = d.get("content", [])
        out += c
        offset += len(c)
        if len(c) < 100 or offset >= d.get("totalFound", 0):
            break
    norm = []
    for p in out:
        loc = p.get("location", {}) or {}
        norm.append({
            "uid": p["id"],                     # bare, to keep the existing state valid
            "title": (p.get("name") or "").strip(),
            "url": f"https://jobs.smartrecruiters.com/HarvardUniversity/{p['id']}",
            "where": ", ".join(x for x in (loc.get("city"), loc.get("region")) if x),
            "dept": (p.get("department") or {}).get("label", ""),
            "source": "staff",
        })
    return norm

def academic_postings():
    """Every open academic posting, normalised. Faculty, postdocs, fellows, lecturers."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(get(ACADEMIC_ATOM, accept="application/atom+xml"))
    norm = []
    for e in root.findall("a:entry", ATOM_NS):
        url = (e.findtext("a:id", "", ATOM_NS) or "").strip()
        num = url.rstrip("/").rsplit("/", 1)[-1]
        if not num:
            continue
        au = e.find("a:author/a:name", ATOM_NS)
        norm.append({
            "uid": ACADEMIC_PREFIX + num,
            "title": (e.findtext("a:title", "", ATOM_NS) or "").strip(),
            "url": url,
            "where": "",                        # the feed carries no location
            "dept": ((au.text or "").strip() if au is not None else ""),
            "source": "academic",
        })
    return norm

def line(p):
    tail = " · ".join(x for x in (p.get("where"), p.get("dept")) if x)
    return f"• <{p['url']}|{p['title'][:72]}>" + (f" — {tail}" if tail else "")

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

def run():
    try:
        seen = set(json.load(open(STATE)).get("ids", []))
    except Exception:
        seen = set()

    posts, failed = [], []
    for name, fn in (("staff", staff_postings), ("academic", academic_postings)):
        try:
            got = fn()
            posts += got
            print(f"{name}: {len(got)} open")
        except Exception as e:
            failed.append(name)
            print(f"{name}: FETCH FAILED ({e})")

    if not posts:
        print("no sources reachable; leaving state alone"); return

    # Baseline each source the first time it is seen, so adding a source announces
    # its whole backlog to nobody. A source that failed this run is not baselined --
    # otherwise one bad fetch would silently swallow everything it would have alerted.
    fresh = {p["source"] for p in posts}
    known = {"academic" if i.startswith(ACADEMIC_PREFIX) else "staff" for i in seen}
    baseline = (fresh - known) - set(failed)

    new = [p for p in posts if p["uid"] not in seen and p["source"] not in baseline]
    for src in sorted(baseline):
        n = sum(1 for p in posts if p["source"] == src)
        print(f"{src}: first run, baselining {n} posting(s) silently")
    print(f"{len(posts)} postings, {len(new)} new")

    if new:
        order = {"staff": 0, "academic": 1}
        new.sort(key=lambda p: (order.get(p["source"], 9), p["title"].lower()))
        head = f"*\U0001F4BC {len(new)} new Harvard job posting{'s' if len(new) != 1 else ''}*"
        parts, shown = [head], 0
        for src, label in (("staff", "Staff & administrative"), ("academic", "Academic")):
            grp = [p for p in new if p["source"] == src]
            if not grp:
                continue
            room = max(0, 40 - shown)
            if not room:
                break
            parts.append(f"*{label}* ({len(grp)})")
            parts += [line(p) for p in grp[:room]]
            if len(grp) > room:
                parts.append(f"…and {len(grp) - room} more")
            shown += min(len(grp), room)
        slack("\n".join(parts))

    # Only record ids for sources that actually answered, so a failed fetch cannot
    # drop a source's postings out of state and re-announce them all next run.
    ok = {p["source"] for p in posts}
    kept = {i for i in seen
            if ("academic" if i.startswith(ACADEMIC_PREFIX) else "staff") not in ok}
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    json.dump({"ids": sorted(kept | {p["uid"] for p in posts})}, open(STATE, "w"))

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "ping":
        slack("Jobs firehose routing test — new Harvard postings will post here.",
              sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        run()
