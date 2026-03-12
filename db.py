"""Database layer - SQLite for trade history and learning state."""
import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
from config import DB_PATH


def init_db():
    """Initialize database tables."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                direction TEXT NOT NULL,  -- 'long' or 'short'
                entry_price REAL NOT NULL,
                exit_price REAL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                position_size REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                pnl REAL,
                status TEXT NOT NULL DEFAULT 'open',  -- 'open', 'closed_tp', 'closed_sl', 'closed_manual'
                signals_json TEXT,  -- JSON of signal scores at entry
                exit_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS signal_weights (
                signal_name TEXT PRIMARY KEY,
                weight REAL NOT NULL DEFAULT 1.0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0.0,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS equity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                equity REAL NOT NULL,
                open_positions INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS news_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source TEXT,
                published TEXT,
                sentiment_score REAL,
                currencies_mentioned TEXT,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


@contextmanager
def get_conn():
    """Get a database connection with WAL mode for concurrent reads."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Trade operations ---

def insert_trade(pair, direction, entry_price, stop_loss, take_profit,
                 position_size, signals: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades (pair, direction, entry_price, stop_loss, take_profit,
               position_size, entry_time, signals_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (pair, direction, entry_price, stop_loss, take_profit,
             position_size, datetime.utcnow().isoformat(), json.dumps(signals))
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, reason: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            return
        if row["direction"] == "long":
            pnl = (exit_price - row["entry_price"]) * row["position_size"]
        else:
            pnl = (row["entry_price"] - exit_price) * row["position_size"]
        conn.execute(
            """UPDATE trades SET exit_price = ?, exit_time = ?, pnl = ?,
               status = ?, exit_reason = ? WHERE id = ?""",
            (exit_price, datetime.utcnow().isoformat(), pnl, reason,
             reason, trade_id)
        )
        return pnl


def get_open_trades() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_trade_history(limit=100) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status != 'open' ORDER BY exit_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_trades(limit=200) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_open_positions() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status = 'open'"
        ).fetchone()
        return row["cnt"]


def get_open_trades_for_pair(pair: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE pair = ? AND status = 'open'", (pair,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_stop_loss(trade_id: int, new_stop_loss: float):
    """Update stop-loss for trailing stop functionality."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET stop_loss = ? WHERE id = ? AND status = 'open'",
            (new_stop_loss, trade_id)
        )


# --- Signal weights ---

def get_signal_weights() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM signal_weights").fetchall()
        return {r["signal_name"]: dict(r) for r in rows}


def upsert_signal_weight(signal_name: str, weight: float, wins: int,
                         losses: int, total_pnl: float):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO signal_weights (signal_name, weight, wins, losses, total_pnl, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(signal_name) DO UPDATE SET
               weight = ?, wins = ?, losses = ?, total_pnl = ?, updated_at = ?""",
            (signal_name, weight, wins, losses, total_pnl, datetime.utcnow().isoformat(),
             weight, wins, losses, total_pnl, datetime.utcnow().isoformat())
        )


# --- Equity history ---

def record_equity(equity: float, open_positions: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO equity_history (timestamp, equity, open_positions) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(), equity, open_positions)
        )


def get_equity_history(limit=500) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM equity_history ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# --- News cache ---

def cache_news(title, source, published, sentiment, currencies):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO news_cache (title, source, published, sentiment_score,
               currencies_mentioned, fetched_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (title, source, published, sentiment, json.dumps(currencies),
             datetime.utcnow().isoformat())
        )


def get_recent_news(hours=4, limit=50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM news_cache
               WHERE fetched_at > datetime('now', ?) ORDER BY fetched_at DESC LIMIT ?""",
            (f"-{hours} hours", limit)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Bot state ---

def get_state(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def set_state(key: str, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (key, json.dumps(value))
        )


# --- Stats ---

def get_stats() -> dict:
    with get_conn() as conn:
        closed = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(pnl) as total_pnl FROM trades WHERE status != 'open'"
        ).fetchone()
        wins = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status != 'open' AND pnl > 0"
        ).fetchone()
        losses = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status != 'open' AND pnl <= 0"
        ).fetchone()
        open_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status = 'open'"
        ).fetchone()

        total = closed["cnt"] or 0
        win_count = wins["cnt"] or 0
        loss_count = losses["cnt"] or 0

        return {
            "total_trades": total,
            "open_trades": open_count["cnt"] or 0,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": (win_count / total * 100) if total > 0 else 0,
            "total_pnl": closed["total_pnl"] or 0.0,
        }
