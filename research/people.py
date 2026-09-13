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
            {"key": sf, "url": url, "render_js": "true", "asp": "true", "country": "us",
             "auto_scroll": "true"})   # pulls lazy/infinite-scroll members into the DOM
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
        return render_exhaustive(url)
    except Exception as e:
        print(f"[debug] local render failed ({e}); plain fetch")
        kind, payload = report._fetch(report._fetch_target(url))
    return payload if kind == "text" else ""


# Controls that reveal more of a directory in place, rather than linking to a page 2.
_MORE_JS = r"""() => {
  const re = /\b(load|show|view|see)\s+(more|all)\b|\bmore\s+(results|people|members|staff)\b/i;
  const els = [...document.querySelectorAll(
    'button, a, [role=button], input[type=button], input[type=submit]')];
  for (const el of els) {
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (!re.test(label)) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none') continue;
    el.scrollIntoView({block: 'center'});
    el.click();
    return label.slice(0, 40);
  }
  return null;
}"""

def render_exhaustive(url, timeout=35, max_rounds=40, budget_s=150):
    """Render a page AND exhaust its in-place pagination.

    Plenty of people directories never link to a page 2 -- they ship one batch and a
    "Load More" button, or load on scroll. report._fetch_rendered() returns the first
    batch and nothing detects that more exists, which is why a 200-person directory
    came back as "fetched 1 page(s)" with a couple of dozen names.

    So: click any visible load-more control, fall back to scrolling for infinite lists,
    and stop when a round adds no new text (or we hit the round/time budget).
    """
    from playwright.sync_api import sync_playwright
    import time
    start = time.time()
    rounds, clicks, last_len = 0, 0, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=report._UA)
            page.set_default_timeout(timeout * 1000)
            page.goto(url, wait_until="networkidle")
            while rounds < max_rounds and time.time() - start < budget_s:
                label = page.evaluate(_MORE_JS)
                if label:
                    clicks += 1
                else:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                cur = page.evaluate("document.body.innerText.length")
                if cur <= last_len:
                    break                      # nothing new appeared -- the list is complete
                last_len = cur
                rounds += 1
            html = page.content()
        finally:
            browser.close()
    print(f"[debug] expanded in place: {clicks} load-more click(s), "
          f"{rounds} round(s), {last_len:,} chars")
    return html

# /page/2/, ?page=2, ?paged=2, ?pg=2 — the usual numbered-pagination shapes.
PAGE_NUM_RE = re.compile(r"(?i)(?:/page/|[?&](?:page|paged|pg)=)(\d+)")

def _page_num(u):
    m = PAGE_NUM_RE.search(u or "")
    return int(m.group(1)) if m else 1

def next_page_url(page_html, base):
    """Find the next page of a directory, across the shapes CMSs actually use:
    rel=next (anchor or <link>), a control labelled/classed 'next', or numbered
    pagination where only 1 2 3 … are rendered and there is no 'next' at all."""
    def resolve(h): return urllib.parse.urljoin(base, h.replace("&amp;", "&").strip())

    # 1. rel=next, on an <a> or on a <link> in the head
    for tag in ("a", "link"):
        m = re.search(r'<%s\b[^>]*\brel=["\']?next\b[^>]*>' % tag, page_html, re.I)
        if m:
            h = re.search(r'href=["\']([^"\']+)', m.group(0))
            if h: return resolve(h.group(1))

    # 2. a link labelled, aria-labelled, titled or classed "next"
    for attrs, inner in re.findall(r"<a\b([^>]*)>(.*?)</a>", page_html, re.S | re.I):
        it = re.sub(r"<[^>]+>", " ", inner).strip().lower()
        al = attrs.lower()
        if (it[:4] == "next" or it in ("\u203a", "\u00bb", "\u2192")
                or re.search(r'(aria-label|title)=["\'][^"\']*next', al)
                or re.search(r'class=["\'][^"\']*\bnext\b', al)):
            h = re.search(r'href=["\']([^"\']+)', attrs)
            if h: return resolve(h.group(1))

    # 3. numbered pagination with no "next" control: take the link one past this page
    here = _page_num(base)
    best = None
    for h in re.findall(r'<a\b[^>]*href=["\']([^"\']+)', page_html, re.I):
        full = resolve(h)
        if not PAGE_NUM_RE.search(full):
            continue
        n = _page_num(full)
        if n == here + 1 and (best is None or len(full) < len(best)):
            best = full
    return best

def page_text(url, max_pages=20):
    """Fetch the page and follow its own pagination ('next') links, so a paginated people
    directory returns everyone — not just page 1. Concatenate the pages' text for one extract."""
    seen, parts, cur = set(), [], url
    while cur and cur not in seen and len(seen) < max_pages:
        seen.add(cur)
        doc = fetch_html(cur)
        if not doc:
            break
        if "Access Denied" in doc[:800] or "don't have permission to access" in doc[:1200]:
            return "__BLOCKED__"
        parts.append(report._page_text(doc))
        cur = next_page_url(doc, cur)
    print(f"[debug] fetched {len(parts)} page(s) of pagination")
    return "\n\n".join(parts)

def extract(text):
    key = os.environ["ANTHROPIC_API_KEY"]
    prompt = ("From this webpage, list every PERSON who is a member of this group/lab/org "
              "(ignore navigation, publications/citations, news items). Return ONLY a JSON array, "
              'no prose:\n[{"name":"","role":"","email":""}]\n'
              'role must be one of: PI/Professor, Postdoc, Grad Student, Undergrad, Staff, Other. '
              'Use "" for a missing email. De-duplicate people who appear on more than one page.'
              '\n\nPAGE:\n' + text[:120000])
    body = json.dumps({"model": MODEL, "max_tokens": 8000,
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
        if text == "__BLOCKED__":
            raise RuntimeError("the site blocked our server's IP (Akamai/WAF — common on *.harvard.edu). "
                               "Set SCRAPFLY_KEY (free, no CC) or SCRAPER_API_KEY to fetch via a residential IP.")
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
