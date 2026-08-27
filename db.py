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

CREATE TABLE IF NOT EXISTS scrape_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT    NOT NULL,
    error_type  TEXT    NOT NULL,
    message     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_errors_occurred_at ON scrape_errors (occurred_at);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
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
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def set_metadata(key: str, value: str, db_path: Path = DB_PATH) -> None:
    """Upsert a metadata key/value pair."""
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_metadata(key: str, db_path: Path = DB_PATH) -> str | None:
    """Return the value for a metadata key, or None if absent."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


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

    Always records ``last_scrape_at`` in the metadata table so the staleness
    check in the API reflects when the scraper last ran, not just when data
    last changed.

    Returns the number of rows actually inserted.
    """
    now = datetime.now(timezone.utc).isoformat()
    set_metadata("last_scrape_at", now, db_path)

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


def log_scrape_error(
    message: str,
    error_type: str = "error",
    db_path: Path = DB_PATH,
) -> None:
    """Record a scrape failure to the scrape_errors table."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scrape_errors (occurred_at, error_type, message)
            VALUES (?, ?, ?)
            """,
            (now, error_type, message),
        )


def query_scrape_errors(
    limit: int = 50,
    from_ts: str | None = None,
    to_ts: str | None = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Return recent scrape errors ordered by occurred_at DESC."""
    conditions: list[str] = []
    params: list = []

    if from_ts:
        conditions.append("occurred_at >= ?")
        params.append(from_ts)
    if to_ts:
        conditions.append("occurred_at <= ?")
        params.append(to_ts)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT occurred_at, error_type, message
            FROM scrape_errors
            {where}
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


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
