# Zillow Puerto Rico Scraper

Scraper de propiedades en Puerto Rico (Houses + Apartments, For Sale + For Rent, 2+ bedrooms) usando `curl_cffi` con TLS fingerprint spoofing y SQLite como base de datos.

---

## Resultado obtenido

| Métrica | Valor |
|---|---|
| Páginas scrapeadas | 13 de 20 |
| Listings guardados | 533 |
| Tipos | FOR_SALE (mayoritariamente), FOR_RENT |
| Detención | Rate limiting activo por PerimeterX en página 14 |
| Error registrado | `BoringSSL SSL_read: BAD_DECRYPT` — interrupción del handshake TLS por Zillow |

---

## Instalación

**Requisitos:** Python 3.10+

```bash
pip install poetry
poetry init --no-interaction --name "zillow-scraper" --python "^3.10"
poetry add curl-cffi
```

## Ejecución

```bash
# Conectarse a hotspot móvil (ver sección de obstáculos)
poetry run python scraper.py
```

Los datos quedan en `listings.db`. La ejecución es **resumible**: si se interrumpe, al volver a correr los listings con `status = done` se saltean automáticamente y se retoma desde donde quedó.

Los logs se escriben simultáneamente en consola y en `scraper.log`.

---

## Approach técnico

### Por qué curl_cffi en lugar de Playwright o requests

La primera decisión de diseño fue cómo hacer los requests HTTP. Las opciones eran:

**1. `requests` estándar de Python** — descartado porque no spoofea el TLS fingerprint. Zillow/PerimeterX analiza el JA3 hash de la negociación TLS (el conjunto de cipher suites, extensiones y versiones que el cliente anuncia) y si no coincide con un browser real, bloquea en la capa de red antes de evaluar cualquier header HTTP.

**2. Playwright (browser headless)** — descartado en producción porque PerimeterX implementa un captcha "Press & Hold" que mide la presión, duración y aceleración del mouse. Un browser automatizado no puede superar esta prueba biométrica. Además consume ~1.8 GB de RAM por instancia.

**3. `curl_cffi`** — la solución elegida. Usa libcurl compilado con BoringSSL y permite especificar `impersonate="chrome124"`, lo que hace que el stack TLS sea byte-a-byte idéntico al de Chrome 124. El JA3 hash resultante es indistinguible de un browser real. No hay browser, no hay captcha, solo HTTP directo.

### Por qué __NEXT_DATA__ en lugar de parsear el DOM

Zillow está construido con Next.js. En cada página de resultados, Next.js embebe el estado completo de la aplicación en un tag `<script id="__NEXT_DATA__">` que contiene un JSON con todos los listings, precios, coordenadas, fotos y metadatos. Este JSON es generado server-side y siempre está presente en el HTML inicial — no requiere ejecutar JavaScript del cliente.

Esto tiene varias ventajas sobre parsear selectores HTML:
- Es más estable: si Zillow cambia las clases CSS o la estructura del DOM, el JSON sigue teniendo los mismos campos
- Es más completo: contiene campos que no se muestran en pantalla (coordenadas exactas, zpid, homeType, etc.)
- Es más simple: un regex para extraer el JSON + `json.loads()`, sin BeautifulSoup ni selectores frágiles

### Estructura del JSON extraído

Dentro de `__NEXT_DATA__`, el path relevante es:

```
props → pageProps → searchPageState → cat1 → searchResults → listResults[]
props → pageProps → searchPageState → cat1 → searchResults → mapResults[]
```

`listResults` contiene los listings visibles en la vista de lista. `mapResults` puede contener listings adicionales visibles en el mapa pero no en la lista. El scraper extrae ambos y deduplica por `zpid`.

### Paginación

Zillow usa el patrón `/pr/{N}_p/` para paginación. La página 1 es `/pr/`, la página 2 es `/pr/2_p/`, etc. El filtro de búsqueda viaja como query param `searchQueryState` en formato JSON URL-encoded.

---

## Obstáculos encontrados y cómo se resolvieron

### 1. PerimeterX captcha "Press & Hold"

**Qué pasó:** Con Playwright headless, Zillow servía una página de captcha con el mensaje "Press & Hold to confirm you are a human". El screenshot lo confirmó: era el challenge biométrico de PerimeterX que mide la interacción física del usuario con el botón.

