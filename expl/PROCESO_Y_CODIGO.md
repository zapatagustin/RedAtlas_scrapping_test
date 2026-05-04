# Proceso completo y explicación del código — Zillow PR Scraper

Este documento describe en detalle todo lo que se hizo para construir el scraper, los problemas que aparecieron, cómo se resolvieron, y una explicación línea por línea del código final.

---

## Parte 1 — El challenge y qué se pedía

El challenge pedía construir un scraper de Zillow para Puerto Rico (`zillow.com/pr/`) que extrajera propiedades en venta y alquiler, específicamente casas y apartamentos con 2 o más habitaciones. Los datos debían guardarse en SQLite con un schema específico: zpid, url, listing_status, property_type, coordenadas, precio, habitaciones, baños, superficie, dirección, descripción y foto.

Además de scraper funcional, el challenge tenía preguntas de arquitectura sobre situaciones reales de producción: qué hacer si el scraper consume demasiada memoria, cómo manejar 500 URLs con concurrencia limitada, cómo hacer un deploy sin perder progreso, y cómo escalar con proxies rotativos.

---

## Parte 2 — Proceso completo, paso a paso

### Paso 1 — Decisión de approach: Playwright + __NEXT_DATA__

Lo primero fue analizar cómo funciona Zillow técnicamente. Zillow está construido con Next.js, un framework de React que hace server-side rendering. Esto significa que cuando el servidor envía el HTML de una página de resultados, ya incluye todos los datos de los listings embebidos en un tag `<script id="__NEXT_DATA__">`. Este JSON es el estado inicial de la aplicación — contiene zpid, precio, coordenadas, fotos, tipo de propiedad, todo.

La decisión inicial fue usar Playwright (browser headless) para cargar la página y extraer ese JSON. Playwright abre un Chromium real, navega a la URL, y luego podemos leer el contenido del DOM. La ventaja sobre `requests` directas era que un browser real puede ejecutar JavaScript y pasar algunas protecciones anti-bot.

### Paso 2 — Primer intento: Playwright bloqueado por captcha

Al correr el scraper por primera vez, Zillow respondía con una página de captcha en lugar de los resultados. El elemento `#__NEXT_DATA__` no existía en el DOM porque la página que se cargaba era el challenge de PerimeterX, no la página de resultados.

El error era: `Page.eval_on_selector: Failed to find element matching selector "#__NEXT_DATA__"`. Esto confirmaba que la página que llegaba no era la de resultados.

Se agregó código de debug: guardar un screenshot (`debug_screenshot.png`) y el HTML completo (`debug_page.html`) inmediatamente después de cargar la página, para ver qué estaba devolviendo Zillow. El screenshot mostró el captcha "Press & Hold to confirm you are a human".

### Paso 3 — Intento de evasión con playwright-stealth

Se instaló `playwright-stealth`, una librería que parchea las señales más comunes que delatan a un browser automatizado: elimina `navigator.webdriver`, falsifica los plugins del browser, corrige el idioma del navigator, entre otras ~20 señales.

Hubo un problema de compatibilidad: la versión nueva de `playwright-stealth` cambió la API de `stealth_async(page)` a `Stealth().apply_stealth_async(page)`. Se actualizó el código.

También se cambió `wait_until="domcontentloaded"` a `wait_until="networkidle"` para dar más tiempo a que la página cargara completamente. Pero esto causó timeouts de 60 segundos porque Zillow tiene requests de red continuos (telemetría, analytics) que nunca paran, así que `networkidle` nunca se alcanzaba.

Se cambió a `wait_for_selector("#__NEXT_DATA__", timeout=15_000)` para esperar específicamente al elemento que necesitábamos.

Nada de esto funcionó — el captcha seguía apareciendo.

### Paso 4 — Captcha manual + sesión humana

La siguiente estrategia fue pausar el scraper, dejar que el usuario resolviera el captcha manualmente en el browser visible, y luego continuar con la sesión ya autenticada. Se agregó un `input()` en el código: el scraper abría Zillow, el usuario resolvía el captcha, presionaba ENTER en la terminal, y el scraper continuaba.

