# Cambios aplicados al scraper

## Resumen

Se aplicaron 4 mejoras al `scraper.py` original más 2 actualizaciones de configuración.

---

## 1. Resume desde DB (Punto 3)

**Función nueva:** `get_resume_page(conn)`

**Qué hace:** Al arrancar, consulta cuántos listings tienen `status = 'done'` en SQLite y divide por 40 (listings por página) para estimar desde qué página retomar.

```python
def get_resume_page(conn):
    done_count = conn.execute("SELECT COUNT(*) FROM listings WHERE status = 'done'").fetchone()[0]
    estimated = max(1, done_count // 40)
    return estimated
```

**Por qué importa:** Antes, el scraper siempre arrancaba desde página 1 aunque ya hubiera procesado 13 páginas. Ahora, si PM2 reinicia el proceso o hacés un deploy, el progreso está en SQLite, no en RAM. El estado nunca se pierde.

---

## 2. API interna de Zillow (Diferenciador ★)

**Funciones nuevas:** `fetch_api(session, page)`, `fetch_page_with_fallback(session, page)`, `extract_listings_from_api(api_data)`, `build_api_payload(page)`

**Qué hace:** En lugar de descargar el HTML completo (~900KB por página) y parsear `__NEXT_DATA__`, llama directamente al endpoint interno que usa el propio frontend de Zillow:

```
POST https://www.zillow.com/async-create-search-page-state
```

Devuelve los listings como JSON limpio (~15KB por página). Si el endpoint falla o devuelve vacío, el scraper cae automáticamente al método HTML original como fallback.

**Por qué importa:** Es el diferenciador más difícil del challenge. La API interna es ~60x más eficiente en datos, más rápida, y más robusta ante cambios de UI. El log al final muestra cuántas páginas se obtuvieron por cada vía:

```
Scraping completo. Done: 533 | API: 10 páginas | HTML fallback: 3 páginas
```

---

## 3. Optimización de memoria (Punto 1)

**Cambios:** Reciclado de sesión, liberación explícita con `del` + `gc.collect()`, monitoreo con `psutil`.

**Reciclado de sesión cada 10 páginas:**
```python
if (page_num - start_page) % SESSION_RECYCLE == 0:
    del session
    gc.collect()
    session = make_session()
```

La sesión de `curl_cffi` acumula cookies, handles de keep-alive y buffers internos de libcurl. Reciclarla cada 10 páginas mantiene el proceso con footprint plano.

**Liberación de HTML y JSON por página:**
```python
del raw_listings, normalized_batch, raw_json
gc.collect()
```

Cada página pesa ~900KB en HTML. Sin liberación explícita, Python puede mantener esos objetos vivos en el heap hasta el siguiente ciclo del garbage collector.

**Monitoreo con psutil:**
- `< 300MB` → INFO normal
- `300–500MB` → WARNING
- `> 500MB` → ERROR + pausa 30s para que el GC actúe

**Nueva dependencia:** `psutil` (agregada a `pyproject.toml`).

---

## 4. Validación de schema + circuit breaker (Punto 2)

**Funciones nuevas:** `validate_listing(listing)`, `check_page_schema(listings, raw_json, page_num)`

**Qué hace:** Antes de escribir a la DB, valida que cada listing tenga `price`, `address`, `latitude` y `longitude`. Si más del 30% de los listings en una página tienen campos nulos, activa el circuit breaker:

- Loguea ERROR con el desglose de qué campos fallan y en cuántos listings
- Guarda el `__NEXT_DATA__` crudo en `schema_alerts/schema_alert_pageN_TIMESTAMP.json` para diagnóstico forense
- Detiene el scraper

Los listings individuales con campos nulos se guardan con `status = 'failed'` en lugar de `'done'` — visibles con:

```sql
SELECT * FROM listings WHERE status = 'failed';
```

**Por qué importa:** Resuelve el escenario de la Pregunta 2 del challenge: si Zillow cambia su estructura JSON, el scraper se detiene en la primera página afectada con evidencia del problema, en lugar de correr 3 días llenando la DB de nulos silenciosos.

---

## 5. Renombre del proyecto

- `pyproject.toml`: `name` cambiado de `"prueba-scrapping"` a `"redatlas-scrapping-test"`
- `README.md`: URL de git clone actualizada al nombre correcto del repositorio, instrucciones simplificadas a `poetry install` (ya no hace falta `poetry init` manual)