**Por qué no se pudo resolver con browser:** PerimeterX en su versión actual analiza múltiples señales simultáneas: canvas fingerprint, WebGL renderer, AudioContext, timing de eventos de mouse, y señales de red. `playwright-stealth` parchea algunas de estas señales pero no todas — en particular, el canvas fingerprint y el timing del mouse siguen siendo detectables.

**Solución:** Cambio de approach completo a `curl_cffi` con TLS spoofing, eliminando la necesidad de un browser.

### 2. IP de VPN gratuita en blacklist

**Qué pasó:** Con Windscribe Free (servidor US) y `curl_cffi`, Zillow devolvía HTTP 403 consistente con el body `px-captcha` desde el primer request. La IP de datacenter de Windscribe está en las blacklists de PerimeterX — el bloqueo era a nivel de IP, anterior a cualquier evaluación de headers o TLS.

**Solución:** Usar el celular como hotspot. Las IPs de operadoras móviles son IPs residenciales/carrier que no están en las blacklists de anti-bot. Con esta IP, el primer request devolvió HTTP 200 y el scraping comenzó normalmente.

### 3. Rate limiting en página 14

**Qué pasó:** Después de 13 páginas (533 listings) exitosas, el request a la página 14 falló con `BoringSSL SSL_read: error:1e000065:Cipher functions:OPENSSL_internal:BAD_DECRYPT`. Este error ocurre cuando PerimeterX detecta un patrón automatizado y interrumpe activamente el handshake TLS, corrompiendo la respuesta para que el cliente no pueda procesarla.

**Por qué es el límite esperado:** El challenge especifica que el scraper debe avanzar hasta donde la IP lo permita sin proxies rotativos. 13 páginas con una IP de hotspot móvil es un resultado dentro de lo esperable para este tipo de anti-bot. El comportamiento está documentado y logueado en `scraper.log`.

**Cómo mitigarlo en producción:** Con un pool de proxies residenciales rotativos (Brightdata, Oxylabs), cada request o cada página puede ir por una IP diferente, eliminando el patrón que PerimeterX detecta.

---

## Preguntas de arquitectura

### 1. Playwright consume 1.8 GB RAM y cada página tarda el doble

**Diagnóstico:** Memory leak en el contexto del browser. Las imágenes, recursos de red y event handlers se acumulan sin liberarse porque el contexto vive durante toda la sesión.

**Solución principal:** Cerrar y recrear el contexto cada N páginas:
```python
await context.close()
context = await browser.new_context(...)
await stealth_async(page)
```
Esto libera toda la memoria del renderer sin reiniciar el proceso.

**Solución complementaria:** Bloquear recursos innecesarios:
```python
await page.route("**/*.{png,jpg,gif,css,font,woff}", lambda r: r.abort())
```
Las imágenes y hojas de estilo no aportan nada al scraping pero consumen RAM del renderer y bandwidth.

**En PM2:** `max_memory_restart: '800M'` reinicia el proceso antes de llegar a swap, combinado con el campo `status` en SQLite para resume automático.

### 2. Mitad de los registros tienen `price = null` y `address = null`

**Causa más probable:** Zillow realizó un cambio silencioso en la estructura del JSON de `__NEXT_DATA__`. Movió los campos a otra key o los renombró. Como el JSON parsea correctamente, no hay excepción — el scraper simplemente extrae `None` en lugar de crashear.

**Cómo detectarlo proactivamente:**
- Validación post-extracción: si `price` es `None` en más del 20% de los listings de una página, loguear `WARNING` y guardar el JSON crudo.
- Query de monitoreo periódico: `SELECT COUNT(*) FROM listings WHERE price IS NULL AND status = 'done'`. Si la cifra sube abruptamente entre dos ejecuciones, algo cambió en el schema.
- El campo `data` (JSON completo) actúa como backup: si los campos normalizados son `null` pero `data` tiene contenido, el problema es el mapeo, no el scraping.

**Cómo corregirlo:** Inspeccionar el JSON crudo en `data` para encontrar los nuevos paths y actualizar `normalize_listing()`.

### 3. Deploy urgente con el scraper en página 15 de 40

El campo `status` con `ON CONFLICT DO UPDATE WHERE status != 'done'` resuelve esto:

1. `pm2 stop scraper` — envía SIGTERM, el proceso termina limpiamente. La página en curso queda en `pending` o sin registrar (no en `done`).
2. Deploy: `git pull && pm2 restart scraper`.
3. El scraper arranca, detecta los zpids con `status = done` y los saltea. Los `pending` se reprocesarán.

