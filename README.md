# Zillow Puerto Rico Scraper

Scraper de propiedades en Puerto Rico (Houses + Apartments, For Sale + For Rent, 2+ bedrooms) usando Playwright y SQLite.

---

## Instalación

**Requisitos:** Python 3.10+

```bash
pip install playwright
playwright install chromium
```

## Ejecución

```bash
python scraper.py
```

El proceso es **resumible**: si se interrumpe, al volver a correr retoma desde donde quedó. Los listings con `status = done` no se vuelven a procesar.

Logs en consola y en `scraper.log`.

---

## Approach técnico

**Playwright (headless Chromium) + `__NEXT_DATA__` JSON**

Zillow embebe todos los datos de cada listing en un `<script id="__NEXT_DATA__">` dentro del HTML de la página de resultados. Ese JSON tiene zpid, precio, coordenadas, fotos, descripción — todo lo que pedía el schema.

Este approach es el más directo porque:
- No hay que parsear selectores frágiles de DOM (si Zillow cambia el HTML, el JSON sigue igual)
- Un browser real pasa el fingerprinting de PerimeterX sin configuración especial
- No requiere interceptar XHR ni replicar headers — el browser lo maneja solo

La alternativa (requests directas sin browser) requeriría spooféar TLS fingerprint con `curl_cffi` o `tls-client` y es mucho más frágil. Para un challenge sin proxies, Playwright es la opción más robusta.

---

## Obstáculos encontrados

**1. Zillow requiere IP de EEUU**  
El sitio bloquea IPs no-americanas. Solución: conectarse a ProtonVPN o Windscribe (tier gratuito, sin tarjeta) con un servidor US antes de correr el scraper.

**2. PerimeterX anti-bot**  
Con browser headless real y un User-Agent moderno, Playwright pasa la mayoría de los checks. El obstáculo real aparece después de varias páginas (rate limiting por IP). El scraper incluye un delay de 3 segundos entre páginas y retry con backoff exponencial para manejar esto.

**3. Estructura del `__NEXT_DATA__` no siempre es consistente**  
Zillow tiene múltiples paths dentro del JSON dependiendo del tipo de página. Se implementaron dos paths de extracción (`listResults` y `mapResults`) para cubrir los casos más comunes.

---

## Preguntas de arquitectura

### 1. Playwright consume 1.8 GB RAM y cada página tarda el doble

**Diagnóstico:**  
El problema más común es un **memory leak en el contexto del browser**: imágenes, recursos de red y handlers de eventos que se acumulan sin liberarse. Con `pm2 logs` y `top` confirmaría si el proceso crece linealmente.

**Solución:**  
- Cerrar y recrear el contexto de browser cada N páginas (p.ej. cada 50): `await context.close(); context = await browser.newContext(...)`. Esto libera toda la memoria del contexto sin reiniciar el proceso entero.
- Bloquear recursos innecesarios con `page.route('**/*.{png,jpg,gif,css,font}', r => r.abort())` — las imágenes y CSS no aportan nada al scraping pero consumen RAM del renderer.
- En PM2: agregar `max_memory_restart: '800M'` para que PM2 reinicie el proceso antes de que llegue a swap, combinado con el campo `status` en SQLite para resume automático.

### 2. Mitad de los registros tienen `price = null` y `address = null` sin errores

**Causa probable:**  
Zillow hizo un cambio silencioso en la estructura del JSON — movió los campos a otra key dentro de `__NEXT_DATA__` o cambió los nombres. Como el scraper no crashea (el JSON parsea bien), no hay excepción; simplemente extrae `None`.

**Cómo detectarlo antes:**  
- Agregar **validación de schema** post-extracción: si `price` o `address` son `None` en más del X% de los resultados de una página, loguear una alerta `WARN` y guardar el JSON crudo para inspección manual.
- Usar el campo `data` (JSON completo) como backup: si los campos normalizados son `null` pero `data` tiene contenido, hay un problema de mapeo, no de scraping.
- Monitoreo con una query periódica: `SELECT COUNT(*) FROM listings WHERE price IS NULL AND status = 'done'` — si sube, algo cambió.

### 3. Deploy urgente con el scraper en page 15 de 40

El campo `status` en SQLite ya resuelve esto:

1. Enviar `SIGTERM` al proceso (`pm2 stop scraper`). El handler de shutdown guarda el estado actual (la página en curso queda en `pending` o `failed`, no en `done`).
2. Hacer el deploy: `git pull && pm2 restart scraper`.
3. El scraper arranca, detecta que los zpids con `status = done` ya existen y los saltea. Los que quedaron `pending` o `failed` los reprocesa.

La clave es que el `UPSERT` con `ON CONFLICT DO UPDATE WHERE status != 'done'` garantiza que nunca se sobreescriba un registro ya completado.

### 4. 500 URLs con máximo 10 concurrentes y retry con backoff

**Implementación básica (Node.js):**

```js
const pLimit = require('p-limit');
const limit = pLimit(10);
const tasks = urls.map(url => limit(() => scrapeWithRetry(url)));
await Promise.all(tasks);
```

**El problema del thundering herd:**  
Si los 10 workers fallan al mismo tiempo (p.ej. bloqueo de IP temporal), los 10 entran en backoff de 30 segundos y se liberan todos a la vez — lo que vuelve a provocar el mismo spike y el mismo bloqueo. Es un ciclo.

**Cómo evitarlo: jitter**  
Agregar variación aleatoria al backoff: `sleep(30 + random(0, 15))`. Así los workers se "desincronizán" y los reintentos se distribuyen en el tiempo en lugar de hacer un spike simultáneo.

### 5. Escalar a miles de listings sin ser bloqueado

**Limitaciones del approach actual:**  
- Una sola IP → Zillow detecta patrones de requests y bloquea.
- Un solo browser context → la memoria escala linealmente con la concurrencia.

**Cambios necesarios:**  
- **Pool de proxies rotativos** (ver pregunta 6): cada worker usa un proxy diferente.
- **Múltiples contextos de browser** en lugar de múltiples tabs — cada contexto tiene su propio fingerprint y cookies.
- **Rate limiting inteligente**: no más de N requests por minuto por IP, con jitter.
- **Queue distribuida** (Redis + workers separados): desacoplar el "discovery" de URLs del "scraping" de detalle para poder escalar horizontalmente.

### 6. Pool de proxies rotativos

**Selección de proxy por request:**  
Mantener un array de proxies con `{ url, failCount, lastUsed, blocked: bool }`. Para cada request, elegir el proxy con menor `failCount` y cuyo `lastUsed` sea el más antiguo (round-robin ponderado por salud).

**Detección de bloqueo:**  
Un proxy está bloqueado si devuelve consistentemente 403, 429, o si el contenido extraído no tiene el JSON esperado (HTML de captcha en lugar de `__NEXT_DATA__`). Tras 3 fallos consecutivos, marcar `blocked = true` y sacarlo de rotación por un tiempo configurable (p.ej. 1 hora).

**En código (Playwright):**
```python
context = await browser.new_context(
    proxy={"server": proxy_url, "username": user, "password": pwd}
)
```
Si el request falla con el proxy actual, marcar como fallido, elegir el siguiente del pool y reintentar — sin incrementar el contador de reintentos del listing en sí.

---

## Estructura del proyecto

```
zillow_scraper/
├── scraper.py      # lógica principal
├── listings.db     # SQLite (generado al correr)
├── scraper.log     # log de ejecución
└── README.md
```
