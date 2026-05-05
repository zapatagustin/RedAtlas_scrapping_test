# Zillow PR Scraper - Challenge Técnico

Este proyecto consiste en un scraper de alto rendimiento diseñado para extraer listings de propiedades en Puerto Rico desde Zillow. La solución supera protecciones avanzadas como el firewall de PerimeterX y bloqueos de IP de datacenter mediante técnicas de mimetismo de red y persistencia de datos.

## 1. Instrucciones de Instalación y Ejecución

**Requisitos:** Python 3.10+ y conexión a internet (se recomienda el uso de hotspot móvil para evitar bloqueos por reputación de IP).

1. **Clonar el repositorio:**
   ```bash
   git clone [https://github.com/zapatagustin/RedAtlas_scrapping_test](https://github.com/zapatagustin/RedAtlas_scrapping_test)
   cd RedAtlas_scrapping_test
   pip install poetry
   poetry init --no-interaction --name "zillow-scraper" --python "^3.10"
   poetry add curl-cffi
    poetry run python scraper.py
   
2. Approach Técnico y Justificación

Se optó por una arquitectura basada en peticiones HTTP de bajo nivel utilizando la librería curl_cffi por las siguientes razones:

    Evasión de Anti-bots (TLS Spoofing): A diferencia de las librerías estándar, curl_cffi permite realizar un handshake de seguridad idéntico al de un navegador Chrome real (mismo JA3 hash), evitando la detección por huella digital TLS.

    Eficiencia de Datos: Zillow utiliza Next.js, lo que permite extraer la información directamente del objeto JSON embebido en el tag __NEXT_DATA__ del HTML inicial. Esto elimina la necesidad de renderizar JavaScript, optimizando el uso de RAM y CPU.

    Persistencia Robusta: Se implementó una base de datos SQLite con lógica de UPSERT y deduplicación por zpid. Esto garantiza que el scraper pueda reanudar su trabajo tras una interrupción sin generar datos duplicados ni perder el progreso acumulado.

3. Obstáculos Encontrados y Soluciones
A. Bloqueo Biométrico (PerimeterX)

    Obstáculo: Playwright (incluso con stealth) activaba el captcha "Press & Hold", que analiza señales biométricas y de hardware.

    Solución: Cambio a un enfoque browserless con curl_cffi, eliminando las señales de telemetría que delataban el entorno automatizado.

B. Reputación de IP (Blacklists de Datacenter)

    Obstáculo: IPs de VPNs comerciales (como Windscribe) eran bloqueadas inmediatamente (403) por pertenecer a centros de datos.

    Solución: Uso de un hotspot móvil. Las IPs móviles tienen reputación residencial, lo que permite el acceso normal al sitio sin ser filtrado por el firewall.

C. Intento de Despliegue en Cloud (GitHub Codespaces)

    Qué se intentó: Se probó ejecutar el scraper en GitHub Codespaces para automatizar la ejecución.

    Resultado: Bloqueo inmediato. Las IPs de Codespaces pertenecen a rangos de Microsoft Azure, los cuales Zillow tiene identificados como tráfico no humano y bloquea preventivamente.

    Conclusión: Esto confirmó que, sin proxies residenciales auténticos, la ejecución desde servidores de nube no es viable para este objetivo.

D. Rate Limiting Activo

    Obstáculo: Tras procesar ~533 listings, el servidor interrumpió la conexión (error BAD_DECRYPT), una técnica para corromper el flujo TLS tras detectar patrones automatizados persistentes.

    Solución: Se documentó como el límite esperado para una sola IP residencial. En producción, esto se resuelve integrando un pool de proxies residenciales rotativos.

4. Respuestas al Cuestionario de Arquitectura

    Q1: Diagnóstico de consumo de RAM y lentitud: Esto indica un memory leak en el navegador headless de Playwright donde el garbage collector no libera los contextos correctamente, saturando la RAM y forzando la paginación del SO. Diagnosticaría con pm2 monit y page.metrics(). Solución: reiniciar el navegador cada 50-100 páginas y configurar max_memory_restart en PM2.

    Q2: Detección de fallos silenciosos (Campos Null): Surgió por un cambio en la estructura de __NEXT_DATA__. Prevención: usar Zod para validar objetos y un Circuit Breaker que detenga el proceso tras detectar fallos consecutivos para evitar ensuciar la base de datos. También implementaría un cron para verificar la salud de los datos periódicamente.

    Q3: Despliegue sin pérdida de progreso: No se debe confiar en el estado de la memoria RAM. La solución es el uso de UPSERT y deduplicación en la base de datos con granularidad fina, garantizando que el scraper verifique qué registros ya están marcados como done tras un reinicio.

    Q4: Concurrencia y Thundering Herd: Si 10 workers reintentan tras 30 segundos exactos, se sincronizan creando picos de tráfico que el firewall detecta como DoS. Solución: implementar un Jitter (ruido aleatorio) en el reintento: tiempo = (base * 2^intento) + aleatorio.

    Q5: Escalado masivo anti-bloqueo: Es necesario superar la limitación de una sola IP mediante una arquitectura distribuida con colas en Redis e integrando un pool de proxies residenciales rotativos. También usaría múltiples sesiones con diferentes JA3 fingerprints para variar el perfil TLS en cada petición.

    Q6: Integración de Proxies Rotativos: Implementaría un Proxy Manager con algoritmo Round-Robin ponderado por salud (fail_count). Detectaría bloqueos validando códigos 403/429 o la ausencia de datos en el HTML. Los proxies fallidos entrarían en cooldown, mientras que la tarea se re-encolaría inmediatamente.
