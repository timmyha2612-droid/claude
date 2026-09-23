"""
Fill the people table (names, roles, emails, LinkedIn) for businesses.

Option A - Hunter.io API (free plan has a small monthly allowance):
  export HUNTER_API_KEY=your-key
  python find_people.py hunter --category food_wholesaler --limit 20

  Each website domain is only ever searched once (chains are searched once
  for all their stores), so you never waste credits on repeats.

Option B - Apollo.io CSV export (use their website, export a people list):
  python find_people.py apollo apollo_export.csv

  People are matched to businesses by website domain, then by company name.

Tip: spend people-lookups on B2B targets (wholesalers, food brands,
grocery chains) first. A single cafe rarely needs more than its own email.
"""
import argparse
import csv
import os
import sys
import time

import requests

import config
import db
from merge import domain

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS people_lookups (
    domain      TEXT PRIMARY KEY,
    source      TEXT,
    found       INTEGER,
    checked_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def add_person(conn, business_id, **p):
    conn.execute(
        "INSERT OR IGNORE INTO people (business_id, full_name, role, email, linkedin, source, domain, confidence, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (business_id, p.get("full_name"), p.get("role"), p.get("email"), p.get("linkedin"),
         p.get("source"), p.get("domain"), p.get("confidence"), p.get("notes")),
    )


def hunter_search(dom, key):
    r = requests.get(
        "https://api.hunter.io/v2/domain-search",
        params={"domain": dom, "api_key": key, "limit": 10},
        timeout=20,
    )
    if r.status_code == 429:
        raise RuntimeError("rate_limited")
    if r.status_code in (401, 403):
        raise RuntimeError(f"auth_or_quota: {r.text[:200]}")
    r.raise_for_status()
    return (r.json().get("data") or {}).get("emails") or []


def run_hunter(conn, category, limit):
    key = os.environ.get("HUNTER_API_KEY")
    if not key:
        sys.exit("Set HUNTER_API_KEY first (free account at hunter.io)")

    q = "SELECT id, website FROM businesses WHERE website IS NOT NULL AND website<>''"
    params = []
    if category:
        q += " AND category=?"
        params.append(category)
    q += " ORDER BY COALESCE(review_count, 0) DESC"   # bigger businesses first

    checked = {r["domain"] for r in conn.execute("SELECT domain FROM people_lookups")}
    by_domain = {}
    for r in conn.execute(q, params):
        d = domain(r["website"])
        if d and d not in checked and d not in by_domain:
            by_domain[d] = r["id"]
    todo = list(by_domain.items())[: limit or None]
    print(f"Searching Hunter for {len(todo)} new domains")

    total = 0
    for d, biz_id in todo:
        try:
            emails = hunter_search(d, key)
        except RuntimeError as exc:
            if "rate_limited" in str(exc):
                time.sleep(30)
                continue
            print(f"Stopping: {exc}")
            break
        except requests.RequestException as exc:
            print(f"  {d}: {exc}")
            continue
        for e in emails:
            name = " ".join(x for x in [e.get("first_name"), e.get("last_name")] if x) or None
            add_person(conn, biz_id, full_name=name, role=e.get("position"), email=e.get("value"),
                       linkedin=e.get("linkedin"), source="hunter", domain=d,
                       confidence=e.get("confidence"), notes=e.get("type"))
        conn.execute("INSERT OR REPLACE INTO people_lookups (domain, source, found) VALUES (?,?,?)",
                     (d, "hunter", len(emails)))
        conn.commit()
        total += len(emails)
        print(f"  {d}: {len(emails)} people")
        time.sleep(1)
    print(f"Done: {total} people added")


def run_apollo(conn, path):
    doms, names = {}, {}
    for r in conn.execute("SELECT id, name, legal_name, website FROM businesses"):
        d = domain(r["website"])
        if d:
            doms.setdefault(d, r["id"])
        for n in (r["name"], r["legal_name"]):
            if n:
                names.setdefault(db.clean_name(n), r["id"])

    added = unmatched = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d = domain(row.get("Website") or row.get("Company Website") or "")
            biz_id = doms.get(d) if d else None
            if not biz_id:
                company = row.get("Company") or row.get("Company Name") or row.get("Company Name for Emails") or ""
                biz_id = names.get(db.clean_name(company))
            if not biz_id:
                unmatched += 1
                continue
            name = " ".join(x for x in [row.get("First Name"), row.get("Last Name")] if x) or row.get("Name")
            add_person(conn, biz_id, full_name=name, role=row.get("Title"), email=(row.get("Email") or None),
                       linkedin=row.get("Person Linkedin Url") or row.get("LinkedIn"), source="apollo", domain=d,
                       notes=row.get("Email Status"))
            added += 1
    conn.commit()
    print(f"Apollo import: {added} people matched to businesses, {unmatched} rows had no matching business")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("hunter")
    h.add_argument("--category")
    h.add_argument("--limit", type=int, default=20)
    a = sub.add_parser("apollo")
    a.add_argument("csv_path")
    args = p.parse_args()

    conn = db.connect(config.DB_PATH)
    conn.executescript(EXTRA_SCHEMA)
    if args.mode == "hunter":
        run_hunter(conn, args.category, args.limit)
    else:
        run_apollo(conn, args.csv_path)


if __name__ == "__main__":
    main()
