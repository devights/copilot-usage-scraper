"""SQLite database layer for Copilot usage snapshots."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "usage.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT    NOT NULL,
    metric_name TEXT    NOT NULL,
    used        INTEGER,
    quota       INTEGER,
    raw_text    TEXT
);

CREATE INDEX IF NOT EXISTS idx_captured_at ON usage_snapshots (captured_at);
CREATE INDEX IF NOT EXISTS idx_metric_name ON usage_snapshots (metric_name);
"""


@contextmanager
def get_conn(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def save_snapshot(metrics: list[dict], db_path: Path = DB_PATH) -> None:
    """Persist a list of metric dicts, each with keys: metric_name, used, quota, raw_text."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO usage_snapshots (captured_at, metric_name, used, quota, raw_text)
            VALUES (:captured_at, :metric_name, :used, :quota, :raw_text)
            """,
            [{**m, "captured_at": now} for m in metrics],
        )


def query_history(metric_name: str | None = None, limit: int = 50, db_path: Path = DB_PATH):
    """Return recent snapshots, optionally filtered by metric_name."""
    with get_conn(db_path) as conn:
        if metric_name:
            rows = conn.execute(
                """
                SELECT captured_at, metric_name, used, quota
                FROM usage_snapshots
                WHERE metric_name = ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (metric_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT captured_at, metric_name, used, quota
                FROM usage_snapshots
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