El problema fue que después de presionar ENTER, el scraper hacía un nuevo `page.goto()` a la URL con los filtros. Ese nuevo navigate tiraba la sesión actual y triggereaba el captcha de nuevo. Las cookies de autenticación de PerimeterX no sobrevivían a un nuevo goto porque la URL con el `searchQueryState` complejo era tratada como una nueva sesión.

Se refactorizó para leer el `__NEXT_DATA__` de la página ya cargada (sin hacer un nuevo goto para la página 1), y hacer gotos solo para las páginas 2 en adelante.

Pero el captcha "Press & Hold" no se podía resolver manualmente tampoco porque decía "Please try again" — PerimeterX detectaba que el click venía de un contexto automatizado incluso cuando el usuario hacía el hold. Esto es porque PerimeterX inyecta JavaScript que analiza el contexto del evento de mouse (si viene de un EventTarget real o fue disparado programáticamente).

### Paso 5 — Diagnóstico del problema de IP

Se creó un script de diagnóstico `debug_fetch.py` usando `curl_cffi` para hacer un request simple y ver qué devolvía Zillow. El resultado fue HTTP 403 desde el primer request, sin siquiera llegar al captcha interactivo.

El body de la respuesta contenía `window._pxAppId = 'PXHYx10rg3'` — la firma de PerimeterX bloqueando a nivel de IP. Las IPs de datacenter de Windscribe Free están en las blacklists de Zillow. El bloqueo ocurría antes de que el request fuera evaluado por cualquier lógica de fingerprinting.

### Paso 6 — Cambio de approach completo: curl_cffi sin browser

Con el diagnóstico claro, se descartó Playwright completamente y se reescribió el scraper usando `curl_cffi`. Esta librería usa libcurl compilado con BoringSSL y permite especificar `impersonate="chrome124"`, lo que hace que el handshake TLS sea idéntico al de Chrome 124 — mismo JA3 hash, mismas cipher suites, mismas extensiones TLS, mismo orden de parámetros.

El resultado: sin browser, sin DOM, sin JavaScript — solo un request HTTP directo que Zillow ve como si viniera de Chrome 124 real. El JSON de `__NEXT_DATA__` se extrae con una expresión regular del HTML recibido.

### Paso 7 — Problema de IP de Windscribe

Con la nueva versión del scraper y `curl_cffi`, los requests seguían devolviendo 403. La razón era la misma: la IP de Windscribe Free estaba en blacklist. `curl_cffi` spoofea el TLS fingerprint pero no puede cambiar la IP.

La solución fue conectar la laptop al hotspot del celular. Las IPs de operadoras móviles son IPs residenciales o de carrier — no están en las blacklists de Zillow porque son IPs que usan millones de personas reales para navegar. Con esta IP, el primer request devolvió HTTP 200.

### Paso 8 — Scraping exitoso, 533 listings

Con `curl_cffi` e IP de hotspot móvil, el scraper funcionó perfectamente. Procesó 13 páginas de 41 listings cada una, guardando 533 propiedades en `listings.db` en aproximadamente 3 minutos.

En la página 14, Zillow interrumpió el handshake TLS con un error `BoringSSL SSL_read: BAD_DECRYPT`. Este error ocurre cuando PerimeterX detecta un patrón automatizado y corrompe activamente la respuesta SSL para que el cliente no pueda procesarla. Es el rate limiting activo de Zillow — 13 páginas (533 listings) es el límite alcanzado con una IP de hotspot móvil sin proxies rotativos.

---

## Parte 3 — Explicación completa del código

### Imports y dependencias

```python
import json          # para parsear el JSON de __NEXT_DATA__
import logging       # sistema de logs con niveles INFO/WARNING/ERROR
import re            # expresiones regulares para extraer __NEXT_DATA__ del HTML
import sqlite3       # base de datos local, viene incluida en Python
import time          # para los delays entre páginas y el backoff
import urllib.parse  # para URL-encodear el searchQueryState

from datetime import datetime, timezone  # timestamps en UTC
from curl_cffi import requests as curl_requests  # requests con TLS spoofing
```