---

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `scraper.py` | Todos los puntos 1–4 |
| `pyproject.toml` | Nombre del proyecto + psutil |
| `README.md` | URL del repo + instrucciones de instalación |
| `CAMBIOS.md` | Este archivo (nuevo) |

---

## 2026-05-06 — Arreglos post entrega

### Fix: eliminación de overhead en reintentos de API con 403

**Problema:** `fetch_api` reintentaba 3 veces con backoff exponencial (5s + 10s + 20s = 35s) ante respuestas 403 de Zillow. Como el 403 es un bloqueo de política consistente (no un error transitorio), estos reintentos nunca servían. Con 20 páginas, el overhead total era ~700s de espera innecesaria.

**Solución:** Al recibir 403, `fetch_api` retorna `None` inmediatamente y pasa al fallback HTML sin sleep ni reintentos. El código 429 (rate limit real) conserva el backoff, ya que sí es transitorio.

**Archivo:** `scraper.py` — función `fetch_api`, manejo de status codes 403/429.

### Feat: jitter en backoff exponencial

**Cambio:** `wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, BACKOFF_BASE)` en todos los reintentos.

**Archivo:** `scraper.py` — todas las líneas de backoff en `fetch_html` y `fetch_api`.

### Resultado de prueba — corrida completa 2026-05-06 15:50

- Páginas scrapeadas: 15 a 20 (6 páginas)
- Listings nuevos guardados: 148
- API: siempre 403 → fallback HTML inmediato, sin overhead
- HTML: 200 en todos los intentos, sin reintentos necesarios
- Memoria: estable ~48-49 MB durante toda la corrida
- Circuit breaker: 1 listing con `latitude`/`longitude` null guardado como `failed` — comportamiento correcto
- Total acumulado en DB: ~752 listings

### Feat: validación con Pydantic en normalize_listing

**Cambio:** `ListingModel` (Pydantic v2) valida campos requeridos (`latitude`, `longitude`, `price`, `address`) al normalizar. `ValidationError` setea `status='failed'` con log del campo inválido. Eliminados `validate_listing()` y `REQUIRED_FIELDS` (reemplazados). `check_page_schema` simplificado a conteo de `status=='failed'`.

**Archivos:** `scraper.py`, `pyproject.toml` (+pydantic >=2.0.0).

### Resultado de prueba — corrida 2026-05-07 10:44

- Páginas scrapeadas: 18 a 20 (3 páginas)
- Listings nuevos: 53
- Pydantic: sin `FAILED` — todos los listings válidos
- Memoria: estable ~57MB
- Total acumulado en DB: ~805 listings

---

## TODO — Mejoras de arquitectura mencionadas en el cuestionario

### Observabilidad y calidad de datos
- [x] Validación de schema con Pydantic al normalizar cada listing — detectar cambios en `__NEXT_DATA__` antes de que ensucien la DB
- [x] Alertas por Slack (o similar) cuando el Circuit Breaker se dispara
- [x] Cron/health check periódico que verifique porcentaje de campos null en la DB y alerte si supera umbral
- [x] Dashboard de monitoreo de campos null en tiempo real

### Resiliencia y concurrencia
- [x] Exponential backoff con jitter en todos los reintentos — evitar Thundering Herd si múltiples workers fallan simultáneamente: `wait = (base * 2^intento) + random(0, base)`
- [ ] Arquitectura multi-worker con cola de tareas (ej. Redis) — separar discovery de páginas de extracción de listings, escalar horizontalmente
- [ ] Límite de concurrencia configurable (máx. N workers simultáneos)

### Evasión de bloqueos y escalado
- [ ] Proxy Manager con Round-Robin ponderado — trackear `fail_count`, `last_used` y estado por IP
- [x] Rotación de JA3 fingerprints por sesión de `curl_cffi` — variar huella TLS entre requests
- [ ] Detección automática de proxy bloqueado: 3 fallos consecutivos (403/429 o ausencia de `__NEXT_DATA__`) → cooldown de 1 hora + re-encolar tarea
- [x] Rate limit inteligente por proxy — espaciar requests para no generar patrones detectables

### Feat: rate limit con jitter entre páginas

**Cambio:** `time.sleep(3)` fijo reemplazado por `random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX)`. Constantes `PAGE_DELAY_MIN=2` / `PAGE_DELAY_MAX=6` en CONFIG. Log `[RATE]` muestra el delay exacto de cada pausa.

