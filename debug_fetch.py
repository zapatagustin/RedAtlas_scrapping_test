"""Script de diagnóstico — corre esto para ver qué devuelve Zillow exactamente."""
from curl_cffi import requests as curl_requests

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}

session = curl_requests.Session(impersonate="chrome124")

# 1. Primero visitar la home para conseguir cookies
print("=== Visitando home... ===")
r1 = session.get("https://www.zillow.com/", headers=HEADERS, timeout=30)
print(f"Home status: {r1.status_code}")
print(f"Cookies recibidas: {dict(r1.cookies)}")

import time
time.sleep(2)

# 2. Ahora intentar la búsqueda con las cookies de sesión
print("\n=== Visitando búsqueda PR... ===")
r2 = session.get("https://www.zillow.com/pr/", headers=HEADERS, timeout=30)
print(f"PR status: {r2.status_code}")
print(f"Primeros 500 chars del body:\n{r2.text[:500]}")

# Guardar HTML completo para inspección
with open("debug_403.html", "w") as f:
    f.write(r2.text)
print("\nHTML guardado en debug_403.html")
