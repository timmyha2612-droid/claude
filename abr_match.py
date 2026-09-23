"""
Match businesses to the Australian Business Register (free).
Adds ABN, legal entity name and whether the ABN is active.

One-time setup: register for a free GUID at
  https://abr.business.gov.au/Tools/WebServices
then set it before running:
  Mac/Linux:  export ABR_GUID=your-guid
  Windows:    set ABR_GUID=your-guid

Usage:
  python abr_match.py --category food_wholesaler      (start with B2B targets)
  python abr_match.py --limit 100
  python abr_match.py                                 (everything not yet checked)
"""
import argparse
import json
import os
import re
import sys
import time

import requests

import config
import db

BASE = "https://abr.business.gov.au/json/"
MIN_SCORE = 90        # ABR relevance score 0-100. Exact registered names score 95+.
PAUSE = 0.4           # seconds between calls


def call(endpoint, **params):
    params.update({"guid": os.environ["ABR_GUID"], "callback": "cb"})
    r = requests.get(BASE + endpoint, params=params, timeout=20)
    r.raise_for_status()
    body = r.text.strip()
    m = re.match(r"^\w+\((.*)\)\s*;?$", body, re.S)   # response is wrapped as cb({...})
    return json.loads(m.group(1) if m else body)


def is_active(status):
    return str(status).lower() in ("active", "0000000001")


def pick_best(candidates, state, postcode):
    best, best_score = None, -1
    for c in candidates:
        score = int(c.get("Score") or 0)
        if score < MIN_SCORE or not c.get("IsCurrent", True):
            continue
        if state and c.get("State") and c["State"] != state:
            continue                      # same name in another state is a different business
        bonus = 0
        if postcode and c.get("Postcode") == postcode:
            bonus += 5
        if is_active(c.get("AbnStatus")):
            bonus += 3
        if score + bonus > best_score:
            best, best_score = c, score + bonus
    return best


def match_one(name, state, postcode):
    data = call("MatchingNames.aspx", name=name, maxResults=10)
    if data.get("Message"):
        raise RuntimeError(data["Message"])
    best = pick_best(data.get("Names") or [], state, postcode)
    if not best:
        return None
    details = call("AbnDetails.aspx", abn=best["Abn"])
    return {
        "abn": best["Abn"],
        "legal_name": details.get("EntityName") or best.get("Name"),
        "abn_status": details.get("AbnStatus") or ("Active" if is_active(best.get("AbnStatus")) else best.get("AbnStatus")),
        "abr_score": int(best.get("Score") or 0),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--category")
    p.add_argument("--limit", type=int)
    args = p.parse_args()

    if not os.environ.get("ABR_GUID"):
        sys.exit("Set ABR_GUID first (free from https://abr.business.gov.au/Tools/WebServices)")

    conn = db.connect(config.DB_PATH)
    q = "SELECT id, name, state, postcode FROM businesses WHERE abn IS NULL AND abr_score IS NULL"
    params = []
    if args.category:
        q += " AND category=?"
        params.append(args.category)
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    todo = conn.execute(q, params).fetchall()
    print(f"Checking {len(todo)} businesses against the ABR")

    matched = 0
    for i, r in enumerate(todo, 1):
        try:
            res = match_one(r["name"], r["state"], r["postcode"])
        except Exception as exc:
            print(f"  {r['name']}: {exc}")
            if "guid" in str(exc).lower():
                sys.exit("ABR rejected the GUID. Check it and try again.")
            time.sleep(5)
            continue
        if res:
            conn.execute(
                "UPDATE businesses SET abn=?, legal_name=?, abn_status=?, abr_score=? WHERE id=?",
                (res["abn"], res["legal_name"], res["abn_status"], res["abr_score"], r["id"]),
            )
            matched += 1
        else:
            conn.execute("UPDATE businesses SET abr_score=-1 WHERE id=?", (r["id"],))  # checked, no match
        if i % 25 == 0:
            conn.commit()
            print(f"  {i}/{len(todo)} checked, {matched} matched")
        time.sleep(PAUSE)
    conn.commit()
    print(f"Done: {matched}/{len(todo)} matched to an ABN")


if __name__ == "__main__":
    main()
