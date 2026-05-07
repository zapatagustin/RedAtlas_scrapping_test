"""
Zillow Scraper — Dashboard web
Lanzar: poetry run python dashboard.py
Acceder: http://localhost:5000
"""
import os
import sqlite3
import subprocess
import sys
import threading

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

DB_PATH = "listings.db"

_scraper_process = None
_lock = threading.Lock()


def get_stats() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT status, COUNT(*) FROM listings GROUP BY status").fetchall()
        status_counts = {r[0]: r[1] for r in rows}
        total = sum(status_counts.values())

        fields = ["price", "address", "latitude", "longitude"]
        null_pcts = {}
        for field in fields:
            count = conn.execute(
                f"SELECT COUNT(*) FROM listings WHERE {field} IS NULL"
            ).fetchone()[0]
            null_pcts[field] = round(count / total * 100, 1) if total else 0.0

        failed = conn.execute(
            "SELECT zpid, address, price, scraped_at FROM listings "
            "WHERE status = 'failed' ORDER BY scraped_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
    except Exception as e:
        return {"error": str(e)}

    return {
        "total": total,
        "done": status_counts.get("done", 0),
        "failed_count": status_counts.get("failed", 0),
        "pending": status_counts.get("pending", 0),
        "null_pcts": null_pcts,
        "failed_listings": [
            {"zpid": r[0], "address": r[1] or "—", "price": r[2], "scraped_at": r[3]}
            for r in failed
        ],
    }


def scraper_running() -> bool:
    global _scraper_process
    with _lock:
        return _scraper_process is not None and _scraper_process.poll() is None


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    stats["scraper_running"] = scraper_running()
    return jsonify(stats)


@app.route("/scraper/start", methods=["POST"])
def scraper_start():
    global _scraper_process
    with _lock:
        if _scraper_process is not None and _scraper_process.poll() is None:
            return jsonify({"ok": False, "message": "Scraper ya está corriendo"})
        _scraper_process = subprocess.Popen(
            ["poetry", "run", "python", "scraper.py"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            cwd=os.getcwd(),
        )
    return jsonify({"ok": True, "message": f"Scraper iniciado (PID {_scraper_process.pid})"})


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Zillow Scraper — Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #f0f2f5; color: #1a1a2e; }
  header {
    background: #1a1a2e; color: #fff; padding: 1rem 2rem;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 1.2rem; font-weight: 600; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot-idle    { background: #6b7280; }
  .dot-running { background: #22c55e; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  main { max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
  .cards {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 1rem; margin-bottom: 2rem;
  }
  .card { background: #fff; border-radius: 8px; padding: 1.2rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .card .label { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; margin-bottom: .4rem; }
  .card .value { font-size: 2rem; font-weight: 700; }
  .card.done    .value { color: #16a34a; }
  .card.failed  .value { color: #dc2626; }
  .card.pending .value { color: #d97706; }
  .section {
    background: #fff; border-radius: 8px; padding: 1.5rem;
    box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 1.5rem;
  }
  .section h2 {
    font-size: .85rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: .05em; color: #6b7280; margin-bottom: 1rem;
  }
  table { width: 100%; border-collapse: collapse; font-size: .875rem; }
  th {
    text-align: left; padding: .5rem .75rem; border-bottom: 2px solid #e5e7eb;
    color: #6b7280; font-weight: 600; font-size: .75rem; text-transform: uppercase;
  }
  td { padding: .5rem .75rem; border-bottom: 1px solid #f3f4f6; }
  .badge { display: inline-block; padding: .2rem .5rem; border-radius: 4px; font-size: .75rem; font-weight: 600; }
  .ok   { background: #dcfce7; color: #16a34a; }
  .warn { background: #fef9c3; color: #854d0e; }
  .crit { background: #fee2e2; color: #dc2626; }
  .btn { padding: .6rem 1.4rem; border: none; border-radius: 6px; cursor: pointer; font-size: .875rem; font-weight: 600; }
  .btn-start          { background: #22c55e; color: #fff; }
  .btn-start:disabled { background: #6b7280; cursor: not-allowed; }
  .controls { display: flex; align-items: center; gap: 1rem; }
  #msg { font-size: .8rem; color: #9ca3af; }
  #last-updated { font-size: .75rem; color: #9ca3af; margin-top: .5rem; text-align: right; }
</style>
</head>
<body>
<header>
  <h1>Zillow Scraper — Dashboard</h1>
  <div class="controls">
    <span id="scraper-status"><span class="status-dot dot-idle"></span>Inactivo</span>
    <span id="msg"></span>
    <button class="btn btn-start" id="btn-start" onclick="startScraper()">&#9654; Lanzar Scraper</button>
  </div>
</header>
<main>
  <div class="cards">
    <div class="card">         <div class="label">Total</div>   <div class="value" id="c-total">—</div></div>
    <div class="card done">    <div class="label">Done</div>    <div class="value" id="c-done">—</div></div>
    <div class="card failed">  <div class="label">Failed</div>  <div class="value" id="c-failed">—</div></div>
    <div class="card pending"> <div class="label">Pending</div> <div class="value" id="c-pending">—</div></div>
  </div>

  <div class="section">
    <h2>Calidad de datos — campos requeridos</h2>
    <table>
      <thead><tr><th>Campo</th><th>Null %</th><th>Estado</th></tr></thead>
      <tbody id="null-table"></tbody>
    </table>
  </div>

  <div class="section">
    <h2>Listings fallidos (últimos 20)</h2>
    <table>
      <thead><tr><th>ZPID</th><th>Dirección</th><th>Precio</th><th>Fecha</th></tr></thead>
      <tbody id="failed-table"></tbody>
    </table>
  </div>

  <div id="last-updated"></div>
</main>

<script>
async function fetchStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();

    document.getElementById('c-total').textContent   = d.total        ?? '—';
    document.getElementById('c-done').textContent    = d.done         ?? '—';
    document.getElementById('c-failed').textContent  = d.failed_count ?? '—';
    document.getElementById('c-pending').textContent = d.pending      ?? '—';

    const nullTb = document.getElementById('null-table');
    nullTb.innerHTML = '';
    for (const [field, pct] of Object.entries(d.null_pcts || {})) {
      const cls   = pct > 15 ? 'crit' : pct > 5 ? 'warn' : 'ok';
      const label = pct > 15 ? 'CRÍTICO' : pct > 5 ? 'WARNING' : 'OK';
      nullTb.innerHTML += `<tr><td>${field}</td><td>${pct}%</td><td><span class="badge ${cls}">${label}</span></td></tr>`;
    }

    const failedTb = document.getElementById('failed-table');
    if (!(d.failed_listings || []).length) {
      failedTb.innerHTML = '<tr><td colspan="4" style="color:#6b7280;text-align:center;padding:1rem">Sin listings fallidos</td></tr>';
    } else {
      failedTb.innerHTML = d.failed_listings.map(l =>
        `<tr>
          <td>${l.zpid}</td>
          <td>${l.address}</td>
          <td>${l.price ? '$' + l.price.toLocaleString() : '—'}</td>
          <td>${l.scraped_at ? l.scraped_at.slice(0, 19) : '—'}</td>
        </tr>`
      ).join('');
    }

    const running = d.scraper_running;
    document.getElementById('scraper-status').innerHTML = running
      ? '<span class="status-dot dot-running"></span>Corriendo...'
      : '<span class="status-dot dot-idle"></span>Inactivo';
    document.getElementById('btn-start').disabled = running;
    if (!running) document.getElementById('msg').textContent = '';

    document.getElementById('last-updated').textContent =
      'Actualizado: ' + new Date().toLocaleTimeString();
  } catch (e) {
    console.error(e);
  }
}

async function startScraper() {
  document.getElementById('btn-start').disabled = true;
  document.getElementById('msg').textContent = 'Iniciando...';
  const r = await fetch('/scraper/start', { method: 'POST' });
  const d = await r.json();
  document.getElementById('msg').textContent = d.message;
  fetchStats();
}

fetchStats();
setInterval(fetchStats, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("Dashboard disponible en http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
