"""
Stage 1: collect businesses from OpenStreetMap (free) through the Overpass API.

Usage:
  python3 collect_osm.py --category coffee_shop --bbox sydney
  python3 collect_osm.py --category restaurant --state NSW
  python3 collect_osm.py --category all --state all        (Australia-wide, slow)
"""
import argparse
import json
import re
import time

import requests

import config
import db


def build_query(tag_filters, bbox=None, iso_area=None):
    if bbox:
        s, w, n, e = bbox
        scope = f"({s},{w},{n},{e})"
        header = ""
    else:
        header = f'area["ISO3166-2"="{iso_area}"]->.a;\n'
        scope = "(area.a)"

    parts = []
    for key, value in tag_filters:
        tag = f'["{key}"]' if value is None else f'["{key}"="{value}"]'
        # nwr = nodes, ways and relations (some shops are drawn as buildings)
        parts.append(f'  nwr{tag}["name"]{scope};')

    return (
        f"[out:json][timeout:{config.OVERPASS_TIMEOUT}][maxsize:1073741824];\n"
        f"{header}(\n" + "\n".join(parts) + "\n);\nout center tags;"
    )


def run_query(query, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                config.OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": config.USER_AGENT},
                timeout=config.OVERPASS_TIMEOUT + 30,
            )
            if r.status_code == 429 or r.status_code >= 500:
                wait = 60 * attempt
                print(f"  server busy ({r.status_code}), waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            # Overpass answers 200 with a "remark" when it ran out of time or memory: the results are incomplete
            remark = data.get("remark", "")
            if "error" in remark.lower():
                print(f"  WARNING: OpenStreetMap returned partial results: {remark}")
                print("  Re-run this command later, or use a smaller area. Rows already saved are kept.")
            return data
        except requests.RequestException as exc:
            print(f"  request failed: {exc} (attempt {attempt})")
            time.sleep(30 * attempt)
    raise RuntimeError("Overpass query failed after retries")


def first(tags, *keys):
    for k in keys:
        if tags.get(k):
            return tags[k]
    return None


def element_to_record(el, category, state_code):
    tags = el.get("tags", {})
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")

    street = " ".join(x for x in [tags.get("addr:housenumber"), tags.get("addr:street")] if x) or None
    sub = next((f"{k}={v}" for k, v in tags.items() if k in ("amenity", "shop", "craft", "industrial", "wholesale")), None)
    website = first(tags, "website", "contact:website", "url")
    if website:
        website = re.split(r"[;\s]+", website.strip())[0]   # some entries list several sites
    if website and not website.startswith("http"):
        website = "https://" + website

    return {
        "source": "osm",
        "source_id": f"{el['type']}/{el['id']}",
        "name": tags.get("name"),
        "category": category,
        "subcategory": sub,
        "cuisine": tags.get("cuisine"),
        "brand": tags.get("brand"),
        "street": street,
        "suburb": first(tags, "addr:suburb", "addr:city"),
        "state": tags.get("addr:state") or state_code,
        "postcode": tags.get("addr:postcode"),
        "lat": lat,
        "lon": lon,
        "phone": first(tags, "phone", "contact:phone"),
        "website": website,
        "email": first(tags, "email", "contact:email"),
        "opening_hours": tags.get("opening_hours"),
        "abn": tags.get("ref:ABN") or tags.get("ref:abn"),
        # socials sometimes live directly in OSM tags, keep them
        "_socials": {
            "instagram": tags.get("contact:instagram"),
            "facebook": tags.get("contact:facebook"),
            "linkedin": tags.get("contact:linkedin"),
            "tiktok": tags.get("contact:tiktok"),
        },
    }


def save(conn, records):
    for rec in records:
        biz_id = db.upsert_business(conn, rec)
        for kind, value in rec["_socials"].items():
            if value:
                db.add_contact(conn, biz_id, kind, value.strip(), "osm_tag")
        if rec.get("email"):
            db.add_contact(conn, biz_id, "email", rec["email"].lower(), "osm_tag")
    conn.commit()


def collect(conn, category, bbox_name=None, state=None, raw_json=None):
    tag_filters = config.CATEGORIES[category]
    if raw_json is not None:                     # used for offline testing
        data = raw_json
        state_code = state
    elif bbox_name:
        data = run_query(build_query(tag_filters, bbox=config.PILOT_BBOX[bbox_name]))
        state_code = None
    else:
        data = run_query(build_query(tag_filters, iso_area=config.REGIONS[state]))
        state_code = state

    records = [element_to_record(el, category, state_code) for el in data.get("elements", [])]
    records = [r for r in records if r["name"]]
    save(conn, records)
    print(f"  {category} / {bbox_name or state or 'file'}: {len(records)} businesses saved")
    return len(records)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--category", default="coffee_shop", help="a key in config.CATEGORIES, or 'all'")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--bbox", help="a key in config.PILOT_BBOX, e.g. sydney")
    g.add_argument("--state", help="NSW, VIC... or 'all'")
    p.add_argument("--from-file", help="load a saved Overpass JSON instead of calling the API")
    args = p.parse_args()
    if not args.from_file:
        config.require_contact_email()

    conn = db.connect(config.DB_PATH)
    cats = list(config.CATEGORIES) if args.category == "all" else [args.category]

    if args.from_file:
        with open(args.from_file) as f:
            raw = json.load(f)
        for c in cats:
            collect(conn, c, state=args.state, raw_json=raw)
        return

    if args.state:
        states = list(config.REGIONS) if args.state == "all" else [args.state]
        jobs = [(c, None, s) for s in states for c in cats]
    else:
        jobs = [(c, args.bbox or "sydney", None) for c in cats]

    for i, (c, b, s) in enumerate(jobs):
        collect(conn, c, bbox_name=b, state=s)
        if i < len(jobs) - 1:
            time.sleep(config.PAUSE_BETWEEN_QUERIES)


if __name__ == "__main__":
    main()
