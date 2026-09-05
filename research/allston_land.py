#!/usr/bin/env python3
"""Allston (or any Boston neighborhood) land-ownership intelligence, off Boston's
open Property Assessment rolls (data.boston.gov, no key).

  python3 allston_land.py owners   [NEIGHBORHOOD]   # biggest landholders + Harvard footprint
  python3 allston_land.py clusters [NEIGHBORHOOD]   # mailing addresses shared by many LLC names
                                                    #   -> unmasks shell-company assemblage
  python3 allston_land.py changes  [NEIGHBORHOOD]   # parcels that changed owner since last year
                                                    #   -> new purchases / sales, big/Harvard flagged

NEIGHBORHOOD defaults to ALLSTON (matches the roll's CITY field; try BRIGHTON, etc.).
Harvard is flagged by its legal owner name AND its known mailing addresses (1350
Massachusetts Ave / 516 Western Ave), so LLCs merely named after *Harvard Avenue*
(a street) aren't mistaken for the university.
"""
import json, sys, urllib.request, urllib.parse

CKAN = "https://data.boston.gov/api/3/action"
PKG = "property-assessment"
UA = "CrimsonNewsroomResearch/1.0 (dhruv.patel@thecrimson.com)"
# Harvard University's real-estate identity (not street-name LLCs)
HARVARD_OWNER = "\"OWNER\" ILIKE '%PRESIDENT AND FELLOWS%' OR \"OWNER\" ILIKE 'HARVARD REAL ESTATE%' OR \"OWNER\" ILIKE 'HARVARD UNIVERSITY%' OR \"OWNER\" ILIKE 'HARVARD RE/%' OR \"OWNER\" ILIKE 'HARVARD RE /%'"
HARVARD_MAIL = ("1350 MASSACHUSETTS", "516 WESTERN")

def api(action, **params):
    url = f"{CKAN}/{action}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    if not d.get("success"):
        raise SystemExit(f"CKAN error: {str(d)[:300]}")
    return d["result"]

def sql(q):
    return api("datastore_search_sql", sql=q)["records"]

def newest_resources(n=2):
    res = [r for r in api("package_show", id=PKG)["resources"] if r.get("datastore_active")]
    res.sort(key=lambda x: (x.get("created") or ""), reverse=True)
    return [r["id"] for r in res[:n]]

def money(v): return f"${float(v):,.0f}"

def owners(nbhd, rid):
    print(f"Biggest landholders in {nbhd} (FY roll {rid[:8]}…):\n")
    for r in sql(f'SELECT "OWNER", count(*) n, sum(replace("TOTAL_VALUE",\',\',\'\')::numeric) v '
                 f'FROM "{rid}" WHERE "CITY"=\'{nbhd}\' GROUP BY "OWNER" ORDER BY n DESC LIMIT 15'):
        print(f"  {int(r['n']):>4} parcels  {money(r['v']):>16}  {r['OWNER']}")
    print("\nHarvard-linked holdings — by legal owner name OR mailing to a Harvard office")
    print("(the mail match unmasks shell LLCs with no 'Harvard' in the name):")
    tot = 0
    rows = sql(f'SELECT "OWNER", "MAIL_STREET_ADDRESS" m, count(*) n, '
               f'sum(replace("TOTAL_VALUE",\',\',\'\')::numeric) v '
               f'FROM "{rid}" WHERE "CITY"=\'{nbhd}\' AND (({HARVARD_OWNER}) '
               f'OR "MAIL_STREET_ADDRESS" ILIKE \'%1350 MASSACHUSETTS%\' '
               f'OR "MAIL_STREET_ADDRESS" ILIKE \'%516 WESTERN%\') '
               f'GROUP BY "OWNER","MAIL_STREET_ADDRESS" ORDER BY v DESC LIMIT 25')
    for r in rows:
        o = (r["OWNER"] or "").upper()
        named = any(s in o for s in ("PRESIDENT AND FELLOWS", "HARVARD REAL ESTATE",
                                     "HARVARD UNIVERSITY", "HARVARD RE/", "HARVARD RE /"))
        why = "" if named else "  ⟵ shell? mails to Harvard office"
        print(f"  {int(r['n']):>4} parcels  {money(r['v']):>16}  {r['OWNER']}{why}"); tot += float(r["v"])
    print(f"  → total Harvard-linked value flagged in {nbhd}: {money(tot)} "
          f"(516 Western Ave is Harvard's Allston dev office — verify partner-vs-Harvard)")

def clusters(nbhd, rid):
    print(f"Mailing addresses shared by many distinct owner-names in {nbhd}")
    print("(a shell-LLC cluster: one office controlling parcels under many LLC names)\n")
    for r in sql(f'SELECT "MAIL_STREET_ADDRESS" a,"MAIL_CITY" c,"MAIL_STATE" s, count(*) parcels, '
                 f'count(distinct "OWNER") owners, sum(replace("TOTAL_VALUE",\',\',\'\')::numeric) v '
                 f'FROM "{rid}" WHERE "CITY"=\'{nbhd}\' AND "MAIL_STREET_ADDRESS" IS NOT NULL '
                 f'GROUP BY "MAIL_STREET_ADDRESS","MAIL_CITY","MAIL_STATE" HAVING count(distinct "OWNER")>=3 '
                 f'ORDER BY owners DESC LIMIT 15'):
        flag = "  ⟵ HARVARD" if any(h in (r['a'] or "").upper() for h in HARVARD_MAIL) else ""
        print(f"  {int(r['owners']):>3} owners /{int(r['parcels']):>3} parcels  {money(r['v']):>15}  "
              f"{r['a']}, {r['c']} {r['s']}{flag}")

def changes(nbhd, new_rid, old_rid):
    print(f"Parcels in {nbhd} that CHANGED OWNER since the prior roll (new purchases / sales):\n")
    # CKAN's SQL whitelist blocks trim()/upper(), so compare the raw owner strings.
    rows = sql(f'SELECT a."PID", a."ST_NUM" num, a."ST_NAME" st, a."OWNER" new_owner, b."OWNER" old_owner, '
               f'replace(a."TOTAL_VALUE",\',\',\'\')::numeric v '
               f'FROM "{new_rid}" a JOIN "{old_rid}" b ON a."PID"=b."PID" '
               f'WHERE a."CITY"=\'{nbhd}\' AND a."OWNER" <> b."OWNER" '
               f'ORDER BY v DESC LIMIT 40')
    if not rows: print("  (no owner changes found — the two rolls may be the same year)"); return
    for r in rows:
        buyer = (r['new_owner'] or "").upper()
        flag = "  ⟵ HARVARD" if ("PRESIDENT AND FELLOWS" in buyer or "HARVARD REAL ESTATE" in buyer
                                  or "HARVARD UNIVERSITY" in buyer or "HARVARD RE/" in buyer) else ""
        print(f"  {money(r['v']):>15}  {str(r['num'] or '').strip()} {r['st']}: "
              f"{r['old_owner']}  →  {r['new_owner']}{flag}")

def main():
    if len(sys.argv) < 2: print(__doc__); return
    mode = sys.argv[1]
    nbhd = (sys.argv[2] if len(sys.argv) > 2 else "ALLSTON").upper()
    rids = newest_resources(2)
    if mode == "owners":   owners(nbhd, rids[0])
    elif mode == "clusters": clusters(nbhd, rids[0])
    elif mode == "changes":  changes(nbhd, rids[0], rids[1])
    else: print(__doc__)

if __name__ == "__main__":
    main()