`curl_cffi` es la única dependencia externa. Todo lo demás es librería estándar de Python.

### Configuración global

```python
DB_PATH      = "listings.db"   # nombre del archivo SQLite
MAX_RETRIES  = 3               # intentos por página antes de rendirse
BACKOFF_BASE = 5               # segundos base para el backoff exponencial
MAX_PAGES    = 20              # límite de páginas para no correr infinito
```

Estos son los parámetros que controlan el comportamiento del scraper. Al tenerlos al tope del archivo es fácil ajustarlos sin tocar la lógica.

### Headers HTTP

```python
HEADERS = {
    "accept": "text/html,...",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124"...',
    "sec-fetch-dest": "document",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)...",
    ...
}
```

Estos headers imitan exactamente los que envía Chrome 124 al navegar a una página. Los headers `sec-ch-ua`, `sec-fetch-*` son específicos de Chromium y forman parte del fingerprint HTTP. Si estos headers no coinciden con el TLS fingerprint (que también es de Chrome 124), PerimeterX detecta la inconsistencia.

### Construcción de la URL de búsqueda

```python
def build_search_url(page: int = 1) -> str:
    qs = {
        "pagination": {"currentPage": page},
        "isMapVisible": False,
        "filterState": {
            "beds":  {"min": 2},    # mínimo 2 habitaciones
            "con":   {"value": False},  # excluir condominios solos
            "gar":   {"value": False},  # excluir garajes
            "mf":    {"value": False},  # excluir multi-family
            "land":  {"value": False},  # excluir terrenos
            "tow":   {"value": False},  # excluir townhouses
        },
        "isListVisible": True,
    }
    encoded = urllib.parse.quote(json.dumps(qs, separators=(",", ":")))
    if page == 1:
        return f"https://www.zillow.com/pr/?searchQueryState={encoded}"
    return f"https://www.zillow.com/pr/{page}_p/?searchQueryState={encoded}"
```

Esta función construye la URL de búsqueda con los filtros del challenge. Zillow recibe los filtros como un objeto JSON URL-encoded en el query param `searchQueryState`. La paginación usa el patrón `/{N}_p/` en el path. `json.dumps` con `separators=(",", ":")` genera JSON sin espacios, que es lo que Zillow espera.

### Sistema de logging

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),           # salida a consola
        logging.FileHandler("scraper.log"), # salida a archivo
    ],
)
log = logging.getLogger(__name__)
```

Configuración del logger para que escriba simultáneamente en consola y en `scraper.log`. El formato incluye timestamp, nivel y mensaje. Tener logs en archivo es crucial para analizar qué pasó después de una ejecución larga.

### Inicialización de la base de datos

```python
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            zpid           TEXT PRIMARY KEY,
            ...
            status         TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    return conn
```

`PRAGMA journal_mode=WAL` activa el modo Write-Ahead Logging de SQLite. En modo WAL, las escrituras van primero a un archivo de log separado y luego se consolidan en la base de datos. Esto tiene dos ventajas: las escrituras son más rápidas y la base de datos puede leerse simultáneamente mientras se escribe (importante si queremos hacer consultas mientras el scraper corre).

`CREATE TABLE IF NOT EXISTS` hace que la función sea idempotente — si la tabla ya existe (de una ejecución anterior), no falla ni la recrea. El campo `zpid` como PRIMARY KEY es el mecanismo de deduplicación.

### UPSERT — el corazón del resume

```python
def upsert_listing(conn: sqlite3.Connection, row: dict):
    conn.execute("""
        INSERT INTO listings (zpid, url, ...)
        VALUES (:zpid, :url, ...)
        ON CONFLICT(zpid) DO UPDATE SET
            url = excluded.url,
            ...
        WHERE listings.status != 'done'
    """, row)
    conn.commit()
