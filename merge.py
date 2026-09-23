"""
Link rows that are the same real-world business (for example the OSM row
and the Google row for one cafe). Each linked set gets the same group_id.
Nothing is deleted: you can always see every source.

Two rows are linked when ANY of these is true:
  1. very similar name AND within 150 m
  2. same local phone number AND within 1 km   (1300/1800 numbers are ignored:
     chains share them across every store)
  3. same website domain AND within 150 m      (chains share one domain too)

Usage:
  python3 merge.py
"""
import math
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlparse

import config
import db

NAME_DISTANCE_M = 150
PHONE_DISTANCE_M = 1000
DOMAIN_DISTANCE_M = 150
NAME_SIMILARITY = 0.85
CELL = 0.01  # ~1.1 km grid cells so we only compare nearby rows


def metres(a, b):
    if a["lat"] is None or b["lat"] is None:
        return None
    dlat = math.radians(b["lat"] - a["lat"])
    dlon = math.radians(b["lon"] - a["lon"])
    x = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a["lat"])) * math.cos(math.radians(b["lat"])) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(x))


def domain(url):
    if not url:
        return None
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if any(s in host for s in ("facebook.", "instagram.", "linktr.ee", "google.", "ubereats", "doordash", "menulog")):
        return None
    return host or None


class Groups:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def same_business(a, b):
    d = metres(a, b)
    if d is None:
        return False
    na, nb = a["cname"], b["cname"]
    if na and nb and d <= NAME_DISTANCE_M:
        if na == nb or na in nb or nb in na or SequenceMatcher(None, na, nb).ratio() >= NAME_SIMILARITY:
            return True
    pa, pb = a["phone_norm"], b["phone_norm"]
    if pa and pa == pb and pa.startswith("+61") and d <= PHONE_DISTANCE_M:
        return True
    if a["domain"] and a["domain"] == b["domain"] and d <= DOMAIN_DISTANCE_M:
        return True
    return False


def main():
    conn = db.connect(config.DB_PATH)
    fixed = db.tidy_contacts(conn)
    if fixed:
        print(f"Tidied {fixed} contacts into the standard format")
    rows = []
    for r in conn.execute("SELECT id, name, lat, lon, phone_norm, website FROM businesses"):
        rows.append({
            "id": r["id"], "lat": r["lat"], "lon": r["lon"], "phone_norm": r["phone_norm"],
            "cname": db.clean_name(r["name"]), "domain": domain(r["website"]),
        })

    grid = defaultdict(list)
    for r in rows:
        if r["lat"] is not None:
            grid[(int(r["lat"] // CELL), int(r["lon"] // CELL))].append(r)

    g = Groups()
    for r in rows:
        g.find(r["id"])
    for (cx, cy), cell in grid.items():
        neighbours = [n for dx in (-1, 0, 1) for dy in (-1, 0, 1) for n in grid.get((cx + dx, cy + dy), [])]
        for a in cell:
            for b in neighbours:
                if a["id"] < b["id"] and same_business(a, b):
                    g.join(a["id"], b["id"])

    conn.executemany("UPDATE businesses SET group_id=? WHERE id=?", [(g.find(r["id"]), r["id"]) for r in rows])
    conn.commit()

    total = len(rows)
    unique = conn.execute("SELECT COUNT(DISTINCT group_id) FROM businesses").fetchone()[0]
    print(f"{total} rows across all sources -> {unique} unique businesses ({total - unique} duplicates linked)")


if __name__ == "__main__":
    main()
