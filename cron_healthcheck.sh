#!/bin/bash
# Cron health check para Zillow scraper.
# Cron recomendado (cada hora):
#   0 * * * * /path/to/project/cron_healthcheck.sh >> /var/log/scraper_health.log 2>&1

set -euo pipefail

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$PROJECT_DIR/healthcheck.log"
ALERT_FILE="$PROJECT_DIR/healthcheck_alerts.log"
MAX_LOG_LINES=1000   # rotar log si supera esta cantidad de líneas
PYTHON_BIN=""        # dejar vacío para autodetectar

# ─── DETECTAR PYTHON ──────────────────────────────────────────────────────────
if [ -z "$PYTHON_BIN" ]; then
    if command -v poetry &>/dev/null && [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        PYTHON_BIN="poetry run python"
    elif [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
    elif command -v python3 &>/dev/null; then
        PYTHON_BIN="python3"
    else
        echo "[ERROR] No se encontró Python. Configurá PYTHON_BIN en el script."
        exit 1
    fi
fi

# ─── ROTAR LOG ────────────────────────────────────────────────────────────────
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt "$MAX_LOG_LINES" ]; then
    tail -n $((MAX_LOG_LINES / 2)) "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

# ─── RUN ──────────────────────────────────────────────────────────────────────
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$TIMESTAMP] Iniciando health check..." >> "$LOG_FILE"

cd "$PROJECT_DIR"
OUTPUT=$($PYTHON_BIN healthcheck.py 2>&1)
EXIT_CODE=$?

echo "$OUTPUT" >> "$LOG_FILE"

# ─── ALERTA ───────────────────────────────────────────────────────────────────
if [ $EXIT_CODE -ne 0 ]; then
    echo "[$TIMESTAMP] ALERTA — health check falló (exit $EXIT_CODE)" >> "$ALERT_FILE"
    echo "$OUTPUT" >> "$ALERT_FILE"
    echo "---" >> "$ALERT_FILE"
    echo "[$TIMESTAMP] ALERTA registrada en $ALERT_FILE" >> "$LOG_FILE"
fi

echo "[$TIMESTAMP] Health check finalizado (exit $EXIT_CODE)" >> "$LOG_FILE"
exit $EXIT_CODE
