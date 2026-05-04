"""
Zillow Puerto Rico Scraper
Approach: curl_cffi (TLS fingerprint spoofing) + __NEXT_DATA__ extraction
"""

import json
import logging
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

from curl_cffi import requests as curl_requests

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DB_PATH      = "listings.db"
MAX_RETRIES  = 3
BACKOFF_BASE = 5
MAX_PAGES    = 20

HEADERS = {
    "accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language":           "en-US,en;q=0.9",
    "accept-encoding":           "gzip, deflate, br",
    "cache-control":             "no-cache",
    "pragma":                    "no-cache",
    "sec-ch-ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":          "?0",
    "sec-ch-ua-platform":        '"Windows"',
    "sec-fetch-dest":            "document",
    "sec-fetch-mode":            "navigate",
    "sec-fetch-site":            "none",
    "sec-fetch-user":            "?1",
    "upgrade-insecure-requests": "1",
    "user-agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

def build_search_url(page: int = 1) -> str:
    qs = {
        "pagination": {"currentPage": page},
        "isMapVisible": False,
        "filterState": {
            "beds":  {"min": 2},
            "con":   {"value": False},
            "gar":   {"value": False},
            "mf":    {"value": False},
            "land":  {"value": False},
            "tow":   {"value": False},
        },
        "isListVisible": True,
    }
    encoded = urllib.parse.quote(json.dumps(qs, separators=(",", ":")))
    if page == 1:
        return f"https://www.zillow.com/pr/?searchQueryState={encoded}"
    return f"https://www.zillow.com/pr/{page}_p/?searchQueryState={encoded}"


# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log"),
    ],
)
log = logging.getLogger(__name__)


# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            zpid           TEXT PRIMARY KEY,
            url            TEXT,
            listing_status TEXT,
            property_type  TEXT,
            latitude       REAL,
            longitude      REAL,
            price          INTEGER,
            bedrooms       INTEGER,
            bathrooms      REAL,
            living_area    INTEGER,
            address        TEXT,
            description    TEXT,
            photo_url      TEXT,
            data           TEXT,
            scraped_at     TEXT,
            status         TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    return conn


def upsert_listing(conn: sqlite3.Connection, row: dict):
    conn.execute("""
        INSERT INTO listings
            (zpid, url, listing_status, property_type, latitude, longitude,
             price, bedrooms, bathrooms, living_area, address, description,
             photo_url, data, scraped_at, status)
        VALUES
            (:zpid, :url, :listing_status, :property_type, :latitude, :longitude,
             :price, :bedrooms, :bathrooms, :living_area, :address, :description,
             :photo_url, :data, :scraped_at, :status)
        ON CONFLICT(zpid) DO UPDATE SET
            url            = excluded.url,
            listing_status = excluded.listing_status,
            property_type  = excluded.property_type,
            latitude       = excluded.latitude,
            longitude      = excluded.longitude,
            price          = excluded.price,
            bedrooms       = excluded.bedrooms,
            bathrooms      = excluded.bathrooms,
            living_area    = excluded.living_area,
            address        = excluded.address,
            description    = excluded.description,
            photo_url      = excluded.photo_url,
            data           = excluded.data,
            scraped_at     = excluded.scraped_at,
            status         = excluded.status
        WHERE listings.status != 'done'
    """, row)
    conn.commit()


def is_done(conn: sqlite3.Connection, zpid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE zpid = ? AND status = 'done'", (zpid,)
    ).fetchone()
    return row is not None


# ─── EXTRACTION ───────────────────────────────────────────────────────────────
def map_home_type(home_type):
    if not home_type:
        return "UNKNOWN"
    t = home_type.upper()
    if "APARTMENT" in t or "CONDO" in t or "MULTI" in t:
        return "APARTMENT"
    return "HOUSE"


def extract_listings_from_html(html: str) -> list:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if not match:
        log.warning("  No se encontró __NEXT_DATA__ en el HTML")
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        log.warning("  HTML guardado en debug_page.html")
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        log.error(f"  Error parseando JSON: {e}")
        return []

    results = []
    try:
        cat1 = data["props"]["pageProps"]["searchPageState"]["cat1"]
        for item in cat1["searchResults"].get("listResults", []):
            results.append(item)
        for item in cat1["searchResults"].get("mapResults", []):
            zpid = item.get("zpid")
            if zpid and not any(r.get("zpid") == zpid for r in results):
                results.append(item)
    except (KeyError, TypeError):
        pass

    return results


