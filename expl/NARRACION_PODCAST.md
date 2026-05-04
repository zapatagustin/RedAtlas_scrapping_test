# Narración — Zillow PR Scraper
*Texto optimizado para text-to-speech. Pegar en ElevenLabs o TTSMaker, voz español latinoamericano.*

---

Hoy les voy a contar cómo construimos un scraper para extraer propiedades inmobiliarias de Zillow en Puerto Rico. Fue un proceso con varios obstáculos reales, y creo que vale la pena contarlo completo porque muestra exactamente cómo se trabaja cuando las cosas no salen a la primera.

El objetivo era simple en papel: entrar a zillow punto com, buscar casas y apartamentos en venta y alquiler en Puerto Rico, con al menos dos habitaciones, y guardar todos esos datos en una base de datos SQLite. Datos como el precio, la dirección, las coordenadas geográficas, las fotos, la cantidad de habitaciones. Todo bien estructurado.

Arranquemos.

---

Lo primero que hice fue analizar cómo funciona Zillow por dentro. Zillow está construido con Next.js, que es un framework de React muy popular. Lo interesante de Next.js es que cuando el servidor te manda una página de resultados, ya incluye todos los datos embebidos en el HTML, dentro de un tag de script especial llamado guión bajo guión bajo NEXT DATA guión bajo guión bajo. Ese tag contiene un objeto JSON gigante con todo: los IDs de cada propiedad, los precios, las coordenadas, las fotos, las descripciones. Todo.

Esto es una ventaja enorme para el scraping porque no hace falta ejecutar JavaScript del lado del cliente ni esperar que carguen requests adicionales. Los datos están ahí, en el HTML inicial.

Con eso claro, la primera decisión fue usar Playwright. Playwright es una librería que abre un browser Chromium real, navega a la URL, y nos permite leer el contenido de la página. La idea era abrir Zillow, esperar que cargue, y extraer ese JSON del tag NEXT DATA.

---

Primer problema: el captcha de PerimeterX.

Cuando corrimos el scraper por primera vez, Zillow no nos mostró los resultados. En cambio, nos sirvió una página de captcha. El error en el código era claro: no encontraba el elemento NEXT DATA en el DOM porque la página que había cargado era el challenge de PerimeterX, no la de resultados.

Para entender exactamente qué estaba pasando, agregué código de diagnóstico: guardar un screenshot de lo que veía el browser en ese momento. La imagen fue reveladora. Decía "Press and Hold to confirm you are a human". Es el captcha biométrico de PerimeterX que mide cómo el usuario interactúa físicamente con el botón.

Intentamos varias cosas para evadir esto. Instalamos una librería llamada playwright-stealth, que parchea unas veinte señales que delatan que el browser es automatizado. Cosas como el valor de navigator punto webdriver, el fingerprint del canvas, los plugins del browser. Hubo incluso un problema de compatibilidad: la nueva versión de playwright-stealth había cambiado su API y el import fallaba. Lo corregimos.

También probamos cambiar la estrategia de espera de la página. En lugar de esperar a que el DOM cargara, esperamos a que la red estuviera completamente idle, sin requests activos. Pero eso causaba timeouts de sesenta segundos porque Zillow tiene requests de telemetría y analytics que corren continuamente y nunca paran. Así que la red nunca quedaba idle.

Nada funcionó. El captcha seguía apareciendo.

---

Siguiente intento: dejar que el usuario resuelva el captcha manualmente.

La idea era esta: el scraper abre el browser visible, el usuario ve el captcha, lo resuelve a mano, presiona ENTER en la terminal, y el scraper continúa con la sesión ya autenticada.

Lo implementamos. Pero aparecía un problema nuevo. Después de que el usuario presionaba ENTER, el scraper hacía una nueva navegación a la URL con los filtros de búsqueda. Y esa nueva navegación tiraba la sesión. Las cookies de autenticación de PerimeterX no sobrevivían al nuevo goto porque la URL compleja con todos los filtros era tratada como una sesión nueva.

Lo refactorizamos para que la primera página se leyera de donde ya estaba el browser, sin navegar de nuevo. Pero incluso así, el captcha "Press and Hold" decía "please try again" cuando el usuario intentaba resolverlo. PerimeterX analizaba el contexto del evento de mouse y detectaba que venía de un entorno automatizado, aunque el click fuera real.

Fue el momento de aceptar que Playwright, en esta situación, no era suficiente.

---

Diagnóstico del problema real: la IP de la VPN.

Hicimos un script de diagnóstico muy simple. Solo un request HTTP a zillow punto com y a ver qué devolvía. El resultado: HTTP 403 desde el primer request, ni siquiera llegaba al captcha interactivo.

El body de la respuesta tenía la firma de PerimeterX bloqueando a nivel de IP. Las IPs de datacenter de Windscribe Free, la VPN gratuita que usábamos, están en las listas negras de Zillow. El bloqueo ocurría antes de que el request fuera evaluado por cualquier lógica de fingerprinting. No importaba qué tan bien espoofáramos el browser: con esa IP, nada pasaba.

---

Cambio de approach completo: curl underscore cffi sin browser.

