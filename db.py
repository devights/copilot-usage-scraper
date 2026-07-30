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


def get_last_snapshot(metric_name: str, db_path: Path = DB_PATH) -> dict | None:
    """Return the most recently captured snapshot for a metric, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT captured_at, metric_name, used, quota, raw_text
            FROM usage_snapshots
            WHERE metric_name = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (metric_name,),
        ).fetchone()
    return dict(row) if row else None


def save_snapshot(metrics: list[dict], db_path: Path = DB_PATH) -> int:
    """
    Persist a list of metric dicts (keys: metric_name, used, quota, raw_text).

    Skips inserting a row when both ``used`` and ``quota`` are identical to the
    most recently stored snapshot for that metric (deduplication).

    Returns the number of rows actually inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    to_insert = []
    for m in metrics:
        last = get_last_snapshot(m["metric_name"], db_path)
        if last is not None and last.get("used") == m.get("used") and last.get("quota") == m.get("quota"):
            continue
        to_insert.append({**m, "captured_at": now})

    if not to_insert:
        return 0

    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO usage_snapshots (captured_at, metric_name, used, quota, raw_text)
            VALUES (:captured_at, :metric_name, :used, :quota, :raw_text)
            """,
            to_insert,
        )
    return len(to_insert)


def query_history(
    metric_name: str | None = None,
    limit: int = 50,
    from_ts: str | None = None,
    to_ts: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """
    Return snapshots ordered by captured_at DESC, optionally filtered by
    metric_name and/or an inclusive timestamp range (ISO-8601 strings).
    """
    conditions: list[str] = []
    params: list = []

    if metric_name:
        conditions.append("metric_name = ?")
        params.append(metric_name)
    if from_ts:
        conditions.append("captured_at >= ?")
        params.append(from_ts)
    if to_ts:
        conditions.append("captured_at <= ?")
        params.append(to_ts)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT captured_at, metric_name, used, quota, raw_text
            FROM usage_snapshots
            {where}
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]
