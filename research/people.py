#!/usr/bin/env python3
"""Agnostic people-page extractor: render ANY lab/org people page (headless, so JS-built
member lists work) and LLM-extract {name, role, email}. Dispatched by the /people slash
command; posts the list back to Slack's response_url.

env: PEOPLE_URL (required), PEOPLE_RESPONSE_URL (Slack response_url), ANTHROPIC_API_KEY,
     ANTHROPIC_MODEL (optional, default a fast model)
"""
import os, sys, re, json, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root -> report.py
import report   # reuse the watch engine's headless render + structure-preserving text

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

import urllib.parse

def fetch_html(url):
    """Get a page's HTML. Sites behind Akamai (e.g. *.harvard.edu) block datacenter IPs
    (GitHub Actions) AND require running the Akamai sensor JS — so we route through a
    free-tier anti-bot API that fetches from a residential IP and solves the challenge:
      SCRAPFLY_KEY   -> Scrapfly with asp=true (purpose-built for Akamai; 1000/mo free, no CC)
      SCRAPER_API_KEY-> ScraperAPI render (1000/mo free)
    With neither, render locally with headless Chromium (fine for non-Akamai sites)."""
    sf, sa = os.environ.get("SCRAPFLY_KEY"), os.environ.get("SCRAPER_API_KEY")
    if sf:
        api = "https://api.scrapfly.io/scrape?" + urllib.parse.urlencode(
            {"key": sf, "url": url, "render_js": "true", "asp": "true", "country": "us"})
        try:
            kind, payload = report._fetch(api, timeout=120)
            if kind == "text":
                return json.loads(payload).get("result", {}).get("content", "")
        except Exception as e:
            print(f"[debug] scrapfly failed ({e})")
    if sa:
        api = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": sa, "url": url, "render": "true"})
        try:
            kind, payload = report._fetch(api, timeout=90)
            if kind == "text":
                return payload
        except Exception as e:
            print(f"[debug] scraperapi failed ({e})")
    try:
        kind, payload = report._fetch_rendered(url)
    except Exception as e:
        print(f"[debug] local render failed ({e}); plain fetch")
        kind, payload = report._fetch(report._fetch_target(url))
    return payload if kind == "text" else ""

def page_text(url):
    return report._page_text(fetch_html(url))

def extract(text):
    key = os.environ["ANTHROPIC_API_KEY"]
    prompt = ("From this webpage, list every PERSON who is a member of this group/lab/org "
              "(ignore navigation, publications/citations, news items). Return ONLY a JSON array, "
              'no prose:\n[{"name":"","role":"","email":""}]\n'
              'role must be one of: PI/Professor, Postdoc, Grad Student, Undergrad, Staff, Other. '
              'Use "" for a missing email.\n\nPAGE:\n' + text[:60000])
    body = json.dumps({"model": MODEL, "max_tokens": 4096,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    out = d["content"][0]["text"].strip()
    m = re.search(r"\[.*\]", out, re.S)   # pull the JSON array out of any fences/prose
    if not m:
        raise ValueError("no JSON array in model output: " + out[:200])
    return json.loads(m.group(0))

def post(resp_url, text):
    if resp_url:
        try:
            urllib.request.urlopen(urllib.request.Request(resp_url,
                data=json.dumps({"response_type": "ephemeral", "text": text[:3900]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=20)
        except Exception as e:
            print("post error", e)
    else:
        print(text)

def main():
    url = os.environ["PEOPLE_URL"]; resp = os.environ.get("PEOPLE_RESPONSE_URL", "")
    try:
        text = page_text(url)
        if "Access Denied" in text[:400] or "don't have permission to access" in text[:600]:
            raise RuntimeError("the site blocked our server's IP (Akamai/WAF — common on *.harvard.edu). "
                               "Set a SCRAPER_API_KEY (free ScraperAPI) to fetch via a residential IP.")
        if len(text) < 40:
            raise RuntimeError("page had no readable text")
        people = extract(text)
        if not isinstance(people, list) or not people:
            msg = f"No people found on <{url}>."
        else:
            lines = [f"• *{p.get('name','?')}* — {p.get('role','?')}"
                     + (f" · {p['email']}" if p.get("email") else "") for p in people[:80]]
            msg = f"*People on <{url}>* ({len(people)})\n" + "\n".join(lines)
            if len(people) > 80:
                msg += f"\n…and {len(people)-80} more"
    except Exception as e:
        msg = f"⚠️ Couldn't extract people from <{url}> ({e})."
    post(resp, msg)

if __name__ == "__main__":
    main()
