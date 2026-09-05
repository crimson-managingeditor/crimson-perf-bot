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

def page_text(url):
    # render with headless Chromium (handles JS member lists); fall back to a plain fetch
    try:
        kind, payload = report._fetch_rendered(url)
    except Exception:
        kind, payload = report._fetch(report._fetch_target(url))
    return report._page_text(payload) if kind == "text" else ""

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
