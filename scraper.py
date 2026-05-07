"""
Zillow Puerto Rico Scraper
Approach: curl_cffi (TLS fingerprint spoofing) + API interna / __NEXT_DATA__ fallback
"""

import gc
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

import psutil
from curl_cffi import requests as curl_requests
from pydantic import BaseModel, ValidationError

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DB_PATH            = "listings.db"
MAX_RETRIES        = 3
BACKOFF_BASE       = 5
MAX_PAGES          = 20
SESSION_RECYCLE    = 10    # recycle curl_cffi session every N pages to prevent memory growth
MEM_WARN_MB        = 300
MEM_CRIT_MB        = 500
NULL_THRESHOLD_PCT = 0.30  # circuit breaker: halt if >30% of page listings have null required fields
SCHEMA_ALERTS_DIR  = "schema_alerts"
PAGE_DELAY_MIN     = 2     # seconds — min wait between pages
PAGE_DELAY_MAX     = 6     # seconds — max wait between pages

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

API_HEADERS = {
    "accept":          "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type":    "application/json",
    "referer":         "https://www.zillow.com/",
    "sec-fetch-dest":  "empty",
    "sec-fetch-mode":  "cors",
    "sec-fetch-site":  "same-origin",
    "user-agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "x-client":        "for-sale",
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


def build_api_payload(page: int = 1) -> dict:
    return {
        "searchQueryState": {
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
            "regionSelection": [{"regionId": 57267, "regionType": 2}],
        },
        "wants": {"cat1": ["listResults", "mapResults"], "cat2": ["total"]},
        "requestId": page,
        "isDebugRequest": False,
    }


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


def get_resume_page(conn: sqlite3.Connection) -> int:
    """Estimate resume page from DB so restarts don't re-scrape from page 1."""
    done_count = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE status = 'done'"
    ).fetchone()[0]
    if done_count == 0:
        return 1
    estimated = max(1, done_count // 40)
    log.info(f"[RESUME] {done_count} listings 'done' en DB. Retomando desde página ~{estimated}.")
    return estimated


# ─── MEMORY MANAGEMENT ────────────────────────────────────────────────────────
def log_memory():
    mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    if mb > MEM_CRIT_MB:
        log.error(f"[MEMORIA] {mb:.1f} MB — crítico. Pausando 30s para GC...")
        time.sleep(30)
    elif mb > MEM_WARN_MB:
        log.warning(f"[MEMORIA] {mb:.1f} MB — proceso creciendo")
    else:
        log.info(f"[MEMORIA] {mb:.1f} MB")


def make_session() -> curl_requests.Session:
    return curl_requests.Session(impersonate="chrome124")


# ─── SCHEMA VALIDATION ────────────────────────────────────────────────────────
from typing import Optional

class ListingModel(BaseModel):
    zpid: str
    url: str
    listing_status: str
    property_type: str
    latitude: float
    longitude: float
    price: int
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    address: str
    description: str = ""
    photo_url: str = ""
    data: str
    scraped_at: str
    status: str


def check_page_schema(listings: list, raw_json: dict, page_num: int) -> bool:
    """Returns False and saves alert JSON if >NULL_THRESHOLD_PCT listings failed Pydantic validation."""
    if not listings:
        return True
    failed = sum(1 for lst in listings if lst.get("status") == "failed")
    ratio = failed / len(listings)
    if ratio > NULL_THRESHOLD_PCT:
        log.error(
            f"[SCHEMA] Página {page_num}: {failed}/{len(listings)} listings inválidos "
            f"({ratio:.0%}). Circuit breaker activado."
        )
        if raw_json:
            os.makedirs(SCHEMA_ALERTS_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCHEMA_ALERTS_DIR, f"schema_alert_page{page_num}_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw_json, f, ensure_ascii=False, indent=2)
            log.error(f"[SCHEMA] JSON crudo guardado en {path}")
        return False
    return True


# ─── EXTRACTION ───────────────────────────────────────────────────────────────
def map_home_type(home_type):
    if not home_type:
        return "UNKNOWN"
    t = home_type.upper()
    if "APARTMENT" in t or "CONDO" in t or "MULTI" in t:
        return "APARTMENT"
    return "HOUSE"


def extract_listings_from_html(html: str) -> tuple[list, dict]:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if not match:
        log.warning("  No se encontró __NEXT_DATA__ en el HTML")
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        return [], {}

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        log.error(f"  Error parseando JSON: {e}")
        return [], {}

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

    return results, data


def extract_listings_from_api(api_data: dict) -> list:
    results = []
    try:
        cat1 = api_data["cat1"]
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

    lat  = (raw.get("latLong") or {}).get("latitude") or raw.get("latitude")
    lng  = (raw.get("latLong") or {}).get("longitude") or raw.get("longitude")
    bedrooms    = raw.get("beds") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("bedrooms")
    bathrooms   = raw.get("baths") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("bathrooms")
    living_area = raw.get("area") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("livingArea")
    address     = raw.get("address") or (raw.get("hdpData") or {}).get("homeInfo", {}).get("streetAddress")
    description = (raw.get("description") or "")[:2000]
    photo_url   = raw.get("imgSrc") or ""
    if not photo_url and raw.get("carouselPhotos"):
        photo_url = raw["carouselPhotos"][0].get("url", "")
    detail_url = raw.get("detailUrl") or ""
    if detail_url and not detail_url.startswith("http"):
        detail_url = "https://www.zillow.com" + detail_url

    row = {
        "zpid": zpid, "url": detail_url, "listing_status": listing_status,
        "property_type": property_type, "latitude": lat, "longitude": lng,
        "price": price, "bedrooms": bedrooms, "bathrooms": bathrooms,
        "living_area": living_area, "address": address, "description": description,
        "photo_url": photo_url, "data": json.dumps(raw, ensure_ascii=False),
        "scraped_at": datetime.now(timezone.utc).isoformat(), "status": "done",
    }
    try:
        ListingModel(**row)
    except ValidationError as e:
        missing = [str(err["loc"][0]) for err in e.errors()]
        log.warning(f"  FAILED zpid={zpid} — campos inválidos: {missing}")
        row["status"] = "failed"
    return row


# ─── FETCHING ─────────────────────────────────────────────────────────────────
def fetch_html(session, url: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"[HTML] Fetching (attempt {attempt}): {url[:90]}...")
            resp = session.get(url, headers=HEADERS, timeout=30)
            log.info(f"  Status: {resp.status_code} | Size: {len(resp.text)} chars")
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code in (403, 429):
                wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, BACKOFF_BASE)
                log.warning(f"  Bloqueado ({resp.status_code}). Esperando {wait}s...")
                time.sleep(wait)
            else:
                log.warning(f"  HTTP {resp.status_code}. Reintentando...")
                time.sleep(BACKOFF_BASE)
        except Exception as e:
            wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, BACKOFF_BASE)
            log.error(f"  Error: {e}. Reintentando en {wait}s...")
            time.sleep(wait)
    log.error(f"  Todos los intentos fallaron para {url[:90]}")
    return None


