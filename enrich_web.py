"""
Stage 2: visit each business website and pull out Instagram, Facebook,
LinkedIn, TikTok, emails and phone numbers.

It checks the homepage first, then common contact/about pages only if
something is still missing. It respects robots.txt.

Usage:
  python3 enrich_web.py              (all pending businesses)
  python3 enrich_web.py --limit 50   (test on a small batch)
  python3 enrich_web.py --retry-errors
"""
import argparse
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

import config
import db

# Any link to a social network. db.social_url decides whether it is a real profile
# and turns it into one standard URL (junk like share buttons and Wix's own page is dropped there).
SOCIAL_PATTERNS = {
    kind: re.compile(rf"https?://(?:[a-z]{{1,3}}\.)?(?:{host})/[^\s\"'<>)]+", re.I)
    for kind, host in db.SOCIAL_HOST.items()
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_JUNK = re.compile(
    r"\.(png|jpe?g|gif|webp|svg)$|sentry|wixpress|example\.|domain\.com|@2x"
    r"|@(email|mysite|yoursite|yourdomain|website|company|wix|squarespace|shopify|godaddy)\.com$"
    r"|^(your|yourname|name|user|username|email|you|firstname|john|jane)(\.?(last)?name)?@",
    re.I,
)
# Free mail / ISP domains a small business may legitimately use as its main email
FREEMAIL = {"gmail.com", "outlook.com", "hotmail.com", "live.com", "live.com.au", "yahoo.com", "yahoo.com.au", "icloud.com",
            "me.com", "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au", "tpg.com.au", "internode.on.net"}
# Footer credits such as "Website by Pixel Agency": that agency's emails and socials are not the business's
CREDIT_RE = re.compile(r"(web\s?site|site|design(ed)?|developed|built|powered|made|created|web design)\s+(by|with)|web design", re.I)
# Australian phone numbers: 02 9123 4567, (02) 9123 4567, +61 2 9123 4567, 0412 345 678, 1300 123 456
PHONE_RE = re.compile(
    r"(?:\+61\s?|\b0)4\d{2}[\s\-]?\d{3}[\s\-]?\d{3}\b"            # mobiles
    r"|(?:\+61\s?\(?0?|\(?0)[2378]\)?[\s\-]?\d{4}[\s\-]?\d{4}\b"  # landlines
    r"|\b1[38]00[\s\-]?\d{3}[\s\-]?\d{3}\b"                        # 1300 / 1800
)

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


def site_domain(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def email_is_own(email, domain):
    """True if the email is at the business's own domain, or a free mailbox a small business might use."""
    at = email.rsplit("@", 1)[-1]
    return at in FREEMAIL or (domain and (at == domain or at.endswith("." + domain) or domain.endswith("." + at)))


def decode_cfemail(hexstr):
    """Cloudflare hides emails as hex (data-cfemail / #email-protection). Decode them."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def extract(html, page_url):
    found = {"instagram": set(), "facebook": set(), "linkedin": set(), "tiktok": set(), "email": set(), "phone": set()}
    soup = BeautifulSoup(html, "html.parser")
    own_domain = site_domain(page_url)

    anchors = soup.find_all("a")
    hrefs = [a.get("href", "") for a in anchors]
    blob = " ".join(hrefs) + " " + html

    # links next to "Website by ..." credits belong to the web designer
    credit_domains, credit_socials = set(), set()
    for a in anchors:
        href = a.get("href", "")
        if not href.startswith("http"):
            continue
        before = a.find_previous(string=True) or ""
        if before and a in before.parents:
            before = ""
        context = str(before)[-60:] + " " + a.get_text(" ", strip=True)
        if not CREDIT_RE.search(context):
            continue
        d = site_domain(href)
        if d and d != own_domain:
            credit_domains.add(d)
        for kind in SOCIAL_PATTERNS:
            url = db.social_url(kind, href) if SOCIAL_PATTERNS[kind].match(href) else None
            if url:
                credit_socials.add(url)

    for kind, pattern in SOCIAL_PATTERNS.items():
        for m in pattern.finditer(blob):
            url = db.social_url(kind, m.group(0))
            if url and url not in credit_socials:
                found[kind].add(url)

    for h in hrefs:
        low = h.lower()
        if low.startswith("mailto:"):
            for addr in unquote(h[7:]).split("?")[0].split(","):
                found["email"].add(addr.strip().lower())
        elif low.startswith("tel:"):
            found["phone"].add(re.sub(r"[^\d+]", "", unquote(h[4:])))
        elif "/cdn-cgi/l/email-protection#" in low:
            found["email"].add(decode_cfemail(h.split("#", 1)[1]).lower())
    for el in soup.select("[data-cfemail]"):
        found["email"].add(decode_cfemail(el["data-cfemail"]).lower())

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    for e in EMAIL_RE.findall(text):
        found["email"].add(e.lower())
    for ph in PHONE_RE.findall(text):
        found["phone"].add(re.sub(r"[^\d+]", "", ph))

    found["email"] = {
        e for e in found["email"]
        if EMAIL_RE.fullmatch(e) and not EMAIL_JUNK.search(e) and e.rsplit("@", 1)[-1] not in credit_domains
    }
    return found


SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "linktr.ee")


def first_website(website):
    """OSM sometimes holds several sites separated by ';' or spaces. Use the first."""
    site = re.split(r"[;\s,]+", (website or "").strip())[0]
    if site and "://" not in site:
        site = "https://" + site
    return site


def fetch_homepage(website):
    """Fetch the homepage. Many small sites have no working https, so fall back to http."""
    try:
        return fetch(website)
    except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
        if not website.startswith("https://"):
            raise
        return fetch("http://" + website[len("https://"):])


def enrich_one(business_id, website):
    """Returns (business_id, status, note, list_of_contacts, seconds). No DB access here (thread safe)."""
    start = time.monotonic()
    status, note, contacts = enrich_site(website)
    return business_id, status, note, contacts, time.monotonic() - start


def enrich_site(website):
    try:
        website = first_website(website)
        host = urlparse(website).netloc.lower()
        if "." not in host:
            return "no_website", f"not a real website: {website}", []
        if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
            f = extract(f'<a href="{website}"></a>', website)
            contacts = [(k, v, "website_field") for k in SOCIAL_PATTERNS for v in f[k]]
            return "social_only", "website field is a social page", contacts

        if not allowed_by_robots(website):
            return "blocked", "robots.txt disallows", []

        html, final_url = fetch_homepage(website)
        if html is None:
            return "error", "homepage not reachable", []

        domain = site_domain(final_url)
        pages = [(final_url, extract(html, final_url))]

        def missing(f):
            has_own_email = any(email_is_own(e, domain) for e in f["email"])
            return not has_own_email or not (f["instagram"] or f["facebook"])

        merged = {k: set(v) for k, v in pages[0][1].items()}
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
                    contacts.append((kind, v, page_url))
        return "done", f"{len(pages)} page(s) checked", contacts

    except Exception as exc:  # never let one bad site stop the batch
        return "error", f"{type(exc).__name__}: {str(exc)[:180]}", []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--retry-errors", action="store_true")
    args = p.parse_args()
    config.require_contact_email()

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
    timings = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = [pool.submit(enrich_one, row["id"], row["website"]) for row in todo]
        for fut in as_completed(futures):
            biz_id, status, note, contacts, seconds = fut.result()
            timings.append(seconds)
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
    elapsed = time.monotonic() - started
    print(f"Enrichment finished in {elapsed:.0f}s")
    if timings:
        timings.sort()
        print(f"  per site: average {sum(timings) / len(timings):.1f}s, median {timings[len(timings) // 2]:.1f}s, "
              f"slowest {timings[-1]:.1f}s ({config.MAX_WORKERS} sites at a time)")


if __name__ == "__main__":
    main()
