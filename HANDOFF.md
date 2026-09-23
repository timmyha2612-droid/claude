# Handoff brief

Read this first, then README.md.

## What this project is

A pipeline that builds a database of Australian food businesses (coffee shops,
restaurants, grocery, food retailers, food wholesalers, food brands) with their
contact details and the people who work there. It is for B2B outreach for an
Australian food import business, so wholesalers, grocery and food brands matter
more than individual cafes.

## Current state

All code is written and passes an end-to-end test against simulated data.
None of it has been run against the real internet yet. The owner is a
non-developer on a Mac, running commands by copy and paste.

Not yet done:
- No real run has happened, so `businesses.db` may not exist yet
- ABR registration is pending, so skip `abr_match.py` entirely for now
- No Hunter or Apollo key yet, so skip `find_people.py` for now

## Your job

1. Check the setup: Python 3.10+, `pip install -r requirements.txt`, and that
   `USER_AGENT` in `config.py` has a real email rather than the placeholder.
2. Run the Sydney coffee shop pilot:

       python3 collect_osm.py --category coffee_shop --bbox sydney
       python3 merge.py
       python3 enrich_web.py --limit 50
       python3 export.py --out sydney_cafes.csv

3. Fix anything that breaks, and tell the owner in plain language what was
   wrong and what you changed.
4. Report back: how many businesses were found, how many have a website, how
   many got an Instagram or email, and how long enrichment took per site.
5. Spot-check about 10 rows of the CSV. Are the businesses real, and do the
   Instagram links and emails belong to that business rather than to a web
   designer, a delivery platform, or a template?

Do not run `enrich_web.py` without `--limit` until the owner has seen the
first 50 results and agreed.

## Things already checked, do not redo

These bugs were found and fixed already. If you see them, something regressed:
- Duplicate matching must not depend on phone numbers being present
- Page scripts must be stripped before scanning text for emails and phones
- One failing website must not stop the batch
- Each thread needs its own requests session
- A website field pointing at Facebook or Instagram is saved as a social,
  not scraped
- A business matching two categories keeps the first one
- Phones are stored normalised as +61...
- Chains sharing a website or 1300 number must not merge unless within 150 m

## Design rules to respect

- The database is the single source of truth. Scripts read and write it, never
  pass data to each other.
- Every stage must stay re-runnable. Rows carry a status, and a stage only
  processes rows that still need it. Never reprocess or duplicate.
- Nothing is deleted on merge. Duplicates are linked with `group_id` and the
  originals stay.
- Free tiers are limited, so never spend an API credit on a domain already
  looked up.
- Be polite to OpenStreetMap: keep the delay between queries and a real
  contact email in the user agent.

## Known weak spots worth your attention

- OSM coverage of food wholesalers and food brands is thin. Confirm how thin,
  because that decides whether paid Google data is worth buying.
- `import_places.py` maps the column names used by Outscraper and Apify, but
  has never seen a real export from either. Check the mapping printout when a
  real file arrives.
- `abr_match.py` and `find_people.py` were only tested against simulated API
  responses.

## Next steps after the pilot works

1. Full enrichment run, then all categories for NSW
2. Decide on buying Google data for wholesalers and brands
3. ABR matching once the GUID arrives
4. People lookups, B2B categories first
5. Consider moving the database to Supabase if it grows past a few tens of
   thousands of rows
