#!/usr/bin/env python3
"""Find Harvard affiliates within a category — reverse lookup via Wikidata.

  python3 affil.py senators
  python3 affil.py house
  python3 affil.py Q<positionQID>        # any 'position held' (P39) value
  python3 affil.py senators --all        # include former office-holders too

Matches ANY Harvard school (College, Law, Kennedy, Business, Medical, …) by
following `part of`* to Harvard University (Q13371), and lists each person's
Harvard degree(s) + Wikipedia link. Notable people only (Wikidata coverage);
for non-notable populations (e.g. a firm's junior associates) scrape the org's
public bio pages and match "Harvard" — that path shares the people-scraper's
LLM extractor and is built separately.
"""
import json, sys, urllib.request, urllib.parse
from collections import OrderedDict

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "CrimsonNewsroomResearch/1.0 (dhruv.patel@thecrimson.com)"
HARVARD = "Q13371"

# friendly name -> Wikidata "position held" (P39) QID
CATEGORIES = {
    "senators":  "Q4416090",    # member of the US Senate
    "house":     "Q13218630",   # member of the US House of Representatives
    "governors": "Q889821",     # governor (US state)
}

def run(position_qid, current_only=True):
    cur = "FILTER NOT EXISTS { ?ps pq:P582 ?end . }" if current_only else ""
    q = f"""
SELECT DISTINCT ?person ?personLabel ?schoolLabel ?article WHERE {{
  ?person p:P39 ?ps . ?ps ps:P39 wd:{position_qid} .
  {cur}
  ?person wdt:P69 ?school . ?school wdt:P361* wd:{HARVARD} .
  OPTIONAL {{ ?article schema:about ?person ; schema:isPartOf <https://en.wikipedia.org/> . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
}} ORDER BY ?personLabel"""
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": q})
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        rows = json.load(r)["results"]["bindings"]
    # aggregate schools per person (dual degrees -> one row)
    people = OrderedDict()
    for b in rows:
        name = b["personLabel"]["value"]
        rec = people.setdefault(name, {"schools": [], "wp": None})
        s = b.get("schoolLabel", {}).get("value")
        if s and s not in rec["schools"]: rec["schools"].append(s)
        if b.get("article"): rec["wp"] = b["article"]["value"]
    return people

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    arg = sys.argv[1]
    current_only = "--all" not in sys.argv[2:]
    qid = CATEGORIES.get(arg, arg)
    if not qid.startswith("Q"):
        print(f"unknown category '{arg}'. Known: {', '.join(CATEGORIES)} — or pass a Q<id>."); return
    people = run(qid, current_only)
    scope = "in office" if current_only else "ever held office"
    print(f"{len(people)} Harvard affiliates who have {scope} — category {arg}:\n")
    for name, rec in people.items():
        schools = ", ".join(rec["schools"])
        wp = f"  {rec['wp']}" if rec["wp"] else ""
        print(f"  • {name} — {schools}{wp}")
    print("\nNote: 'in office' relies on Wikidata end-dates, which are incomplete for large\n"
          "bodies (e.g. the House) — a count above the real seat total means former members\n"
          "lack an end-date and slipped in. For precise current membership, intersect with an\n"
          "authoritative roster (bioguide/FEC id) — the list-intersection mode.")

if __name__ == "__main__":
    main()