La garantía es que un listing con `status = done` nunca se sobreescribe, por lo que los datos ya recolectados son seguros independientemente de cuándo se interrumpa el proceso.

### 4. 500 URLs con máximo 10 concurrentes y retry con backoff

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
import random, time

def scrape_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            return fetch_page(session, url)
        except Exception:
            wait = 5 * (2 ** attempt) + random.uniform(0, 5)  # jitter
            time.sleep(wait)
    return None

with ThreadPoolExecutor(max_workers=10) as executor:
    futures = {executor.submit(scrape_with_retry, url): url for url in urls}
    for future in as_completed(futures):
        result = future.result()
```

**El problema del thundering herd:** Si los 10 workers fallan simultáneamente (bloqueo de IP temporal), los 10 entran en backoff de 30 segundos y se liberan todos a la vez — provocando el mismo spike que generó el bloqueo. Es un ciclo autorreferente.

**Solución — jitter:** `sleep(base * 2^attempt + random(0, 5))`. La variación aleatoria desincroniza los workers. Los reintentos se distribuyen en el tiempo en lugar de concentrarse en un spike.

### 5. Escalar a miles de listings sin ser bloqueado

**Limitaciones del approach actual:**
- Una sola IP → Zillow detecta el volumen y aplica rate limiting progresivo.
- Una sola sesión curl_cffi → mismo TLS fingerprint en todos los requests → patrón detectable.

**Cambios necesarios para escala:**
- **Pool de proxies residenciales rotativos:** cada request o cada página usa una IP diferente. Brightdata y Oxylabs ofrecen IPs de ISPs reales que no están en blacklists.
- **Múltiples sesiones curl_cffi** con diferentes fingerprints (`chrome110`, `chrome116`, `chrome124`, `safari17`) para variar el JA3 hash.
- **Rate limiting inteligente:** no más de N requests por minuto por IP, con delays variables entre páginas.
- **Queue distribuida con Redis:** desacoplar el discovery de URLs del scraping de detalle. Permite escalar horizontalmente añadiendo workers.
- **Rotación de User-Agent coordinada con el fingerprint:** usar el UA correspondiente al `impersonate` elegido para cada sesión.

### 6. Pool de proxies rotativos

**Estructura de datos:**
```python
proxies = [
    {"url": "http://...", "fail_count": 0, "last_used": 0, "blocked": False},
    ...
]
```

**Selección:** El proxy con menor `fail_count` cuyo `last_used` sea el más antiguo (round-robin ponderado por salud). Esto distribuye la carga equitativamente y prioriza los proxies más saludables.

**Detección de bloqueo:** Un proxy está bloqueado si devuelve consistentemente 403/429, o si el HTML recibido no contiene `__NEXT_DATA__` (indica que Zillow sirvió una página de captcha). Tras 3 fallos consecutivos: `blocked = True`, excluirlo por 1 hora.

**En curl_cffi:**
```python
session = curl_requests.Session(
    impersonate="chrome124",
    proxies={"https": proxy_url}
)
```

Si el request falla con el proxy actual, marcar como fallido, seleccionar el siguiente del pool y reintentar sin incrementar el contador de reintentos del listing.

---

## Estructura del proyecto

```
zillow_scraper/
├── scraper.py         # lógica principal
├── listings.db        # SQLite (generado al correr)
├── scraper.log        # log completo de ejecución
├── README.md          # este archivo
└── debug_fetch.py     # script de diagnóstico (no requerido en producción)
```

## Schema de la base de datos

| Campo | Tipo | Descripción |
|---|---|---|
| zpid | TEXT PK | ID único de Zillow |
| url | TEXT | URL del listing en zillow.com |
| listing_status | TEXT | FOR_SALE o FOR_RENT |
| property_type | TEXT | HOUSE o APARTMENT |
| latitude | REAL | Coordenada geográfica |
| longitude | REAL | Coordenada geográfica |
| price | INTEGER | Precio en USD |
| bedrooms | INTEGER | Cantidad de habitaciones |
| bathrooms | REAL | Cantidad de baños |
| living_area | INTEGER | Superficie en sq ft |
| address | TEXT | Dirección completa |
| description | TEXT | Descripción (max 2000 chars) |
| photo_url | TEXT | URL de la foto principal |
| data | TEXT | JSON crudo completo del listing |
| scraped_at | TEXT | Timestamp ISO 8601 UTC |
| status | TEXT | pending / done |
