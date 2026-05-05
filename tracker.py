"""
SQLite paper trading tracker — AUM NEXUS POLY.
DB: predictions.db

Extended with:
  - Brier Score auto-calibration per bucket
  - Calibration bias factors (auto-applied on next prediction)
  - Portfolio risk management (exposure cap + drawdown halt)
"""
import sqlite3
import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "predictions.db"

# ─── Risk limits ──────────────────────────────────────────────────────────────
MAX_TOTAL_EXPOSURE = 0.25   # 25% bankroll max across all active positions
MAX_PER_CATEGORY   = 0.10   # 10% per sport/topic category
DRAWDOWN_HALT      = 0.15   # halt at -15% from peak


# ─── Connection ───────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Schema ───────────────────────────────────────────────────────────────────

def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                market      TEXT    NOT NULL,
                category    TEXT    NOT NULL DEFAULT 'general',
                yes_price   REAL    NOT NULL,
                true_prob   REAL    NOT NULL,
                verdict     TEXT    NOT NULL,
                kelly_pct   REAL    NOT NULL,
                resolved    INTEGER NOT NULL DEFAULT 0,
                outcome     INTEGER,
                pnl         REAL,
                brier_score REAL
            )
        """)
        # Add category column if upgrading from older schema
        try:
            conn.execute("ALTER TABLE predictions ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE predictions ADD COLUMN brier_score REAL")
        except Exception:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS calibration (
                bucket      TEXT PRIMARY KEY,
                n_total     INTEGER NOT NULL DEFAULT 0,
                n_yes       INTEGER NOT NULL DEFAULT 0,
                bias_factor REAL    NOT NULL DEFAULT 1.0,
                brier_sum   REAL    NOT NULL DEFAULT 0.0,
                updated_at  TEXT
            )
        """)
        # Seed calibration buckets
        for bucket in ("0-20", "20-40", "40-60", "60-80", "80-100"):
            conn.execute(
                "INSERT OR IGNORE INTO calibration (bucket, n_total, n_yes, bias_factor, brier_sum, updated_at) "
                "VALUES (?,0,0,1.0,0.0,?)",
                (bucket, datetime.utcnow().isoformat()),
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                bankroll    REAL NOT NULL DEFAULT 1.0,
                peak        REAL NOT NULL DEFAULT 1.0,
                drawdown    REAL NOT NULL DEFAULT 0.0
            )
        """)
        conn.commit()
    logger.info("[DB] predictions.db initialised")


# ─── Calibration helpers ───────────────────────────────────────────────────────

def _bucket_for(prob: float) -> str:
    if prob < 0.20:  return "0-20"
    if prob < 0.40:  return "20-40"
    if prob < 0.60:  return "40-60"
    if prob < 0.80:  return "60-80"
    return "80-100"


def get_bias_factor(true_prob: float) -> float:
    """
    Return the calibration bias factor for the probability bucket.
    E.g. if LLM overestimates in 60-80% bucket, factor < 1.
    """
    bucket = _bucket_for(true_prob)
    with _conn() as conn:
        row = conn.execute(
            "SELECT bias_factor FROM calibration WHERE bucket=?", (bucket,)
        ).fetchone()
    return float(row["bias_factor"]) if row else 1.0


def apply_calibration(true_prob: float) -> float:
    """Adjust true_prob using the learned bias factor for its bucket."""
    factor = get_bias_factor(true_prob)
    calibrated = max(0.01, min(0.99, true_prob * factor))
    if abs(factor - 1.0) > 0.02:
        logger.info("[CALIB] bucket=%s factor=%.3f: %.0f%% → %.0f%%",
                    _bucket_for(true_prob), factor, true_prob * 100, calibrated * 100)
    return calibrated


def _update_calibration(true_prob: float, outcome: int):
    """Update bucket stats and recompute bias factor after a resolution."""
    bucket = _bucket_for(true_prob)
    brier = (true_prob - outcome) ** 2

    with _conn() as conn:
        conn.execute(
            "UPDATE calibration SET "
            "n_total = n_total + 1, "
            "n_yes   = n_yes + ?, "
            "brier_sum = brier_sum + ?, "
            "updated_at = ? "
            "WHERE bucket = ?",
            (outcome, brier, datetime.utcnow().isoformat(), bucket),
        )
        row = conn.execute(
            "SELECT n_total, n_yes FROM calibration WHERE bucket=?", (bucket,)
        ).fetchone()

        if row and row["n_total"] >= 5:
            actual_rate = row["n_yes"] / row["n_total"]
            # Midpoint of bucket as predicted rate
            bucket_mid = {"0-20": 0.10, "20-40": 0.30, "40-60": 0.50,
                          "60-80": 0.70, "80-100": 0.90}.get(bucket, 0.50)
            # bias_factor shrinks when LLM overestimates (actual < predicted)
            bias_factor = round(actual_rate / bucket_mid, 4) if bucket_mid > 0 else 1.0
            bias_factor = max(0.50, min(2.00, bias_factor))
            conn.execute(
                "UPDATE calibration SET bias_factor=? WHERE bucket=?",
                (bias_factor, bucket),
            )
            logger.info("[CALIB] bucket=%s updated: actual=%.0f%% predicted=%.0f%% factor=%.3f",
                        bucket, actual_rate * 100, bucket_mid * 100, bias_factor)
        conn.commit()


# ─── Portfolio risk ────────────────────────────────────────────────────────────

def get_portfolio_status() -> dict:
    """
    Returns current exposure, drawdown, and risk flags.
    """
    with _conn() as conn:
        # Active positions (unresolved, non-ALIGNÉ)
        active = conn.execute(
            "SELECT category, kelly_pct FROM predictions WHERE resolved=0 AND verdict != 'ALIGNÉ'"
        ).fetchall()

        total_exposure = sum(r["kelly_pct"] for r in active)

        # Per-category exposure
        category_exposure: dict[str, float] = {}
        for r in active:
            cat = r["category"]
            category_exposure[cat] = category_exposure.get(cat, 0) + r["kelly_pct"]

        # Cumulative PnL to compute bankroll + drawdown
        resolved = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM predictions WHERE resolved=1"
        ).fetchone()
        total_pnl = float(resolved["total_pnl"] or 0)
        bankroll = 1.0 + total_pnl

        # Peak bankroll
        peak_row = conn.execute(
            "SELECT MAX(bankroll) as peak FROM portfolio_snapshots"
        ).fetchone()
        peak = max(float(peak_row["peak"] or 1.0), bankroll)
        drawdown = (peak - bankroll) / peak if peak > 0 else 0.0

        # Snapshot
        conn.execute(
            "INSERT INTO portfolio_snapshots (timestamp, bankroll, peak, drawdown) VALUES (?,?,?,?)",
            (datetime.utcnow().isoformat(), bankroll, peak, round(drawdown, 4)),
        )
        conn.commit()

    # Risk flags
    flags = []
    if total_exposure >= MAX_TOTAL_EXPOSURE:
        flags.append(f"⚠️ EXPOSURE MAX: {total_exposure:.0%} ≥ {MAX_TOTAL_EXPOSURE:.0%} — nouveaux trades bloqués")
    for cat, exp in category_exposure.items():
        if exp > MAX_PER_CATEGORY:
            flags.append(f"⚠️ {cat.upper()}: {exp:.0%} > {MAX_PER_CATEGORY:.0%} max")
    if drawdown >= DRAWDOWN_HALT:
        flags.append(f"🛑 DRAWDOWN HALT: -{drawdown:.0%} depuis le pic — trading suspendu")

    return {
        "total_exposure": round(total_exposure, 4),
        "category_exposure": {k: round(v, 4) for k, v in category_exposure.items()},
        "bankroll": round(bankroll, 4),
        "peak": round(peak, 4),
        "drawdown": round(drawdown, 4),
        "active_positions": len(active),
        "flags": flags,
        "trading_halted": (
            total_exposure >= MAX_TOTAL_EXPOSURE or drawdown >= DRAWDOWN_HALT
        ),
    }


def check_risk_before_trade(kelly_pct: float, category: str = "general") -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    Call before saving a prediction — block if limits breached.
    """
    status = get_portfolio_status()
    if status["trading_halted"]:
        return False, "\n".join(status["flags"])
    new_exposure = status["total_exposure"] + kelly_pct
    if new_exposure > MAX_TOTAL_EXPOSURE:
        return False, (
            f"⚠️ PORTFOLIO: ajout de {kelly_pct:.0%} dépasserait la limite "
            f"({new_exposure:.0%} > {MAX_TOTAL_EXPOSURE:.0%})"
        )
    cat_exp = status["category_exposure"].get(category, 0) + kelly_pct
    if cat_exp > MAX_PER_CATEGORY:
        return False, (
            f"⚠️ CATÉGORIE {category.upper()}: {cat_exp:.0%} > {MAX_PER_CATEGORY:.0%} max"
        )
    return True, ""


