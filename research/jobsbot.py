#!/usr/bin/env python3
"""Harvard jobs firehose — polls the SmartRecruiters public postings API for ALL new
Harvard job postings and pings Slack. (Reporters also get /jobs <keyword> to search and
/job <url> to watch a single posting page.)

env:   SLACK_WEBHOOK_URL (where the firehose posts)
state: jobs/seen.json — {"ids": [...]}
"""
import json, os, urllib.request

API = "https://api.smartrecruiters.com/v1/companies/HarvardUniversity/postings"
UA = "CrimsonNewsroom/1.0 (dhruv.patel@thecrimson.com)"
STATE = os.environ.get("JOBS_STATE", "jobs/seen.json")

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)

def all_postings():
    out, offset = [], 0
    while True:
        d = get(f"{API}?limit=100&offset={offset}")
        c = d.get("content", [])
        out += c
        offset += len(c)
        if len(c) < 100 or offset >= d.get("totalFound", 0):
            break
    return out

def posting_url(p): return f"https://jobs.smartrecruiters.com/HarvardUniversity/{p['id']}"

def line(p):
    loc = p.get("location", {}) or {}
    dept = (p.get("department") or {}).get("label", "")
    where = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
    return f"• <{posting_url(p)}|{(p.get('name') or '')[:72]}> — {where}{(' · ' + dept) if dept else ''}"

def slack(text):
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        print("(no SLACK_WEBHOOK_URL)"); return
    req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try: urllib.request.urlopen(req, timeout=20)
    except Exception as e: print("slack err", e)

def run():
    try: seen = set(json.load(open(STATE)).get("ids", []))
    except Exception: seen = set()
    posts = all_postings()
    ids = [p["id"] for p in posts]
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    if not seen:                       # first run: baseline silently
        json.dump({"ids": ids}, open(STATE, "w"))
        print(f"baseline: {len(ids)} postings"); return
    new = [p for p in posts if p["id"] not in seen]
    print(f"{len(posts)} postings, {len(new)} new")
    if new:
        body = f"*💼 {len(new)} new Harvard job posting(s)*\n" + "\n".join(line(p) for p in new[:40])
        if len(new) > 40: body += f"\n…and {len(new) - 40} more"
        slack(body)
    json.dump({"ids": ids}, open(STATE, "w"))

if __name__ == "__main__":
    run()
