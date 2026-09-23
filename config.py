"""
Settings for the scraper. Edit this file to change what and where you collect.
"""

# Each category maps to a list of OpenStreetMap tag filters.
# Format: (key, value). value=None means "any value for this key".
CATEGORIES = {
    "coffee_shop": [("amenity", "cafe")],
    "restaurant": [("amenity", "restaurant"), ("amenity", "fast_food")],
    "grocery": [
        ("shop", "supermarket"),
        ("shop", "convenience"),
        ("shop", "greengrocer"),
        ("shop", "deli"),
    ],
    "food_retailer": [
        ("shop", "bakery"),
        ("shop", "butcher"),
        ("shop", "seafood"),
        ("shop", "beverages"),
        ("shop", "confectionery"),
        ("shop", "food"),
    ],
    # OSM coverage is weak for these two. Pair them with ABR / paid Google data later.
    "food_wholesaler": [("shop", "wholesale"), ("wholesale", None)],
    "food_brand": [("industrial", "food_industry"), ("craft", "brewery"), ("craft", "winery")],
}

# Regions. Each state is looked up by its official ISO code, so you can
# go Australia-wide just by listing all of them.
REGIONS = {
    "NSW": "AU-NSW",
    "VIC": "AU-VIC",
    "QLD": "AU-QLD",
    "WA": "AU-WA",
    "SA": "AU-SA",
    "TAS": "AU-TAS",
    "ACT": "AU-ACT",
    "NT": "AU-NT",
}

# A smaller box for pilots: (south, west, north, east). Greater Sydney.
PILOT_BBOX = {"sydney": (-34.17, 150.52, -33.42, 151.35)}

# The main public server first, then public mirrors used if it refuses or is down
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_TIMEOUT = 300          # seconds the server may spend on a query
PAUSE_BETWEEN_QUERIES = 10      # be polite to the free public server

# Website enrichment
# Put YOUR email after "contact:". OpenStreetMap and website owners use it to reach you
# instead of blocking you. The scripts refuse to run while it still says you@example.com.
CONTACT_EMAIL = "you@example.com"
USER_AGENT = f"Mozilla/5.0 (compatible; BusinessDirectoryBot/0.1; contact: {CONTACT_EMAIL})"
# OpenStreetMap's server refuses (error 406) anything that looks like a browser or a bare script.
# It wants a plain app name plus a contact.
OSM_USER_AGENT = f"AusFoodBusinessDirectory/0.1 (contact: {CONTACT_EMAIL})"
REQUEST_TIMEOUT = 15
MAX_WORKERS = 8                 # parallel website fetches
EXTRA_PAGES = ["/contact", "/contact-us", "/about", "/about-us"]

DB_PATH = "businesses.db"


def require_contact_email():
    if "example.com" in USER_AGENT or "@" not in USER_AGENT:
        raise SystemExit(
            "Stop: open config.py and replace you@example.com in CONTACT_EMAIL with your real email, then run again."
        )