Con el diagnóstico claro, descartamos Playwright completamente. Reescribimos el scraper usando una librería llamada curl cffi. Esta librería usa libcurl compilado con BoringSSL y permite especificar que queremos impersonar Chrome 124. Lo que hace es que el handshake TLS, la negociación de seguridad entre el cliente y el servidor de Zillow, sea byte a byte idéntico al de Chrome 124 real. Mismo JA3 hash, mismas cipher suites, mismas extensiones TLS, mismo orden de parámetros.

El resultado: sin browser, sin DOM, sin JavaScript. Solo un request HTTP directo que Zillow ve como si viniera de Chrome 124 real. El JSON del NEXT DATA se extrae del HTML recibido con una expresión regular.

Ahora el problema era la IP. curl cffi puede spooféar el fingerprint TLS pero no puede cambiar la dirección IP. Con Windscribe, seguíamos en 403.

---

La solución: el hotspot del celular.

Las IPs de las VPNs gratuitas son IPs de datacenter. Están registradas a empresas de hosting y están en todas las listas negras. Pero las IPs de operadoras móviles son IPs residenciales o de carrier. Son las mismas IPs que usan millones de personas reales para navegar. Zillow no las tiene en blacklist porque bloquearlas significaría bloquear a usuarios reales.

Conectamos la laptop al hotspot del celular, desactivamos la VPN, y corrimos el scraper.

El primer request devolvió HTTP 200. Doscientos setenta y siete mil novecientos setenta y cinco caracteres de HTML. Con el JSON de NEXT DATA adentro. Con cuarenta y un listings en la primera página.

---

El scraping funcionó.

Procesamos trece páginas de cuarenta y un listings cada una. Quinientos treinta y tres propiedades guardadas en la base de datos SQLite en aproximadamente tres minutos. Cada una con su ID de Zillow, dirección, precio, coordenadas, tipo de propiedad, cantidad de habitaciones y baños, foto, descripción y el JSON completo original.

En la página catorce, Zillow interrumpió el handshake TLS con un error de BoringSSL. Lo que hace PerimeterX cuando detecta un patrón automatizado es corromper activamente la respuesta SSL para que el cliente no pueda procesarla. Es el rate limiting activo. Trece páginas, quinientos treinta y tres listings, es el límite que se alcanza con una IP de hotspot móvil sin proxies rotativos.

Exactamente lo que el challenge pedía: avanzar hasta donde la IP lo permita y documentarlo.

---

Ahora déjenme contarles cómo funciona el código por dentro.

El scraper arranca inicializando la base de datos SQLite. Crea la tabla de listings si no existe, con todos los campos requeridos: el zpid que es el ID único de Zillow, la URL del listing, el estado del listing, el tipo de propiedad, las coordenadas, el precio, habitaciones, baños, superficie, dirección, descripción, foto, y el JSON completo. También tiene un campo status que puede ser pending o done.

Ese campo status es el mecanismo de resume. Si el proceso se interrumpe en la mitad, cuando vuelve a correr, salta todos los listings que ya tienen status igual a done. Nunca los reprocesa. Los datos ya guardados son inmutables.

Luego crea la sesión de curl cffi con impersonate igual a chrome ciento veinticuatro. Esta sesión mantiene cookies entre requests y spoofea el TLS en cada handshake.

El loop principal itera por las páginas. Para cada página construye la URL con los filtros correctos: mínimo dos habitaciones, casas y apartamentos, para la venta y alquiler. La URL usa un query param llamado searchQueryState que es un JSON URL-encodeado con todos esos filtros.

Para cada URL intenta hacer el request. Si falla, espera con backoff exponencial: cinco segundos en el primer reintento, diez en el segundo, veinte en el tercero. Si falla tres veces, detiene el scraping y lo registra en el log.

Si el request es exitoso, extrae el JSON del NEXT DATA con un regex. Navega el JSON por el path que corresponde a los resultados de búsqueda. Extrae los listings de la vista de lista y también los del mapa, deduplicando por zpid.

Por cada listing crudo, normaliza los datos. El precio puede estar en dos lugares diferentes del JSON dependiendo del tipo de listing, así que lo busca en ambos. Las coordenadas también pueden estar anidadas de formas distintas. La función de normalización maneja todas esas variaciones y construye un diccionario limpio con exactamente los campos que va a la base de datos.

Antes de guardar cada listing, verifica si el zpid ya existe con status done. Si es así, lo saltea. Si no, hace un UPSERT: inserta el registro, y si ya existe por conflicto de clave primaria, actualiza todos los campos pero solo si el status actual no es done. Así los registros completos son inviolables.

Entre páginas, espera tres segundos. No porque sea obligatorio, sino como delay de cortesía para no hacer requests demasiado rápido.

Al final, cierra la conexión a la base de datos y loguea el total de listings guardados.

---

Todo esto queda en un archivo listings punto db que se puede abrir con cualquier cliente SQLite, consultar con SQL estándar, o importar a Excel, Python, lo que sea.

Y eso es exactamente lo que hicimos: partimos de un challenge de scraping, nos encontramos con PerimeterX, probamos cuatro approaches distintos, diagnosticamos el problema real, lo resolvimos con la herramienta correcta, y terminamos con quinientos treinta y tres propiedades de Puerto Rico bien estructuradas en una base de datos.