```

Este es el mecanismo más importante del scraper. `INSERT OR REPLACE` es destructivo, pero `ON CONFLICT DO UPDATE` (UPSERT) permite actualizar solo los campos que cambiaron. La cláusula `WHERE listings.status != 'done'` es la garantía del resume: si un listing ya tiene `status = 'done'`, el UPDATE no lo toca aunque se vuelva a intentar insertar. Los datos scrapeados exitosamente son inmutables.

`excluded.url` en SQLite es la sintaxis para referirse al valor que se intentaba insertar (el "excluido" por el conflicto).

### Verificación de listing ya procesado

```python
def is_done(conn: sqlite3.Connection, zpid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE zpid = ? AND status = 'done'", (zpid,)
    ).fetchone()
    return row is not None
```

Antes de procesar cada listing, verificamos si ya fue procesado en una ejecución anterior. El `?` es un parámetro posicional de SQLite — nunca se construyen queries con string formatting para evitar SQL injection (aunque aquí no hay riesgo real, es buena práctica).

### Clasificación del tipo de propiedad

```python
def map_home_type(home_type):
    if not home_type:
        return "UNKNOWN"
    t = home_type.upper()
    if "APARTMENT" in t or "CONDO" in t or "MULTI" in t:
        return "APARTMENT"
    return "HOUSE"
```

Zillow usa valores como `"SINGLE_FAMILY"`, `"APARTMENT"`, `"CONDO"`, `"MULTI_FAMILY"`. Esta función los normaliza a los dos valores que pide el challenge: `HOUSE` o `APARTMENT`.

### Extracción del __NEXT_DATA__

```python
def extract_listings_from_html(html: str) -> list:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if not match:
        log.warning("  No se encontró __NEXT_DATA__ en el HTML")
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        return []

    data = json.loads(match.group(1))

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
```

El regex busca el tag `<script>` con `id="__NEXT_DATA__"` y captura su contenido. `re.DOTALL` hace que el `.` también matchee saltos de línea (el JSON puede tener múltiples líneas). `match.group(1)` extrae el contenido del primer grupo de captura (los paréntesis en el regex).

Si no encuentra el tag, guarda el HTML para debugging y retorna lista vacía — el scraper no crashea, simplemente registra el problema.

El path `data["props"]["pageProps"]["searchPageState"]["cat1"]` es la estructura de Next.js. `cat1` es la categoría de resultados principal. Dentro tiene `listResults` (los que aparecen en la lista) y `mapResults` (los del mapa, que pueden ser diferentes).

El `try/except (KeyError, TypeError)` captura silenciosamente cualquier cambio de estructura — si Zillow reorganiza el JSON, el scraper retorna lista vacía en lugar de crashear.

### Normalización de cada listing

```python
def normalize_listing(raw: dict):
    zpid = str(raw.get("zpid", "")).strip()
    if not zpid or zpid == "None":
        return None
```

Primero extraemos el `zpid`. Es el ID único de Zillow para cada propiedad. Si no existe o es la string "None" (puede pasar con algunas entradas de mapResults), descartamos el listing.

```python
    status_raw = (raw.get("statusType") or "").upper()
    listing_status = "FOR_RENT" if "RENT" in status_raw else "FOR_SALE"
```

`statusType` puede ser `"FOR_SALE"`, `"FOR_RENT"`, `"SOLD"`, etc. Si contiene "RENT", lo clasificamos como `FOR_RENT`, de lo contrario `FOR_SALE`.

```python
    price = (
        raw.get("unformattedPrice")
        or raw.get("hdpData", {}).get("homeInfo", {}).get("price")
    )
    if isinstance(price, str):
        price = int("".join(filter(str.isdigit, price)) or 0) or None
```

El precio puede estar en dos lugares del JSON: `unformattedPrice` (un número) o dentro de `hdpData.homeInfo.price`. Si es una string como `"$245,000"`, la limpiamos con `filter(str.isdigit, price)` que extrae solo los dígitos.

```python
    lat = (raw.get("latLong") or {}).get("latitude") or raw.get("latitude")
    lng = (raw.get("latLong") or {}).get("longitude") or raw.get("longitude")
