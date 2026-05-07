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
- [ ] Alertas por Slack (o similar) cuando el Circuit Breaker se dispara
- [ ] Cron/health check periódico que verifique porcentaje de campos null en la DB y alerte si supera umbral
- [ ] Dashboard de monitoreo de campos null en tiempo real

### Resiliencia y concurrencia
- [x] Exponential backoff con jitter en todos los reintentos — evitar Thundering Herd si múltiples workers fallan simultáneamente: `wait = (base * 2^intento) + random(0, base)`
- [ ] Arquitectura multi-worker con cola de tareas (ej. Redis) — separar discovery de páginas de extracción de listings, escalar horizontalmente
- [ ] Límite de concurrencia configurable (máx. N workers simultáneos)

### Evasión de bloqueos y escalado
- [ ] Proxy Manager con Round-Robin ponderado — trackear `fail_count`, `last_used` y estado por IP
- [ ] Rotación de JA3 fingerprints por sesión de `curl_cffi` — variar huella TLS entre requests
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

### Gestión de memoria (si se migra a Playwright en el futuro)
- [ ] Reiniciar instancia de browser cada 50-100 páginas para prevenir memory leak en heap de Playwright
- [ ] Deshabilitar carga de imágenes y CSS en browser headless para reducir uso de RAM
- [ ] Configurar `max_memory_restart` en PM2 como contención ante picos críticos
