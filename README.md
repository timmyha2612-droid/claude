# Food Business Scraper

Collects Australian food businesses, enriches them, and stores everything in
one local database (`businesses.db`). Every step is safe to re-run.

## Setup (once)

1. Install Python 3.10+ from python.org
2. In this folder run: `python3 -m pip install -r requirements.txt`
   (on a Mac the command is always `python3`, never `python`)
3. In `config.py`, replace `you@example.com` in `CONTACT_EMAIL` with your real email
4. Optional free keys:
   - ABR GUID: https://abr.business.gov.au/Tools/WebServices
   - Hunter API key: https://hunter.io (free plan)

## The pipeline

| # | Command | What it does | Cost |
|---|---|---|---|
| 1 | `python3 collect_osm.py --category coffee_shop --bbox sydney` | Businesses from OpenStreetMap | Free |
| 2 | `python3 import_places.py export.csv --category coffee_shop` | Add Google Maps data bought from Outscraper / Apify | Paid (optional) |
| 3 | `python3 merge.py` | Link the same business across sources | Free |
| 4 | `python3 enrich_web.py` | Socials, emails, phones from each website | Free |
| 5 | `python3 abr_match.py --category food_wholesaler` | ABN, legal name, active status | Free (GUID) |
| 6 | `python3 find_people.py hunter --category food_wholesaler --limit 20` | Named staff from website domain | Free tier |
| 6b | `python3 find_people.py apollo apollo_export.csv` | Import people exported from Apollo | Free tier |
| 7 | `python3 export.py` | Report + `businesses_export.csv` + `people_export.csv` | Free |

Setting keys: Mac/Linux `export ABR_GUID=...`, Windows `set ABR_GUID=...` (same for HUNTER_API_KEY).

## Pilot first

    python3 collect_osm.py --category coffee_shop --bbox sydney
    python3 merge.py
    python3 enrich_web.py --limit 50
    python3 export.py --out sydney_cafes.csv

## Scale up

    python3 collect_osm.py --category all --state NSW
    python3 collect_osm.py --category all --state all     # Australia-wide, run overnight

After adding any new data, re-run merge.py, then enrich_web.py (it only processes new rows).
`python3 enrich_web.py --retry-errors` retries failed websites.

## Categories

coffee_shop, restaurant, grocery, food_retailer, food_wholesaler, food_brand
(edit the OSM tags for each in config.py). OSM coverage of wholesalers and
brands is thin: those are the best categories to buy Google data for.

## How duplicates are handled

Nothing is deleted. Rows that are the same business get the same group_id,
and the export shows one row per business with contacts combined from all
sources. Chains are protected: a shared website or 1300/1800 number only
links rows within 150 m, so different stores stay separate.

## Viewing the data

Open `businesses.db` with "DB Browser for SQLite" (free), or open the CSVs in Excel.
Tables: businesses, contacts, people, people_lookups.
