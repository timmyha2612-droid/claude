"""
Stage 2: visit each business website and pull out Instagram, Facebook,
LinkedIn, TikTok, emails and phone numbers.

It checks the homepage first, then common contact/about pages only if
something is still missing. It respects robots.txt.

Usage:
  python enrich_web.py              (all pending businesses)
  python enrich_web.py --limit 50   (test on a small batch)
  python enrich_web.py --retry-errors
"""
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

import config
import db

SOCIAL_PATTERNS = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?", re.I),
    "facebook": re.compile(r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9.\-]{2,80})/?", re.I),
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|in)/([A-Za-z0-9\-_%]{2,100})/?", re.I),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})/?", re.I),
}
# Paths that look like profiles but are not
SOCIAL_JUNK = {
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "tv"},
    "facebook": {"sharer", "sharer.php", "share", "plugins", "tr", "dialog", "groups", "events", "watch", "profile.php", "pages", "login", "home.php"},
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_JUNK = re.compile(r"\.(png|jpe?g|gif|webp|svg)$|sentry|wixpress|example\.com|domain\.com|@2x", re.I)
# Australian phone numbers: 02 9123 4567, (02) 9123 4567, +61 2 9123 4567, 0412 345 678, 1300 123 456
PHONE_RE = re.compile(
    r"(?:\+61\s?|\b0)4\d{2}[\s\-]?\d{3}[\s\-]?\d{3}\b"            # mobiles
    r"|(?:\+61\s?\(?0?|\(?0)[2378]\)?[\s\-]?\d{4}[\s\-]?\d{4}\b"  # landlines
    r"|\b1[38]00[\s\-]?\d{3}[\s\-]?\d{3}\b"                        # 1300 / 1800
)

import threading

_local = threading.local()


def get_session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers.update({"User-Agent": config.USER_AGENT})
    return _local.s



def allowed_by_robots(url):
    parts = urlparse(url)
    rp = RobotFileParser()
    try:
        r = get_session().get(f"{parts.scheme}://{parts.netloc}/robots.txt", timeout=8)
        if r.status_code >= 400:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(config.USER_AGENT, url)
    except requests.RequestException:
        return True


def fetch(url):
    r = get_session().get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
    if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
        return None, r.url
    return r.text[:2_000_000], r.url


def extract(html, page_url):
    found = {"instagram": set(), "facebook": set(), "linkedin": set(), "tiktok": set(), "email": set(), "phone": set()}
    soup = BeautifulSoup(html, "html.parser")

    hrefs = [a.get("href", "") for a in soup.find_all("a")]
    blob = " ".join(hrefs) + " " + html

    for kind, pattern in SOCIAL_PATTERNS.items():
        for m in pattern.finditer(blob):
            if kind == "linkedin":
                handle = f"{m.group(1)}/{m.group(2)}"
            else:
                handle = m.group(1).rstrip(".")
            if handle.lower() in SOCIAL_JUNK.get(kind, set()):
                continue
            found[kind].add(handle)

    for h in hrefs:
        if h.lower().startswith("mailto:"):
            found["email"].add(h[7:].split("?")[0].strip().lower())
        if h.lower().startswith("tel:"):
            found["phone"].add(re.sub(r"[^\d+]", "", h[4:]))

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    for e in EMAIL_RE.findall(text):
        found["email"].add(e.lower())
    for ph in PHONE_RE.findall(text):
        found["phone"].add(re.sub(r"[^\d+]", "", ph))

    found["email"] = {e for e in found["email"] if not EMAIL_JUNK.search(e)}
    return found


def normalise_social(kind, handle):
    base = {
        "instagram": "https://instagram.com/",
        "facebook": "https://facebook.com/",
        "linkedin": "https://linkedin.com/",
        "tiktok": "https://tiktok.com/@",
    }[kind]
    return base + handle


SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "linktr.ee")


def enrich_one(business_id, website):
    """Returns (business_id, status, note, list_of_contacts). No DB access here (thread safe)."""
    try:
        host = urlparse(website).netloc.lower()
        if any(host.endswith(h) for h in SOCIAL_HOSTS):
            f = extract(f'<a href="{website}"></a>', website)
            contacts = [(k, normalise_social(k, v), "website_field") for k in SOCIAL_PATTERNS for v in f[k]]
            return business_id, "social_only", "website field is a social page", contacts

        if not allowed_by_robots(website):
            return business_id, "blocked", "robots.txt disallows", []

        html, final_url = fetch(website)
        if html is None:
            return business_id, "error", "homepage not reachable", []

        pages = [(final_url, extract(html, final_url))]

        def missing(f):
            return not f["email"] or not (f["instagram"] or f["facebook"])

        merged = pages[0][1]
        for path in config.EXTRA_PAGES:
            if not missing(merged):
                break
            url = urljoin(final_url, path)
            try:
                sub_html, sub_url = fetch(url)
            except requests.RequestException:
                continue
            if sub_html:
                f = extract(sub_html, sub_url)
                pages.append((sub_url, f))
                for k in merged:
                    merged[k] |= f[k]

        contacts = []
        for page_url, f in pages:
            for kind, values in f.items():
                for v in values:
                    value = normalise_social(kind, v) if kind in SOCIAL_PATTERNS else v
                    contacts.append((kind, value, page_url))
        return business_id, "done", f"{len(pages)} page(s) checked", contacts

    except Exception as exc:  # never let one bad site stop the batch
        return business_id, "error", f"{type(exc).__name__}: {str(exc)[:180]}", []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--retry-errors", action="store_true")
    args = p.parse_args()

    conn = db.connect(config.DB_PATH)

    conn.execute(
        "UPDATE businesses SET enrich_status='no_website' "
        "WHERE (website IS NULL OR website='') AND enrich_status='pending'"
    )
    conn.commit()

    statuses = ("pending", "error") if args.retry_errors else ("pending",)
    q = f"SELECT id, website FROM businesses WHERE enrich_status IN ({','.join('?' * len(statuses))})"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    todo = conn.execute(q, statuses).fetchall()
    print(f"Enriching {len(todo)} websites with {config.MAX_WORKERS} workers")

    done = 0
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = [pool.submit(enrich_one, row["id"], row["website"]) for row in todo]
        for fut in as_completed(futures):
            biz_id, status, note, contacts = fut.result()
            for kind, value, found_on in contacts:
                db.add_contact(conn, biz_id, kind, value, found_on)
            conn.execute(
                "UPDATE businesses SET enrich_status=?, enrich_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, note, biz_id),
            )
            done += 1
            if done % 25 == 0:
                conn.commit()
                print(f"  {done}/{len(todo)}")
    conn.commit()
    print("Enrichment finished")


if __name__ == "__main__":
    main()