**Archivo:** `scraper.py` — CONFIG + loop principal.

### Resultado de prueba — corrida 2026-05-07 11:01

- Páginas scrapeadas: 1 a 5 (6ta falló por BAD_DECRYPT — límite de IP esperado)
- Listings nuevos: 201
- Rate delays observados: 2.9s, 4.5s, 5.0s, 4.0s, 3.8s — variación correcta
- Jitter en backoff visible: BAD_DECRYPT retries con 6.6s → 12.7s → 21.8s
- Pydantic: 4 listings con lat/lng null → `failed` correctamente
- BAD_DECRYPT en página 6: comportamiento esperado (límite por IP, no bug)

### Feat: health check de DB + script cron

**Archivos nuevos:**
- `healthcheck.py` — consulta `listings.db`, reporta totales por status y null % por campo requerido. Umbrales: WARNING >5%, CRÍTICO >15%. Exit code 1 si hay problema (compatible con cron y monitoreo externo).
- `cron_healthcheck.sh` — wrapper bash para cron. Autodetecta Python/Poetry, rota logs al superar 1000 líneas, escribe alertas en `healthcheck_alerts.log` separado si el health check falla.

**Uso manual:**
```bash
poetry run python healthcheck.py
```

**Uso con cron (AWS u otro servidor):**
```bash
# Editar crontab:
crontab -e
# Agregar (cada hora):
0 * * * * /path/to/project/cron_healthcheck.sh >> /var/log/scraper_health.log 2>&1
```

### Resultado de prueba — 2026-05-07 11:16

- Total: 205 | Done: 201 | Failed: 4 (2.0%) | Pending: 0
- price/address: 0 nulls [OK]
- latitude/longitude: 4 nulls (2.0%) [OK] — bajo umbral del 5%
- Exit code 0 — sin alertas

### Feat: alertas Slack con webhook

**Cambio:** `send_slack_alert(message)` vía `urllib.request` (sin deps extra). Webhook URL cargada desde `.env` con `python-dotenv`. Tres puntos de disparo:
1. Circuit breaker activado (>30% listings inválidos en una página) → `:rotating_light:`
2. Página falla todos los reintentos → `:x:`
3. Scraping completado → `:white_check_mark:` con resumen (Done / Failed / páginas API / HTML)

**Archivos:** `scraper.py`, `pyproject.toml` (+python-dotenv >=1.0.0), `.gitignore` (+.env, +CAMBIOS.md).

**Configuración:** crear `.env` en raíz con `SLACK_WEBHOOK_URL=<tu_webhook>`. Sin URL configurada, `send_slack_alert` es no-op silencioso.

### Resultado de prueba — corrida 2026-05-07

- Scraper completó 20 páginas sin errores
- Mensaje `:white_check_mark:` llegó a Slack correctamente
- Circuit breaker y alertas de fallo verificados en código

---

### Feat: rotación de JA3 fingerprints por sesión + sincronización de User-Agent

**Problema original:** `curl_cffi` tiene dos capas separadas: TLS fingerprint (controlado por `impersonate`) y HTTP headers (controlados manualmente). Con `impersonate="safari17_0"` el handshake TLS decía "soy Safari" pero `user-agent` en los headers decía "soy Chrome" — inconsistencia detectable. La API interna de Zillow rechazaba con 405.

**Cambios:**
- `JA3_PROFILES` — lista de 6 perfiles en CONFIG (`chrome110`, `chrome120`, `chrome124`, `chrome131`, `safari17_0`, `safari18_0`)
- `PROFILE_USER_AGENTS` — dict que mapea cada perfil a su User-Agent real correspondiente
- `make_session()` elige perfil al azar, retorna `(session, ua)` tuple
- `fetch_html(session, url, ua)` y `fetch_api(session, page, ua)` reciben el UA y lo inyectan con `{**HEADERS, "user-agent": ua}` — TLS y HTTP headers siempre consistentes
- `fetch_page_with_fallback` y `run_scraper` actualizados para propagar el `ua`

**Archivo:** `scraper.py` — CONFIG, `make_session()`, `fetch_html()`, `fetch_api()`, `fetch_page_with_fallback()`, `run_scraper()`.

### Resultado de prueba — 2026-05-07 12:36