def fetch_api(session, page: int) -> list | None:
    """Call Zillow's internal search API directly — no HTML parsing, ~60x less data than HTML."""
    url = "https://www.zillow.com/async-create-search-page-state"
    payload = build_api_payload(page)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"[API] Página {page} (attempt {attempt})...")
            resp = session.post(url, headers=API_HEADERS, json=payload, timeout=30)
            log.info(f"  API Status: {resp.status_code} | Size: {len(resp.text)} chars")
            if resp.status_code == 200:
                data = resp.json()
                listings = extract_listings_from_api(data)
                if listings:
                    log.info(f"  [API] {len(listings)} listings extraídos directo de JSON")
                    return listings
                log.warning("  [API] 200 pero sin listings — fallback a HTML")
                return None
            elif resp.status_code == 403:
                log.warning("  [API] 403 — bloqueo de política, sin reintentos. Fallback a HTML.")
                return None
            elif resp.status_code == 429:
                wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, BACKOFF_BASE)
                log.warning(f"  [API] Rate limit (429). Esperando {wait}s...")
                time.sleep(wait)
            else:
                log.warning(f"  [API] HTTP {resp.status_code}. Reintentando...")
                time.sleep(BACKOFF_BASE)
        except Exception as e:
            wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, BACKOFF_BASE)
            log.warning(f"  [API] Error: {e}. Reintentando en {wait}s...")
            time.sleep(wait)
    return None


