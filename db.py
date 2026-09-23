"""
Local SQLite database. The schema is kept simple so it can be copied to
Supabase (Postgres) later with minimal changes.

Tables:
  businesses  one row per business per source (OSM, Google, ABR...)
  contacts    socials, emails, phones found for a business (many per business)
  people      staff / decision makers (filled later from Apollo, Hunter, manual)
"""
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- 'osm', 'google', 'abr'
    source_id     TEXT NOT NULL,          -- id inside that source
    name          TEXT,
    category      TEXT,
    subcategory   TEXT,                   -- raw tag, e.g. amenity=cafe
    cuisine       TEXT,
    brand         TEXT,
    street        TEXT,
    suburb        TEXT,
    state         TEXT,
    postcode      TEXT,
    lat           REAL,
    lon           REAL,
    phone         TEXT,
    phone_norm    TEXT,
    website       TEXT,
    email         TEXT,
    opening_hours TEXT,
    abn           TEXT,
    fingerprint   TEXT,                   -- used to spot the same business across sources
    enrich_status TEXT DEFAULT 'pending', -- pending / done / no_website / error / blocked
    enrich_note   TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    kind         TEXT NOT NULL,           -- instagram, facebook, linkedin, tiktok, email, phone
    value        TEXT NOT NULL,
    found_on     TEXT,                    -- page URL or 'osm_tag'
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (business_id, kind, value)
);

CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    full_name    TEXT,
    role         TEXT,
    email        TEXT,
    linkedin     TEXT,
    source       TEXT,                    -- apollo, hunter, manual
    notes        TEXT,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_biz_fingerprint ON businesses(fingerprint);
CREATE INDEX IF NOT EXISTS idx_biz_phone ON businesses(phone_norm);
CREATE INDEX IF NOT EXISTS idx_biz_status ON businesses(enrich_status);
CREATE INDEX IF NOT EXISTS idx_biz_category ON businesses(category, state);
"""

BUSINESS_FIELDS = [
    "source", "source_id", "name", "category", "subcategory", "cuisine", "brand",
    "street", "suburb", "state", "postcode", "lat", "lon", "phone", "phone_norm", "website",
    "email", "opening_hours", "abn", "fingerprint",
]


# Columns added after the first version. Existing databases get them automatically.
MIGRATIONS = {
    "businesses": [
        ("phone_norm", "TEXT"),
        ("legal_name", "TEXT"),       # from ABR
        ("abn_status", "TEXT"),       # Active / Cancelled
        ("abr_score", "INTEGER"),     # how confident the name match was
        ("rating", "REAL"),           # from Google exports
        ("review_count", "INTEGER"),
        ("group_id", "INTEGER"),      # same real-world business across sources (merge.py)
    ],
    "people": [("domain", "TEXT"), ("confidence", "INTEGER")],
}


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # older databases: add missing columns before indexes that use them
    for table, cols in MIGRATIONS.items():
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.executescript(
        "CREATE INDEX IF NOT EXISTS idx_biz_group ON businesses(group_id);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_people_unique ON people(business_id, email);"
    )
    conn.commit()
    return conn


def normalise_phone(raw):
    """Turn any Australian format into +61XXXXXXXXX so the same number always matches."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("61"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 9:
        return "+61" + digits
    if len(digits) == 10 and digits[:4] in ("1300", "1800"):
        return digits
    return digits or None


def clean_name(name):
    clean = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return re.sub(r"(ptyltd|pty|ltd|cafe|coffee|restaurant)$", "", clean)


def make_fingerprint(name, lat, lon, phone=None):
    """Same cleaned name within roughly 100m. Phone is matched separately in merge.py."""
    loc = f"{round(lat, 3)},{round(lon, 3)}" if lat is not None and lon is not None else "noloc"
    return f"{clean_name(name)}|{loc}"


def upsert_business(conn, rec):
    rec = {k: rec.get(k) for k in BUSINESS_FIELDS}
    rec["phone_norm"] = normalise_phone(rec["phone"])
    rec["fingerprint"] = make_fingerprint(rec["name"], rec["lat"], rec["lon"], rec["phone"])
    cols = ", ".join(BUSINESS_FIELDS)
    marks = ", ".join("?" for _ in BUSINESS_FIELDS)
    # keep the first category a business was found under; refresh everything else
    updates = ", ".join(
        "category=COALESCE(businesses.category, excluded.category)" if c == "category" else f"{c}=excluded.{c}"
        for c in BUSINESS_FIELDS if c not in ("source", "source_id")
    )
    conn.execute(
        f"INSERT INTO businesses ({cols}) VALUES ({marks}) "
        f"ON CONFLICT(source, source_id) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP",
        [rec[c] for c in BUSINESS_FIELDS],
    )
    row = conn.execute(
        "SELECT id FROM businesses WHERE source=? AND source_id=?",
        (rec["source"], rec["source_id"]),
    ).fetchone()
    return row["id"]


def add_contact(conn, business_id, kind, value, found_on):
    if kind == "phone":
        value = normalise_phone(value) or value
    conn.execute(
        "INSERT OR IGNORE INTO contacts (business_id, kind, value, found_on) VALUES (?, ?, ?, ?)",
        (business_id, kind, value, found_on),
    )
