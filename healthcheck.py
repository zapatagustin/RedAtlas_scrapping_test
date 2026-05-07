"""
Health check para listings.db.
Uso: poetry run python healthcheck.py
"""

import sqlite3
import sys

DB_PATH         = "listings.db"
NULL_WARN_PCT   = 0.05   # warning si >5% de campos requeridos son null
NULL_CRIT_PCT   = 0.15   # crítico si >15%
REQUIRED_FIELDS = ["price", "address", "latitude", "longitude"]


def run():
    try:
        conn = sqlite3.connect(DB_PATH)
    except Exception as e:
        print(f"[ERROR] No se pudo abrir {DB_PATH}: {e}")
        sys.exit(1)

    total    = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    done     = conn.execute("SELECT COUNT(*) FROM listings WHERE status = 'done'").fetchone()[0]
    failed   = conn.execute("SELECT COUNT(*) FROM listings WHERE status = 'failed'").fetchone()[0]
    pending  = conn.execute("SELECT COUNT(*) FROM listings WHERE status = 'pending'").fetchone()[0]

    print(f"\n{'─'*45}")
    print(f"  HEALTHCHECK — {DB_PATH}")
    print(f"{'─'*45}")
    print(f"  Total    : {total}")
    print(f"  Done     : {done}")
    print(f"  Failed   : {failed}  ({failed/total*100:.1f}%)" if total else "  Failed   : 0")
    print(f"  Pending  : {pending}")
    print(f"{'─'*45}")

    has_issue = False

    for field in REQUIRED_FIELDS:
        null_count = conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE {field} IS NULL OR CAST({field} AS TEXT) = ''"
        ).fetchone()[0]
        pct = null_count / total if total else 0

        if pct >= NULL_CRIT_PCT:
            tag = "CRÍTICO"
            has_issue = True
        elif pct >= NULL_WARN_PCT:
            tag = "WARNING"
            has_issue = True
        else:
            tag = "OK"

        print(f"  {field:<12}: {null_count:>4} nulls ({pct*100:.1f}%)  [{tag}]")

    print(f"{'─'*45}\n")

    conn.close()
    sys.exit(1 if has_issue else 0)


if __name__ == "__main__":
    run()
