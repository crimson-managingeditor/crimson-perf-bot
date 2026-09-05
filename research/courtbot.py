#!/usr/bin/env python3
"""Court docket watcher: polls CourtListener for NEW filings on watched dockets and
posts each to Slack with a free RECAP link, or a "sign in to PACER to buy" link when
the PDF isn't free. Reporters add dockets with /casewatch; this runs on a schedule.

env:   COURTLISTENER_TOKEN (required for the API), SLACK_BOT_TOKEN
state: court/watches.json — [{docket_id, name, court, url, channel, added_by, added_by_id, seen:[entryIds]}]

Note: docket-entries / recap-documents are token-gated, so this is validated on the
first scheduled run (it prints what it sees, and baselines silently before alerting).
"""
import json, os, urllib.request

CL = "https://www.courtlistener.com/api/rest/v4"
TOKEN = os.environ.get("COURTLISTENER_TOKEN", "")
UA = "CrimsonNewsroom/1.0 (dhruv.patel@thecrimson.com)"
WATCHES = os.environ.get("COURT_WATCHES", "court/watches.json")
SEEN_CAP = 250

def cl_get(path):
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    if TOKEN: hdr["Authorization"] = "Token " + TOKEN
    req = urllib.request.Request(CL + path, headers=hdr)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def entries(docket_id):
    # newest first; recap_sequence_number is the stable per-docket ordering
    return cl_get(f"/docket-entries/?docket={docket_id}&order_by=-recap_sequence_number&page_size=40").get("results", [])

def doc_link(rd):
    """(url, is_free) — prefer the free RECAP PDF, else the CourtListener page (Buy on PACER)."""
    if rd.get("is_available") and rd.get("filepath_local"):
        return "https://storage.courtlistener.com/" + rd["filepath_local"], True
    if rd.get("absolute_url"):
        return "https://www.courtlistener.com" + rd["absolute_url"], bool(rd.get("is_available"))
    return "", False

def slack(channel, text):
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok or not channel:
        print("(no slack token/channel)"); return
    req = urllib.request.Request("https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text, "unfurl_links": False}).encode(),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
        if not r.get("ok"): print("slack error", r.get("error"))
    except Exception as e:
        print("slack error", e)

def run():
    try: watches = json.load(open(WATCHES))
    except Exception: watches = []
    if not watches:
        print("no court watches"); return
    changed = False
    for w in watches:
        did = w.get("docket_id")
        try:
            es = entries(did)
        except Exception as e:
            print(f"fetch error docket {did}: {e}"); continue
        ids = [e["id"] for e in es]
        seen = set(w.get("seen", []))
        if not seen:                       # first sighting: baseline silently
            w["seen"] = ids[:SEEN_CAP]; changed = True
            print(f"baseline docket {did}: {len(ids)} entries"); continue
        new = [e for e in es if e["id"] not in seen]
        for e in reversed(new):            # oldest-new first
            num, date = e.get("entry_number"), e.get("date_filed") or ""
            desc = (e.get("description") or "").strip()[:600] or "(no description)"
            link, free = "", False
            for rd in (e.get("recap_documents") or []):
                l, fr = doc_link(rd)
                if l: link, free = l, fr; break
            tag = "📄 free on RECAP" if free else "🔒 not free — open, sign in to PACER to buy"
            body = f"🆕 *{w.get('name') or 'Docket ' + str(did)}* — filing #{num} ({date})\n{desc}"
            body += f"\n<{link or w.get('url','')}|{tag}>"
            slack(w.get("channel"), body)
            print(f"alerted docket {did} entry {e['id']}")
        if new:
            w["seen"] = sorted(seen | set(ids))[-SEEN_CAP:]; changed = True
    if changed:
        os.makedirs(os.path.dirname(WATCHES) or ".", exist_ok=True)
        json.dump(watches, open(WATCHES, "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    run()
