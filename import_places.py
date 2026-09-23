"""
Import Google Maps data you bought from Outscraper, Apify, Bright Data etc.
into the same database. It recognises the common column names from each
tool, so you usually don't need to edit the CSV first.

Usage:
  python import_places.py outscraper_export.csv --category coffee_shop
  python import_places.py apify_dataset.csv --category restaurant
  python import_places.py export.xlsx ...   (save as CSV first)

Run merge.py afterwards to link these rows to the matching OSM rows.
"""
import argparse
import csv
import sys

import config
import db

# For each field, the column names different tools use (first match wins).
COLUMN_MAP = {
    "source_id": ["place_id", "placeId", "google_id", "cid", "data_id"],
    "name": ["name", "title", "business_name"],
    "subcategory": ["category", "categoryName", "type", "main_category"],
    "street": ["street", "address_street"],
    "suburb": ["city", "suburb", "neighborhood", "locality"],
    "state": ["state", "us_state", "region"],
    "postcode": ["postal_code", "postalCode", "zip", "postcode"],
    "lat": ["latitude", "lat", "location/lat", "location.lat"],
    "lon": ["longitude", "lng", "lon", "location/lng", "location.lng"],
    "phone": ["phone", "phoneUnformatted", "phone_number", "international_phone_number"],
    "website": ["site", "website", "url_website", "domain"],
    "email": ["email_1", "email", "emails/0", "emails"],
    "opening_hours": ["working_hours", "openingHours", "hours"],
    "rating": ["rating", "totalScore", "stars"],
    "review_count": ["reviews", "reviewsCount", "user_ratings_total"],
    "full_address": ["full_address", "address", "formatted_address"],
}
SOCIAL_COLUMNS = {
    "instagram": ["instagram", "instagrams/0", "instagrams"],
    "facebook": ["facebook", "facebooks/0", "facebooks"],
    "linkedin": ["linkedin", "linkedIns/0", "linkedins"],
    "tiktok": ["tiktok", "tiktoks/0", "tiktoks"],
}
AU_STATES = {
    "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD", "western australia": "WA",
    "south australia": "SA", "tasmania": "TAS", "australian capital territory": "ACT", "northern territory": "NT",
}


def pick(row, names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "[]", "null"):
            return str(v).strip()
    return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path")
    p.add_argument("--category", required=True, help="one of: " + ", ".join(config.CATEGORIES))
    p.add_argument("--source", default="google", help="label stored in the source column")
    args = p.parse_args()

    if args.category not in config.CATEGORIES:
        sys.exit(f"Unknown category. Use one of: {', '.join(config.CATEGORIES)}")

    conn = db.connect(config.DB_PATH)
    saved = skipped = 0
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        found = {k: next((n for n in v if n in cols), None) for k, v in COLUMN_MAP.items()}
        print("Column mapping detected:")
        for k, v in found.items():
            print(f"  {k:<14} <- {v or '(not in file)'}")
        if not found["name"]:
            sys.exit("Could not find a name/title column. Check the file.")

        for row in reader:
            name = pick(row, COLUMN_MAP["name"])
            if not name:
                skipped += 1
                continue
            sid = pick(row, COLUMN_MAP["source_id"])
            lat, lon = to_float(pick(row, COLUMN_MAP["lat"])), to_float(pick(row, COLUMN_MAP["lon"]))
            if not sid:  # no id column: build a stable one from name + location
                sid = f"{db.clean_name(name)}@{lat},{lon}"

            state = pick(row, COLUMN_MAP["state"])
            if state:
                state = AU_STATES.get(state.lower(), state.upper())

            website = pick(row, COLUMN_MAP["website"])
            if website and not website.startswith("http"):
                website = "https://" + website

            rec = {
                "source": args.source,
                "source_id": sid,
                "name": name,
                "category": args.category,
                "subcategory": pick(row, COLUMN_MAP["subcategory"]),
                "street": pick(row, COLUMN_MAP["street"]) or pick(row, COLUMN_MAP["full_address"]),
                "suburb": pick(row, COLUMN_MAP["suburb"]),
                "state": state,
                "postcode": pick(row, COLUMN_MAP["postcode"]),
                "lat": lat,
                "lon": lon,
                "phone": pick(row, COLUMN_MAP["phone"]),
                "website": website,
                "email": pick(row, COLUMN_MAP["email"]),
                "opening_hours": pick(row, COLUMN_MAP["opening_hours"]),
            }
            biz_id = db.upsert_business(conn, rec)
            conn.execute(
                "UPDATE businesses SET rating=?, review_count=? WHERE id=?",
                (to_float(pick(row, COLUMN_MAP["rating"])), to_int(pick(row, COLUMN_MAP["review_count"])), biz_id),
            )
            if rec["email"]:
                for e in rec["email"].replace(";", ",").split(","):
                    if "@" in e:
                        db.add_contact(conn, biz_id, "email", e.strip().lower(), args.source)
            for kind, names in SOCIAL_COLUMNS.items():
                v = pick(row, names)
                if v:
                    db.add_contact(conn, biz_id, kind, v, args.source)
            saved += 1
            if saved % 500 == 0:
                conn.commit()
    conn.commit()
    print(f"\nImported {saved} businesses ({skipped} rows skipped with no name)")
    print("Next: python merge.py, then python enrich_web.py for any new websites")


if __name__ == "__main__":
    main()