def get_calibration_report() -> dict:
    """Return Brier scores and bias factors per bucket."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT bucket, n_total, n_yes, bias_factor, brier_sum FROM calibration ORDER BY bucket"
        ).fetchall()
        # Global Brier score from resolved predictions
        brier_row = conn.execute(
            "SELECT COALESCE(AVG(brier_score), 0) as avg_brier, COUNT(*) as n "
            "FROM predictions WHERE resolved=1 AND brier_score IS NOT NULL"
        ).fetchone()

    buckets = []
    for r in rows:
        n = r["n_total"]
        actual_pct = (r["n_yes"] / n * 100) if n > 0 else None
        bucket_mid = {"0-20": 10, "20-40": 30, "40-60": 50,
                      "60-80": 70, "80-100": 90}.get(r["bucket"], 50)
        avg_brier_bucket = r["brier_sum"] / n if n > 0 else None
        buckets.append({
            "bucket": r["bucket"],
            "n": n,
            "predicted_pct": bucket_mid,
            "actual_pct": round(actual_pct, 1) if actual_pct is not None else None,
            "bias_factor": r["bias_factor"],
            "avg_brier": round(avg_brier_bucket, 4) if avg_brier_bucket is not None else None,
        })

    return {
        "buckets": buckets,
        "global_brier": round(float(brier_row["avg_brier"]), 4),
        "n_resolved": brier_row["n"],
    }


# ─── CRUD ─────────────────────────────────────────────────────────────────────

def save_prediction(market: str, yes_price: float, true_prob: float,
                    verdict: str, kelly_pct: float,
                    category: str = "general") -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO predictions (timestamp,market,category,yes_price,true_prob,verdict,kelly_pct) "
            "VALUES (?,?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), market, category,
             yes_price, true_prob, verdict, kelly_pct),
        )
        conn.commit()
        return cur.lastrowid


def resolve_prediction(pred_id: int, outcome: int) -> dict:
    """outcome: 1=YES won, 0=NO won."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT yes_price, true_prob, kelly_pct, verdict FROM predictions WHERE id=?",
            (pred_id,),
        ).fetchone()
        if not row:
            return {"error": f"Prediction #{pred_id} not found"}

        yes_price = row["yes_price"]
        kelly_pct = row["kelly_pct"]
        verdict   = row["verdict"]
        true_prob = row["true_prob"]

        if verdict == "SOUS-ESTIMÉ":
            pnl = kelly_pct * ((1.0 / yes_price) - 1.0) if outcome == 1 else -kelly_pct
        elif verdict == "SURESTIMÉ":
            no_price = 1.0 - yes_price
            pnl = kelly_pct * ((1.0 / no_price) - 1.0) if (outcome == 0 and no_price > 0) else -kelly_pct
        else:
            pnl = 0.0

        brier = (true_prob - outcome) ** 2

        conn.execute(
            "UPDATE predictions SET resolved=1, outcome=?, pnl=?, brier_score=? WHERE id=?",
            (outcome, pnl, brier, pred_id),
        )
        conn.commit()

    # Update calibration buckets
    _update_calibration(true_prob, outcome)

    return {"id": pred_id, "outcome": outcome, "pnl": pnl, "brier": brier}


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
            "kelly_pct, resolved, pnl, brier_score FROM predictions ORDER BY id DESC LIMIT 10"
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