```

Las coordenadas pueden estar anidadas en `latLong.latitude` o directamente en `latitude`. El `or {}` antes del `.get("latitude")` previene un `AttributeError` si `latLong` es `None`.

```python
    return {
        "zpid": zpid, "url": detail_url, "listing_status": listing_status,
        ...
        "data": json.dumps(raw, ensure_ascii=False),  # JSON crudo completo
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "status": "done",
    }
```

El campo `data` guarda el JSON crudo completo del listing. Esto es fundamental: si hay campos que no extraemos ahora o si el schema cambia, los datos originales están preservados y pueden re-procesarse. `ensure_ascii=False` permite caracteres UTF-8 (acentos, ñ, etc.) en las direcciones de PR.

### Descarga de páginas con retry y backoff

```python
def fetch_page(session, url: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code in (403, 429):
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning(f"  Bloqueado ({resp.status_code}). Esperando {wait}s...")
                time.sleep(wait)
            else:
                time.sleep(BACKOFF_BASE)
        except Exception as e:
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            log.error(f"  Error: {e}. Reintentando en {wait}s...")
            time.sleep(wait)
    return None
```

El backoff exponencial: intento 1 espera 5s, intento 2 espera 10s, intento 3 espera 20s. La fórmula es `BACKOFF_BASE * 2^(attempt-1)`.

Los errores 403 y 429 son de bloqueo/rate limiting. Otros errores (timeout, SSL) caen en el `except Exception`. Después de `MAX_RETRIES` intentos fallidos, retorna `None` y el loop principal detiene el scraping.

### Loop principal

```python
def run_scraper():
    conn = init_db(DB_PATH)
    session = curl_requests.Session(impersonate="chrome124")

    for page_num in range(1, MAX_PAGES + 1):
        url = build_search_url(page_num)
        html = fetch_page(session, url)

        if not html:
            break

        raw_listings = extract_listings_from_html(html)

        if not raw_listings:
            break

        for raw in raw_listings:
            listing = normalize_listing(raw)
            if listing is None:
                continue
            if is_done(conn, listing["zpid"]):
                continue
            upsert_listing(conn, listing)

        if len(raw_listings) < 10:
            break  # última página

        time.sleep(3)  # delay entre páginas
```

`curl_requests.Session(impersonate="chrome124")` crea una sesión que mantiene cookies entre requests (importante para la sesión de Zillow) y spoofea el TLS fingerprint de Chrome 124 en cada handshake.

El loop itera hasta `MAX_PAGES` o hasta que no haya más resultados. La condición `len(raw_listings) < 10` detecta la última página — Zillow siempre devuelve 40-41 listings por página excepto la última. El `time.sleep(3)` es el delay de cortesía entre páginas para no hacer requests demasiado rápido.

---

## Parte 4 — Qué se podría mejorar con más tiempo

1. **Proxies rotativos:** Con Brightdata o un proveedor de proxies residenciales, cada página iría por una IP diferente. Esto eliminaría el rate limiting después de 13 páginas.

2. **Scraping de páginas de detalle:** Actualmente solo se scrapean las páginas de resultados. Cada listing tiene una URL de detalle donde hay más información (descripción completa, fotos adicionales, historial de precio, datos del agente). Con un segundo loop se podría enriquecer cada registro.

3. **Monitoreo de cambios de schema:** Si Zillow cambia la estructura del JSON, actualmente el scraper falla silenciosamente (campos null). Se podría agregar una validación que alerte cuando más del X% de los campos son null.

4. **Exportación a otros formatos:** La base SQLite es fácil de consultar con cualquier herramienta. Pero se podría agregar exportación a CSV o JSON para facilitar el análisis.

5. **Tests unitarios:** Las funciones `normalize_listing`, `extract_listings_from_html` y `map_home_type` son puras (no tienen efectos secundarios) y fáciles de testear con fixtures del JSON de Zillow.
