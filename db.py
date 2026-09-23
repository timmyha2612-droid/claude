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
from urllib.parse import unquote

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


# ---------------------------------------------------------------------------
# Contact cleaning. Every contact from every source (OSM tags, websites, Google
# exports) goes through here, so the same profile is always stored the same way.

SOCIAL_KINDS = ("instagram", "facebook", "linkedin", "tiktok")
SOCIAL_BASE = {
    "instagram": "https://instagram.com/",
    "facebook": "https://facebook.com/",
    "linkedin": "https://linkedin.com/",
    "tiktok": "https://tiktok.com/@",
}
SOCIAL_HOST = {
    "instagram": r"instagram\.com|instagr\.am",
    "facebook": r"facebook\.com|fb\.com",
    "linkedin": r"linkedin\.com",
    "tiktok": r"tiktok\.com",
}
# Paths that look like profiles but are not
SOCIAL_JUNK = {
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "about", "legal", "developer", "direct", "web"},
    "facebook": {"sharer", "sharer.php", "share", "share.php", "plugins", "tr", "dialog", "groups", "events", "watch",
                 "login", "login.php", "home.php", "business", "policy.php", "privacy", "help", "legal", "ads",
                 "photo.php", "hashtag", "l.php", "flx", "fbml", "profile.php", "pages", "people", "pg", "p"},
}
# Accounts of website builders, delivery apps and the social networks themselves.
# Their links sit in template footers and "order on" buttons, not the business's own.
PLATFORM_HANDLES = {
    "wix", "wixcom", "squarespace", "shopify", "wordpress", "wordpressdotcom", "weebly", "godaddy", "webflow",
    "ubereats", "ubereats_aus", "ubereatsau", "ubereats_au", "doordash", "doordash_au", "doordashau", "menulog",
    "deliveroo", "opentable", "opentableau", "mryum", "mryum_au", "hungrypanda", "square", "squareup",
    "lightspeedhq", "instagram", "facebook", "meta", "tiktok", "linkedin", "google", "youtube", "twitter",
}
# Looks like a version number, year or file rather than a profile (facebook.com/2008/fbml, /v12.0/dialog, /embed.js)
JUNK_HANDLE_RE = re.compile(r"^(\d{1,6}|v\d+(\.\d+)*)$|\.(js|php|css|png|jpe?g|gif|svg)$", re.I)
EMAIL_FULL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}")
VALID_PHONE_RE = re.compile(r"\+61[2-478]\d{8}|1[38]00\d{6}|13\d{4}")


def social_url(kind, raw):
    """Turn any way of writing a profile (full URL, bare handle, @handle, mobile link...)
    into one standard URL. Returns None if it is not a real profile."""
    v = unquote((raw or "").strip()).replace("#!/", "")
    if not v or EMAIL_FULL_RE.fullmatch(v.lower()):
        return None
    m = re.search(rf"(?:^|[/.])(?:{SOCIAL_HOST[kind]})/+(.*)", v, re.I)
    if m:
        path = m.group(1)
    elif "/" in v or re.search(r"\.(com|net|org|au|io|co)\b", v, re.I):
        return None                                   # some other website, not a handle
    else:
        path = v
    if kind == "facebook":
        pid = re.search(r"profile\.php\?id=(\d+)", path)
        if pid:
            return SOCIAL_BASE[kind] + pid.group(1)
    path = re.split(r"[?#]", path)[0]
    # "facebook.com/www.facebook.com/123" style double hosts
    path = re.sub(rf"^(?:[a-z]+\.)?(?:{SOCIAL_HOST[kind]})/+", "", path, flags=re.I)
    parts = [x for x in path.split("/") if x]
    if not parts:
        return None
    if kind == "linkedin":
        if len(parts) < 2 or parts[0].lower() not in ("company", "in", "school", "showcase"):
            return None
        return SOCIAL_BASE[kind] + f"{parts[0]}/{parts[1]}".lower()
    if kind == "facebook" and parts[0].lower() in ("pages", "people", "p", "pg") and len(parts) > 1:
        # facebook.com/people/Name/12345 and /pages/Name/12345 -> the page id; /pg/name -> name
        ids = [x for x in parts[1:] if x.isdigit()]
        tail = re.search(r"(\d{8,})$", parts[1])
        parts = [ids[0] if ids else (tail.group(1) if tail else parts[1])]
    handle = parts[0].lstrip("@").rstrip(".").lower()
    if (not handle or handle in SOCIAL_JUNK.get(kind, set()) or handle in PLATFORM_HANDLES
            or JUNK_HANDLE_RE.search(handle) or not re.fullmatch(r"[\w.\-]{2,100}", handle)):
        return None
    return SOCIAL_BASE[kind] + handle


def clean_email(raw):
    e = unquote((raw or "").strip()).lower().strip(".")
    if e.startswith("mailto:"):
        e = e[7:]
    local, _, dom = e.partition("@")
    if dom.startswith("www."):
        dom = dom[4:]
    e = f"{local}@{dom}"
    return e if EMAIL_FULL_RE.fullmatch(e) else None


def clean_contact(kind, value):
    """Returns (kind, value) to store, or None to skip. A social field holding an email is stored as an email."""
    if kind in SOCIAL_KINDS:
        url = social_url(kind, value)
        if url:
            return kind, url
        # an Instagram link typed into the Facebook field (and so on): file it under the right network
        for other in SOCIAL_KINDS:
            if other != kind and re.search(rf"(?:^|[/.])(?:{SOCIAL_HOST[other]})/", value or "", re.I):
                url = social_url(other, value)
                return (other, url) if url else None
        e = clean_email(value)
        return ("email", e) if e else None
    if kind == "email":
        e = clean_email(value)
        return ("email", e) if e else None
    if kind == "phone":
        p = normalise_phone(value)
        return ("phone", p) if p and VALID_PHONE_RE.fullmatch(p) else None
    return kind, value


def add_contact(conn, business_id, kind, value, found_on):
    cleaned = clean_contact(kind, value)
    if not cleaned:
        return
    kind, value = cleaned
    conn.execute(
        "INSERT OR IGNORE INTO contacts (business_id, kind, value, found_on) VALUES (?, ?, ?, ?)",
        (business_id, kind, value, found_on),
    )


def tidy_contacts(conn):
    """Bring contacts saved by older versions into the standard format. Safe to run any number of times."""
    rows = conn.execute("SELECT id, business_id, kind, value, found_on FROM contacts").fetchall()
    changed = 0
    for r in rows:
        cleaned = clean_contact(r["kind"], r["value"])
        if cleaned == (r["kind"], r["value"]):
            continue
        conn.execute("DELETE FROM contacts WHERE id=?", (r["id"],))
        if cleaned:
            add_contact(conn, r["business_id"], cleaned[0], cleaned[1], r["found_on"])
        changed += 1
    # name+developer@x.com next to name@x.com is the same inbox: keep the plain one
    for r in conn.execute("SELECT id, business_id, value FROM contacts WHERE kind='email' AND value LIKE '%+%@%'").fetchall():
        local, dom = r["value"].split("@", 1)
        plain = f"{local.split('+')[0]}@{dom}"
        if conn.execute("SELECT 1 FROM contacts WHERE business_id=? AND kind='email' AND value=?",
                        (r["business_id"], plain)).fetchone():
            conn.execute("DELETE FROM contacts WHERE id=?", (r["id"],))
            changed += 1
    conn.commit()
    return changed