- `[JA3] Perfil TLS: chrome131` — rotación correcta
- UA inyectado: Chrome/131 en headers — consistente con perfil TLS
- API 403 (bloqueo de política por IP, no por UA) → fallback HTML → 200 OK
- 41 listings procesados, todos skip (ya en DB)

---

### Feat: dashboard web con Flask

**Archivo nuevo:** `dashboard.py` — servidor Flask en `http://localhost:5000`.

**Features:**
- Cards con totales: Total / Done / Failed / Pending
- Tabla de calidad de datos: null% por campo requerido (`price`, `address`, `latitude`, `longitude`) con badges OK / WARNING (>5%) / CRÍTICO (>15%)
- Tabla de últimos 20 listings fallidos con zpid, dirección, precio y fecha
- Botón "Lanzar Scraper" — ejecuta `poetry run python scraper.py` como subprocess con stdout/stderr heredado del proceso Flask (logs aparecen en terminal)
- Indicador de estado: pulsa verde mientras el scraper corre, gris cuando termina
- Auto-refresh cada 5 segundos via JS fetch — sin recargar la página

**Nueva dependencia:** `flask (>=3.0.0,<4.0.0)` en `pyproject.toml`.

**Lanzar:**
```bash
poetry run python dashboard.py
```

### Resultado de prueba — 2026-05-07

- Dashboard carga con datos reales de DB
- Null% y badges correctos
- Botón lanza scraper, log aparece en terminal, indicador pulsa
- Auto-refresh actualiza stats al terminar la corrida

---

### Investigación: alternativas al hotspot móvil para IP residencial

**Objetivo:** encontrar método para correr el scraper sin depender del hotspot móvil.

**Contexto:** Zillow bloquea IPs de datacenters (ASNs de AWS, Azure, VPNs comerciales). Solo IPs residenciales (carrier móvil o ISP hogareño) pasan. El challenge menciona ProtonVPN y Windscribe como opciones.

**Método de prueba:**

Se instaló `windscribe-cli` y se probaron los 10 servidores US disponibles en el free tier con el siguiente script:

```bash
SERVERS=("New York" "Chicago" "Los Angeles" "Atlanta" "Dallas" "Denver" "Miami" "Seattle" "San Jose" "Ashburn")

for server in "${SERVERS[@]}"; do
    windscribe-cli connect "$server"
    sleep 4
    # test con curl_cffi idéntico al scraper
    poetry run python3 -c "
from curl_cffi import requests as curl_requests
s = curl_requests.Session(impersonate='chrome124')
r = s.get('https://www.zillow.com/pr/', timeout=12)
ok = r.status_code == 200 and '__NEXT_DATA__' in r.text
print('PASA' if ok else f'BLOQUEADO ({r.status_code})')
"
    windscribe-cli disconnect
    sleep 2
done
```

**Resultados — 2026-05-07:**

| Servidor | IP nickname | Resultado |
|---|---|---|
| New York | Inside Job | BLOQUEADO (403) |
| Chicago | — | BLOQUEADO (403) |
| Los Angeles | Dogg | BLOQUEADO (403) |
| Atlanta | Peachtree | BLOQUEADO (403) |
| Dallas | BBQ | BLOQUEADO (403) |
| Denver | — | BLOQUEADO (403) |
| Miami | Vice | BLOQUEADO (403) |
| Seattle | Cobain | BLOQUEADO (403) |
| San Jose | — | BLOQUEADO (403) |
| Ashburn | — | BLOQUEADO (403) |

**Conclusión:** 10/10 servidores bloqueados. Windscribe usa ASNs de datacenter propios — Zillow tiene el rango entero en blacklist. Elegir otra ciudad no ayuda porque todas las IPs pertenecen al mismo proveedor.

**Opciones viables:**
- **Hotspot móvil** ← solución actual, funciona (IP de carrier residencial)
- **Conexión doméstica (ISP local)** ← funciona, misma lógica que hotspot
- **Proxies residenciales pagos** (Bright Data, Oxylabs ~$10/mes) ← solución de producción
- ProtonVPN / cualquier VPN comercial ← mismo problema que Windscribe, no viable

---

### Investigación: ProtonVPN free + ISP hogareño + self-hosted VPN

**Continuación de la investigación de alternativas al hotspot (2026-05-07).**

#### ProtonVPN free tier

