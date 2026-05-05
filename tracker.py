"""
SQLite paper trading tracker.
DB: predictions.db
"""
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "predictions.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                market    TEXT    NOT NULL,
                yes_price REAL    NOT NULL,
                true_prob REAL    NOT NULL,
                verdict   TEXT    NOT NULL,
                kelly_pct REAL    NOT NULL,
                resolved  INTEGER NOT NULL DEFAULT 0,
                outcome   INTEGER,
                pnl       REAL
            )
        """)
        conn.commit()
    logger.info("[DB] predictions.db initialised")


def save_prediction(market: str, yes_price: float, true_prob: float,
                    verdict: str, kelly_pct: float) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO predictions (timestamp,market,yes_price,true_prob,verdict,kelly_pct) "
            "VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), market, yes_price, true_prob, verdict, kelly_pct),
        )
        conn.commit()
        return cur.lastrowid


def resolve_prediction(pred_id: int, outcome: int) -> dict:
    """
    outcome: 1 = YES won, 0 = NO won.
    Calculates PnL based on the paper position direction (verdict).
    Returns dict with pnl and status message.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT yes_price, true_prob, kelly_pct, verdict FROM predictions WHERE id=?",
            (pred_id,),
        ).fetchone()
        if not row:
            return {"error": f"Prediction #{pred_id} not found"}

        yes_price = row["yes_price"]
        kelly_pct = row["kelly_pct"]
        verdict = row["verdict"]

        # Determine PnL based on paper trade direction
        if verdict == "SOUS-ESTIMÉ":
            # We paper-traded YES
            if outcome == 1:
                pnl = kelly_pct * ((1.0 / yes_price) - 1.0)
            else:
                pnl = -kelly_pct
        elif verdict == "SURESTIMÉ":
            # We paper-traded NO
            no_price = 1.0 - yes_price
            if outcome == 0 and no_price > 0:
                pnl = kelly_pct * ((1.0 / no_price) - 1.0)
            else:
                pnl = -kelly_pct
        else:
            # ALIGNÉ — SKIP, no trade
            pnl = 0.0

        conn.execute(
            "UPDATE predictions SET resolved=1, outcome=?, pnl=? WHERE id=?",
            (outcome, pnl, pred_id),
        )
        conn.commit()

    return {"id": pred_id, "outcome": outcome, "pnl": pnl}


def get_stats() -> dict:
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE resolved=1"
        ).fetchone()[0]
        wins = conn.execute(
            """SELECT COUNT(*) FROM predictions WHERE resolved=1 AND (
                (verdict='SOUS-ESTIMÉ' AND outcome=1) OR
                (verdict='SURESTIMÉ'  AND outcome=0)
            )"""
        ).fetchone()[0]
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM predictions WHERE resolved=1"
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT id, timestamp, market, yes_price, true_prob, verdict, "
            "kelly_pct, resolved, pnl FROM predictions ORDER BY id DESC LIMIT 10"
        ).fetchall()

    win_rate = (wins / resolved * 100) if resolved > 0 else 0.0
    return {
        "total": total,
        "resolved": resolved,
        "wins": wins,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "recent": [dict(r) for r in recent],
    }