def fetch_page_with_fallback(session, page: int) -> tuple[list, dict, str]:
    """Try internal API first, fall back to HTML + __NEXT_DATA__. Returns (listings, raw_json, source)."""
    api_listings = fetch_api(session, page)
    if api_listings is not None:
        return api_listings, {}, "api"

    url = build_search_url(page)
    html = fetch_html(session, url)
    if not html:
        return [], {}, "failed"

    listings, raw_json = extract_listings_from_html(html)
    del html
    gc.collect()
    return listings, raw_json, "html"


# ─── SCRAPER ──────────────────────────────────────────────────────────────────
def run_scraper():
    conn = init_db(DB_PATH)
    log.info(f"Base de datos inicializada en {DB_PATH}")

    start_page = get_resume_page(conn)
    session = make_session()

    total_saved = 0
    failed_listings = 0
    pages_api = 0
    pages_html = 0

    for page_num in range(start_page, MAX_PAGES + 1):

        # Recycle session every SESSION_RECYCLE pages — prevents cookie/buffer accumulation in libcurl
        if (page_num - start_page) > 0 and (page_num - start_page) % SESSION_RECYCLE == 0:
            log.info(f"[SESIÓN] Reciclando sesión en página {page_num}")
            del session
            gc.collect()
            session = make_session()

        log_memory()

        raw_listings, raw_json, source = fetch_page_with_fallback(session, page_num)

        if source == "failed":
            log.error(f"No se pudo obtener la página {page_num}. Deteniendo.")
            break

        if source == "api":
            pages_api += 1
        else:
            pages_html += 1

        log.info(f"Página {page_num} [{source.upper()}]: {len(raw_listings)} listings encontrados")

        if not raw_listings:
            log.info("Sin listings — fin de resultados o bloqueado.")
            break

        normalized_batch = [lst for raw in raw_listings if (lst := normalize_listing(raw))]

        # Circuit breaker: stop if schema changed and most fields are null
        if not check_page_schema(normalized_batch, raw_json, page_num):
            log.error("Circuit breaker activado. Deteniendo scraper.")
            break

        new_on_page = 0
        for listing in normalized_batch:
            if is_done(conn, listing["zpid"]):
                log.info(f"  Skip: zpid={listing['zpid']}")
                continue
            if listing["status"] == "failed":
                failed_listings += 1
            else:
                new_on_page += 1
                total_saved += 1
                log.info(f"  OK zpid={listing['zpid']} | {listing['address']} | {listing['listing_status']}")
            upsert_listing(conn, listing)

        page_size = len(normalized_batch)
        del raw_listings, normalized_batch, raw_json
        gc.collect()

        log.info(f"Página {page_num}: +{new_on_page} nuevos. Total acumulado: {total_saved}")

        if page_size < 10:
            log.info("Menos de 10 resultados — última página.")
            break

        delay = random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX)
        log.info(f"[RATE] Esperando {delay:.1f}s antes de próxima página...")
        time.sleep(delay)

    conn.close()
    log.info(
        f"Scraping completo. Done: {total_saved} | "
        f"API: {pages_api} páginas | HTML fallback: {pages_html} páginas"
    )
    if failed_listings:
        log.warning(f"[AUDITORÍA] {failed_listings} listings con campos nulos guardados como 'failed'.")
        log.warning("Consultar con: SELECT * FROM listings WHERE status = 'failed';")


if __name__ == "__main__":
    run_scraper()
