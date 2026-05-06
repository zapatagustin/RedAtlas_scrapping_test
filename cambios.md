# Cambios

## 2026-05-06

### Fix: eliminación de overhead en reintentos de API con 403

**Problema:** `fetch_api` reintentaba 3 veces con backoff exponencial (5s + 10s + 20s = 35s) ante respuestas 403 de Zillow. Como el 403 es un bloqueo de política consistente (no un error transitorio), estos reintentos nunca servían. Con 20 páginas, el overhead total era ~700s de espera innecesaria.

**Solución:** Al recibir 403, `fetch_api` retorna `None` inmediatamente y pasa al fallback HTML sin sleep ni reintentos. El código 429 (rate limit real) conserva el backoff, ya que sí es transitorio.

**Archivo:** `scraper.py` — función `fetch_api`, manejo de status codes 403/429.