def normalize_listing(raw: dict):
    zpid = str(raw.get("zpid", "")).strip()
    if not zpid or zpid == "None":
        return None

    status_raw = (raw.get("statusType") or "").upper()
    listing_status = "FOR_RENT" if "RENT" in status_raw else "FOR_SALE"
    home_type = (
        raw.get("hdpData", {}).get("homeInfo", {}).get("homeType")
        or raw.get("homeType")
    )
    property_type = map_home_type(home_type)

    price = (
        raw.get("unformattedPrice")
        or raw.get("hdpData", {}).get("homeInfo", {}).get("price")
    )
    if isinstance(price, str):
        price = int("".join(filter(str.isdigit, price)) or 0) or None

    lat = (raw.get("latLong") or {}).get("latitude") or raw.get("latitude")
    lng = (raw.get("latLong") or {}).get("longitude") or raw.get("longitude")
    bedrooms   = raw.get("beds") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("bedrooms")
    bathrooms  = raw.get("baths") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("bathrooms")
    living_area = raw.get("area") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("livingArea")
    address    = raw.get("address") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("streetAddress")
    description = (raw.get("description") or "")[:2000]
    photo_url  = raw.get("imgSrc") or ""
    if not photo_url and raw.get("carouselPhotos"):
        photo_url = raw["carouselPhotos"][0].get("url", "")
    detail_url = raw.get("detailUrl") or ""
    if detail_url and not detail_url.startswith("http"):
        detail_url = "https://www.zillow.com" + detail_url

    return {
        "zpid": zpid, "url": detail_url, "listing_status": listing_status,
        "property_type": property_type, "latitude": lat, "longitude": lng,
        "price": price, "bedrooms": bedrooms, "bathrooms": bathrooms,
        "living_area": living_area, "address": address, "description": description,
        "photo_url": photo_url, "data": json.dumps(raw, ensure_ascii=False),
        "scraped_at": datetime.now(timezone.utc).isoformat(), "status": "done",
    }


# ─── SCRAPER ──────────────────────────────────────────────────────────────────
def fetch_page(session, url: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"Fetching (attempt {attempt}): {url[:90]}...")
            resp = session.get(url, headers=HEADERS, timeout=30)
            log.info(f"  Status: {resp.status_code} | Size: {len(resp.text)} chars")
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code in (403, 429):
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning(f"  Bloqueado ({resp.status_code}). Esperando {wait}s...")
                time.sleep(wait)
            else:
                log.warning(f"  HTTP {resp.status_code}. Reintentando...")
                time.sleep(BACKOFF_BASE)
        except Exception as e:
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            log.error(f"  Error: {e}. Reintentando en {wait}s...")
            time.sleep(wait)
    log.error(f"  Todos los intentos fallaron para {url[:90]}")
    return None


def run_scraper():
    conn = init_db(DB_PATH)
    log.info(f"Base de datos inicializada en {DB_PATH}")

    # curl_cffi impersona el TLS fingerprint de Chrome124 — evita PerimeterX sin browser
    session = curl_requests.Session(impersonate="chrome124")

    total_saved = 0

    for page_num in range(1, MAX_PAGES + 1):
        url = build_search_url(page_num)
        html = fetch_page(session, url)

        if not html:
            log.error(f"No se pudo obtener la página {page_num}. Deteniendo.")
            break

        raw_listings = extract_listings_from_html(html)
        log.info(f"Página {page_num}: {len(raw_listings)} listings encontrados")

        if not raw_listings:
            log.info("Sin listings — fin de resultados o bloqueado.")
            break

        new_on_page = 0
        for raw in raw_listings:
            listing = normalize_listing(raw)
            if listing is None:
                continue
            if is_done(conn, listing["zpid"]):
                log.info(f"  Skip: zpid={listing['zpid']}")
                continue
            upsert_listing(conn, listing)
            new_on_page += 1
            total_saved += 1
            log.info(f"  OK zpid={listing['zpid']} | {listing['address']} | {listing['listing_status']}")

        log.info(f"Página {page_num}: +{new_on_page} nuevos. Total acumulado: {total_saved}")

        if len(raw_listings) < 10:
            log.info("Menos de 10 resultados — última página.")
            break

        time.sleep(3)

    conn.close()
    log.info(f"Scraping completo. Total guardados: {total_saved}")


if __name__ == "__main__":
    run_scraper()
