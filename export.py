"""
Stage 3: coverage report + CSV exports.

Usage:
  python3 export.py                         -> businesses_export.csv + people_export.csv
  python3 export.py --category coffee_shop --out sydney_cafes.csv
  python3 export.py --all-rows              (one row per source instead of per business)

By default each real business is ONE row (after merge.py), with details and
contacts combined from every source (OSM, Google, website, ABR).
"""
import argparse
import csv
from collections import defaultdict

import config
import db

KINDS = ["instagram", "facebook", "linkedin", "tiktok", "email", "phone"]
FIELDS = ["name", "legal_name", "category", "subcategory", "cuisine", "street", "suburb", "state", "postcode",
          "lat", "lon", "phone", "website", "opening_hours", "rating", "review_count", "abn", "abn_status"]
PREFER = {"google": 0, "osm": 1}  # which source wins when both have a value


def report(conn):
    print("\n=== Coverage report ===")
    unique = conn.execute("SELECT COUNT(DISTINCT COALESCE(group_id, -id)) FROM businesses").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    print(f"{total} rows, {unique} unique businesses" + ("" if total == unique else " (after merge)"))

    print("\nBy category:")
    for r in conn.execute(
        "SELECT category, COUNT(*) n, SUM(website IS NOT NULL AND website<>'') w, SUM(abn IS NOT NULL) a "
        "FROM businesses GROUP BY category ORDER BY n DESC"
    ):
        print(f"  {r['category']:<16} {r['n']:>6} rows  {r['w']:>6} with website  {r['a']:>6} with ABN")

    print("\nBy source:")
    for r in conn.execute("SELECT source, COUNT(*) n FROM businesses GROUP BY 1"):
        print(f"  {r['source']:<10} {r['n']}")

    print("\nWebsite enrichment status:")
    for r in conn.execute("SELECT enrich_status, COUNT(*) n FROM businesses GROUP BY 1"):
        print(f"  {r['enrich_status']:<12} {r['n']}")

    unique_with = lambda where: conn.execute(
        f"SELECT COUNT(DISTINCT COALESCE(b.group_id, -b.id)) FROM businesses b WHERE {where}").fetchone()[0]
    has_phone = ("(b.phone IS NOT NULL AND b.phone<>'') OR EXISTS "
                 "(SELECT 1 FROM contacts c WHERE c.business_id=b.id AND c.kind='phone')")
    has_mobile = ("b.phone_norm LIKE '+614%' OR EXISTS "
                  "(SELECT 1 FROM contacts c WHERE c.business_id=b.id AND c.kind='phone' AND c.value LIKE '+614%')")
    has_email = "EXISTS (SELECT 1 FROM contacts c WHERE c.business_id=b.id AND c.kind='email')"
    has_free = ("EXISTS (SELECT 1 FROM contacts c WHERE c.business_id=b.id AND c.kind='email' AND ("
                + " OR ".join(f"c.value LIKE '%@{d}'" for d in sorted(db.FREEMAIL)) + "))")
    print("\nMain goal (unique businesses):")
    print(f"  any phone   {unique_with(has_phone):>6}   of which mobile {unique_with(has_mobile)}")
    print(f"  any email   {unique_with(has_email):>6}   of which gmail/free mail {unique_with(has_free)}")

    print("\nBusinesses with at least one:")
    for k in KINDS:
        n = conn.execute("SELECT COUNT(DISTINCT business_id) FROM contacts WHERE kind=?", (k,)).fetchone()[0]
        print(f"  {k:<10} {n}")
    n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT business_id) FROM people").fetchone()
    print(f"\nPeople: {n[0]} across {n[1]} businesses")


def export_businesses(conn, path, category, all_rows):
    where, params = "", []
    if category:
        where, params = "WHERE category=?", [category]
    rows = conn.execute(f"SELECT * FROM businesses {where}", params).fetchall()

    groups = defaultdict(list)
    for r in rows:
        key = r["id"] if all_rows or r["group_id"] is None else r["group_id"]
        groups[key].append(r)

    contacts = defaultdict(lambda: defaultdict(list))
    for c in conn.execute("SELECT business_id, kind, value FROM contacts"):
        contacts[c["business_id"]][c["kind"]].append(c["value"])
    people_count = defaultdict(int)
    for p in conn.execute("SELECT business_id, COUNT(*) n FROM people GROUP BY 1"):
        people_count[p["business_id"]] = p["n"]

    out = []
    for key, members in groups.items():
        members.sort(key=lambda r: PREFER.get(r["source"], 9))
        merged = {f: next((m[f] for m in members if m[f] not in (None, "")), None) for f in FIELDS}
        merged["business_key"] = key
        merged["sources"] = ", ".join(sorted({m["source"] for m in members}))
        merged["source_ids"] = " | ".join(f"{m['source']}:{m['source_id']}" for m in members)
        for k in KINDS:
            vals = []
            for m in members:
                for v in contacts[m["id"]][k]:
                    if v not in vals:
                        vals.append(v)
            merged[f"{k}s"] = " | ".join(vals)
        emails = [e for e in merged["emails"].split(" | ") if e]
        if emails:
            best = min(emails, key=lambda e: db.email_rank(e, merged["name"])[0])
            merged["best_email"], merged["email_type"] = best, db.email_rank(best, merged["name"])[1]
        phones = [p for p in merged["phones"].split(" | ") if p]
        phones += [c[1] for m in members for c in [db.clean_contact("phone", m["phone"])] if c and c[1] not in phones]
        if phones:
            best = min(phones, key=lambda p: db.phone_rank(p)[0])
            merged["best_phone"], merged["phone_type"] = best, db.phone_rank(best)[1]
            merged["phones"] = " | ".join(phones)
        merged["people_found"] = sum(people_count[m["id"]] for m in members)
        out.append(merged)

    out.sort(key=lambda r: (r["category"] or "", r["suburb"] or "", r["name"] or ""))
    cols = (["business_key", "name", "best_email", "email_type", "best_phone", "phone_type"]
            + [f for f in FIELDS if f != "name"] + [f"{k}s" for k in KINDS] + ["people_found", "sources", "source_ids"])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="")
        w.writeheader()
        w.writerows(out)
    print(f"\nExported {len(out)} businesses to {path}")


def export_people(conn, path):
    rows = conn.execute(
        "SELECT COALESCE(b.group_id, b.id) business_key, b.name business, b.category, b.suburb, b.state, "
        "p.full_name, p.role, p.email, p.linkedin, p.confidence, p.source "
        "FROM people p JOIN businesses b ON b.id=p.business_id ORDER BY b.category, b.name"
    ).fetchall()
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys())
        w.writerows([tuple(r) for r in rows])
    print(f"Exported {len(rows)} people to {path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--category")
    p.add_argument("--out", default="businesses_export.csv")
    p.add_argument("--people-out", default="people_export.csv")
    p.add_argument("--all-rows", action="store_true")
    args = p.parse_args()
    conn = db.connect(config.DB_PATH)
    fixed = db.tidy_contacts(conn)
    if fixed:
        print(f"Tidied {fixed} contacts into the standard format")
    report(conn)
    export_businesses(conn, args.out, args.category, args.all_rows)
    export_people(conn, args.people_out)


if __name__ == "__main__":
    main()