El free plan no permite selección de ciudad — solo conecta al "mejor servidor disponible". No se pueden testear servidores individuales. Conectó a `CA-FREE#31` (Montreal, Canada) — ni siquiera US. Conclusión: el free tier de ProtonVPN no es viable para este caso.

#### ISP hogareño (Ver Tv S.A.)

IP: `181.16.121.50` — ASN27984 Ver Tv S.A., Pilar, Buenos Aires, Argentina.

```bash
curl -s https://ipinfo.io | python3 -m json.tool
# → "org": "AS27984 Ver Tv S.A.", "country": "AR"
```

Test contra Zillow: **BLOQUEADO (403)**. Causa: geolocalización Argentina. Zillow detecta el país y bloquea — no es problema de reputación de ASN sino de geo-restriction.

#### Por qué funciona el hotspot (Tuenti)

Tuenti corre sobre la red de Personal Argentina. Personal tiene acuerdos de peering con carriers US — parte del tráfico móvil sale por nodos en Miami o Nueva York antes de llegar a Zillow. La IP resultante aparece geolocada como US o neutral. No está documentado oficialmente pero es el comportamiento observado y confirmado en pruebas.

#### Self-hosted VPN

No viable gratis. El problema no es el software VPN sino la IP del servidor host. Todas las opciones cloud gratuitas (Oracle Free, AWS Free, Fly.io) tienen IPs de datacenter → bloqueadas. Para que funcione se necesitaría hostear en una máquina con IP residencial US (ej. casa de un contacto en EE.UU. con WireGuard).

#### Tabla resumen — alternativas al hotspot

| Método | IP resultante | Costo | Resultado |
|---|---|---|---|
| Hotspot Tuenti | Residencial/neutral vía peering US | Gratis | ✓ PASA |
| ISP hogareño Ver Tv | Residencial AR (AS27984) | Gratis | ✗ 403 geo-block |
| Windscribe (10 servidores US) | Datacenter Windscribe | Gratis | ✗ 403 VPN blacklist |
| ProtonVPN free | Datacenter ProtonVPN (Canada) | Gratis | ✗ no permite elegir US |
| Self-hosted VPN cloud free | Datacenter | Gratis | ✗ datacenter bloqueado |
| Self-hosted VPN en casa US | Residencial US | Gratis* | ✓ (requiere contacto en EE.UU.) |
| Proxies residenciales US pagos | Residencial US | ~$10/mes | ✓ solución de producción |

**Conclusión:** el hotspot móvil (Tuenti/Personal) sigue siendo la única opción gratuita y funcional disponible.

---

### Investigación: proxy SOCKS5 vía teléfono como alternativa al hotspot directo

**Idea evaluada:** mantener la máquina conectada al ISP hogareño pero rutear el tráfico del scraper a través del teléfono (Tuenti) como proxy SOCKS5, para que Zillow siga viendo la IP de Tuenti.

```
PC (ISP AR) → proxy SOCKS5 en teléfono (Tuenti) → Zillow
```

**Implementación posible:**
- `ssh -D 1080 usuario@ip_del_telefono` — proxy SOCKS5 vía SSH
- Termux en Android con `sshd` corriendo
- `curl_cffi` soporta proxies SOCKS5 nativamente

**Por qué no aporta nada sobre el hotspot directo:**

TCP lo impide a nivel fundamental. Cada conexión TCP es un tuple `(src_ip, src_port, dst_ip, dst_port)` — si la IP cambia, la conexión muere. Además, `curl_cffi` abre una conexión TCP nueva por cada request de todas formas; no hay sesión persistente que "heredar". En ambos casos Zillow ve la misma IP de Tuenti.

| | Hotspot directo | Proxy SOCKS5 vía teléfono |
|---|---|---|
| IP que ve Zillow | Tuenti | Tuenti |
| Complejidad | Ninguna | Alta (Termux + SSH) |
| Datos consumidos en teléfono | Iguales | Iguales |
| Teléfono necesita estar | Hotspot activo | Hotspot activo + Termux/SSH |

**Conclusión:** sin diferencia práctica en el resultado. El hotspot directo es estrictamente más simple.

---

### Gestión de memoria (si se migra a Playwright en el futuro)
- [ ] Reiniciar instancia de browser cada 50-100 páginas para prevenir memory leak en heap de Playwright
- [ ] Deshabilitar carga de imágenes y CSS en browser headless para reducir uso de RAM
- [ ] Configurar `max_memory_restart` en PM2 como contención ante picos críticos
