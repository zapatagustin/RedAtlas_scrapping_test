# Zillow PR Scraper - Challenge Técnico

Este proyecto consiste en un scraper de alto rendimiento diseñado para extraer listings de propiedades en Puerto Rico desde Zillow. La solución supera protecciones avanzadas como el firewall de PerimeterX y bloqueos de IP de datacenter mediante técnicas de mimetismo de red y persistencia de datos.

## 1. Instrucciones de Instalación y Ejecución

**Requisitos:** Python 3.10+ y conexión a internet (se recomienda el uso de hotspot móvil para evitar bloqueos por reputación de IP).

1. [ ] **Clonar el repositorio:**
   ```bash
   git clone https://github.com/zapatagustin/RedAtlas_scrapping_test
   cd RedAtlas_scrapping_test
   pip install poetry
   poetry install
   poetry run python scraper.py
   ```

2. **Verificar salud de la DB (opcional):**
   ```bash
   poetry run python healthcheck.py
   ```
   Reporta totales por status y % de campos null. Exit code 1 si supera umbrales (WARNING >5%, CRÍTICO >15%).

3. **Automatizar health check con cron (para despliegue en servidor):**
   ```bash
   # Dar permisos de ejecución (solo primera vez):
   chmod +x cron_healthcheck.sh

   # Editar crontab:
   crontab -e

   # Agregar esta línea (corre cada hora):
   0 * * * * /ruta/al/proyecto/cron_healthcheck.sh >> /var/log/scraper_health.log 2>&1
   ```
   - Logs en `healthcheck.log` (rotación automática a 1000 líneas)
   - Alertas en `healthcheck_alerts.log` si el check falla

4. **Inspeccionar listings fallidos:**
   ```sql
   SELECT * FROM listings WHERE status = 'failed';
   ```

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

Pregunta 1: Tu scraper con Playwright lleva 2 horas corriendo, no hay errores, pero el proceso está consumiendo 1.8 GB de RAM y cada página tarda el doble que al inicio. ¿Cómo diagnosticás qué está pasando y cómo lo resolverías? ¿Qué cambiarías en tu código o en la config de PM2?

Esto indica un memory leak en el navegador headless de Playwright. Probablemente el garbage collector no da abasto y acumula basura en el heap, saturando la RAM y forzando la paginación del sistema operativo, lo que ralentiza el proceso y el event loop de Node.js. Para diagnosticarlo, usaría pm2 monit y page.metrics() para trackear el crecimiento de memoria. Como solución, reduciría el ciclo de vida del navegador a 50-100 páginas antes de reiniciarlo y optimizaría el scraper deshabilitando imágenes o CSS innecesarios. Finalmente, configuraría max_memory_restart en PM2 como medida de contención ante picos críticos.  

Pregunta 2: El scraper corrió sin errores durante 3 días, pero al revisar la base de datos te das cuenta que la mitad de los registros tienen price = null y address = null. No hubo ningún crash ni log de error. ¿Qué pudo haber pasado y cómo diseñarías el sistema para detectar esto antes de que ocurra?

El fallo silencioso sugiere un cambio en la estructura de __NEXT_DATA__ en Zillow. Al no haber validación, el sistema extrae nulos sin alertar, ensuciando la base de datos. Para detectarlo antes, implementaría librerías como Zod para validar objetos y un Circuit Breaker que detenga el proceso y envíe alertas (ej. Slack) tras varios fallos. También sumaría un dashboard para monitorear campos null y un cron que verifique la salud de los datos. Como recurso rápido, programaría el scraper para alertar si un porcentaje determinado de elementos resultan en null, permitiendo ajustar el mapeo manualmente sin perder tiempo de ejecución.  

Pregunta 3: Tenés un scraper corriendo 24/7 en PM2 y necesitás deployar un fix urgente. El proceso está en el medio de una corrida, procesando la página 15 de 40. ¿Cómo hacés el deploy sin perder el progreso ni duplicar registros ya procesados?

Este problema ocurre porque el progreso en RAM se elimina al reiniciar. La mejor solución es que el script use un UPSERT y deduplicación en la base de datos para no arriesgar la pérdida de datos, con una granularidad fina. Esto garantiza que ante cualquier eventualidad (corte de luz, crash, problemas en el deploy) se mantenga la integridad de los datos. Si bien esto retrasaría un poco la ejecución, el cambio sería mínimo y casi imperceptible, volviendo al sistema mucho más robusto al no confiar en el estado de la memoria.  

Pregunta 4: Necesitás procesar 500 URLs en paralelo en Node.js pero con un máximo de 10 concurrentes. ¿Cómo lo implementás? Si uno de los 10 workers falla y hace retry con backoff de 30 segundos, ¿qué problema podría aparecer cuando todos los workers fallan al mismo tiempo? ¿Cómo lo evitás?

Este es el clásico problema de Thundering Herd. Si los 10 workers fallan y reintentan tras 30 segundos exactos, se liberarán al mismo tiempo provocando bloqueos recurrentes, spikes de CPU y riesgo de que el firewall lo tome como un ataque DoS. Esto podría traer problemas legales por incumplir términos de servicio. Para evitarlo, implementaría un Jitter que distribuya uniformemente las peticiones mediante una fórmula de exponential backoff con ruido aleatorio: tiempo = (base * 2^intento) + ruido_aleatorio. Así, las peticiones se dispersan en el tiempo evitando picos de tráfico sincronizados.

Pregunta 5: Dado el scraper que construiste, ¿cómo lo modificarías para procesar miles de listings en paralelo sin que Zillow te bloquee? ¿Qué partes del código cambiarían, qué limitaciones tiene tu approach actual y cómo las resolverías?

El problema es que una sola IP y un comportamiento lineal son fáciles de identificar para los bots de Zillow. Durante las pruebas, el servicio se detenía en la página 14 por esta razón. Como solución, es necesario utilizar un pool de proxies rotativos residenciales y múltiples sesiones de curl_cffi con diferentes fingerprints para variar el TLS. También implementaría un rate limit inteligente y una arquitectura distribuida con colas en Redis para separar el "discovery" de URLs de la extracción de detalles, permitiendo escalar horizontalmente con más workers.

Pregunta 6: ¿Cómo integrarías un pool de proxies rotativos a tu scraper? Describí los cambios concretos en tu código — cómo elegirías qué proxy usar en cada request, cómo detectarías que un proxy fue bloqueado y cómo lo sacarías de rotación.

Para integrar el pool es necesario un Proxy Manager que rastree el fail_count, last_used y estado de cada IP. Utilizaría un algoritmo Round-Robin ponderado para elegir el proxy que no esté bloqueado y tenga el tiempo de uso más antiguo. En el código, inyectaría el proxy en la sesión junto con una rotación de JA3 fingerprints. Detectaría bloqueos validando códigos HTTP (403/429) o la ausencia de __NEXT_DATA__ en el HTML. Tras 3 fallos consecutivos, el proxy se sacaría de rotación por una hora de cooldown y la tarea se re-encolaría inmediatamente para no afectar el progreso de los listings.
