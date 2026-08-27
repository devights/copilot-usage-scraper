"""Simple Flask dashboard for Copilot AI credit usage."""

import csv
import io
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template, request

import db
import scraper

app = Flask(__name__)

# Ensure all tables exist (safe to call repeatedly; uses CREATE IF NOT EXISTS).
db.init_db()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_API_DATA_CACHE_SECONDS = _env_int("API_DATA_CACHE_SECONDS", 10)
_AUTH_STATUS_CACHE_SECONDS = _env_int("AUTH_STATUS_CACHE_SECONDS", 300)
_AUTH_CHECK_TIMEOUT_MS = _env_int("AUTH_CHECK_TIMEOUT_MS", 2500)
_API_DATA_CACHE_LOCK = threading.Lock()
_API_DATA_CACHE: dict = {
    "cached_at_epoch": None,
    "payload": None,
}


def _cycle_start() -> datetime:
    """First of the current month in UTC."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _cycle_end() -> datetime:
    """First of next month in UTC."""
    start = _cycle_start()
    # Advance to next month
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _weekday_seconds_between(start: datetime, end: datetime) -> float:
    """Elapsed seconds between two datetimes counting only Monday-Friday time."""
    if end <= start:
        return 0.0

    total = 0.0
    cursor = start
    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        segment_end = min(next_midnight, end)
        if cursor.weekday() < 5:
            total += (segment_end - cursor).total_seconds()
        cursor = segment_end
    return total


def _add_weekday_days(start: datetime, days: float) -> datetime:
    """Add weekday-only days to a datetime, skipping Saturday/Sunday time."""
    if days <= 0:
        return start

    remaining_seconds = days * 86400
    cursor = start

    while remaining_seconds > 0:
        # Jump forward to Monday 00:00 if currently in the weekend.
        if cursor.weekday() >= 5:
            days_to_monday = 7 - cursor.weekday()
            cursor = (cursor + timedelta(days=days_to_monday)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            continue

        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        available_seconds = (next_midnight - cursor).total_seconds()
        consume = min(remaining_seconds, available_seconds)
        cursor += timedelta(seconds=consume)
        remaining_seconds -= consume

    return cursor


def _burndown_stats(rows: list[dict]) -> dict:
    """
    Compute burndown stats anchored to the current billing cycle
    (resets on the 1st of each month).
    """
    now = datetime.now(timezone.utc)
    cycle_start = _cycle_start()
    cycle_end = _cycle_end()

    # Only consider data from this cycle
    cycle_rows = [
        r for r in rows
        if (_ts := _parse_iso_utc(r["captured_at"])) is not None and _ts >= cycle_start
    ]

    days_in_cycle_elapsed = _weekday_seconds_between(cycle_start, now) / 86400
    days_until_reset = _weekday_seconds_between(now, cycle_end) / 86400

    # Require at least 1 weekday-hour of data before computing a burn rate,
    # matching the burn_24h guard and preventing absurd rates at cycle start.
    if not cycle_rows or days_in_cycle_elapsed < 1 / 24:
        return {
            "daily_burn_rate": None,
            "days_remaining": round(days_until_reset, 1),
            "exhaustion_date": None,
            "cycle_reset_date": cycle_end.strftime("%Y-%m-%d"),
        }

    last = cycle_rows[-1]
    current_used = last["used"] or 0
    quota = last["quota"]

    # Burn rate = credits used since cycle start / elapsed weekdays
    daily_burn = current_used / days_in_cycle_elapsed

    exhaustion_date = None
    days_to_exhaustion = None
    exhausts_before_reset = False
    if daily_burn > 0 and quota is not None:
        remaining_credits = quota - current_used
        days_to_exhaustion = remaining_credits / daily_burn
        exhaustion_dt = _add_weekday_days(now, days_to_exhaustion)
        exhaustion_date = exhaustion_dt.strftime("%Y-%m-%d")
        exhausts_before_reset = exhaustion_dt < cycle_end

    # --- Today's burn rate (since midnight UTC) ---
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_rows = [
        r for r in rows
        if (_ts := _parse_iso_utc(r["captured_at"])) is not None and _ts >= today_start
    ]

    burn_24h = None
    days_to_exhaustion_24h = None
    exhaustion_date_24h = None
    exhausts_before_reset_24h = False

    if len(today_rows) >= 2:
        t_first = datetime.fromisoformat(today_rows[0]["captured_at"])
        t_last  = datetime.fromisoformat(today_rows[-1]["captured_at"])
        elapsed_hours = (t_last - t_first).total_seconds() / 3600
        # Require at least 1 hour of data before extrapolating a daily rate
        if elapsed_hours >= 1:
            elapsed_days = elapsed_hours / 24
            used_delta = (today_rows[-1]["used"] or 0) - (today_rows[0]["used"] or 0)
            burn_24h = used_delta / elapsed_days
            if burn_24h > 0 and quota is not None:
                remaining_credits_now = quota - current_used
                days_to_exhaustion_24h = remaining_credits_now / burn_24h
                exhaustion_dt_24h = _add_weekday_days(now, days_to_exhaustion_24h)
                exhaustion_date_24h = exhaustion_dt_24h.strftime("%Y-%m-%d")
                exhausts_before_reset_24h = exhaustion_dt_24h < cycle_end

    today_used = None
    if today_rows:
        first_val = today_rows[0]["used"] or 0
        last_val  = today_rows[-1]["used"] or 0
        today_used = max(0, last_val - first_val)

    credits_remaining = (quota - current_used) if quota is not None else None

    budget_per_day = None
    if credits_remaining is not None and days_until_reset > 0:
        # Cap at credits_remaining: near end-of-cycle days_until_reset → 0
        # which makes the raw division explode above the total quota.
        budget_per_day = round(min(credits_remaining / days_until_reset, credits_remaining), 1)

    projected_at_reset = None
    projected_over_quota = False
    if quota is not None:
        projected_at_reset = round(current_used + daily_burn * days_until_reset)
        projected_over_quota = projected_at_reset > quota

    return {
        "daily_burn_rate": round(daily_burn, 1),
        "days_remaining": round(days_until_reset, 1),
        "days_to_exhaustion": round(days_to_exhaustion, 1) if days_to_exhaustion is not None else None,
        "exhaustion_date": exhaustion_date,
        "exhausts_before_reset": exhausts_before_reset,
        "cycle_reset_date": cycle_end.strftime("%Y-%m-%d"),
        "burn_24h": round(burn_24h, 1) if burn_24h is not None else None,
        "days_to_exhaustion_24h": round(days_to_exhaustion_24h, 1) if days_to_exhaustion_24h is not None else None,
        "exhaustion_date_24h": exhaustion_date_24h,
        "exhausts_before_reset_24h": exhausts_before_reset_24h,
        "today_used": today_used,
        "credits_remaining": credits_remaining,
        "budget_per_day": budget_per_day,
        "projected_at_reset": projected_at_reset,
        "projected_over_quota": projected_over_quota,
    }


def _parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _rows_after_timestamp(rows: list[dict], since_ts: str) -> list[dict]:
    """Return rows with captured_at strictly newer than the given timestamp."""
    since_dt = _parse_iso_utc(since_ts)
    if since_dt is None:
        return []

    out: list[dict] = []
    for row in rows:
        row_dt = _parse_iso_utc(row.get("captured_at"))
        if row_dt is not None and row_dt > since_dt:
            out.append(row)
    return out


def _build_usage_payload(
    all_rows: list[dict],
    chart_rows: list[dict],
    *,
    include_chart: bool = True,
) -> dict:
    """Build the standard API payload for usage + forecast + session status."""
    stats = _burndown_stats(all_rows)

    latest = all_rows[-1] if all_rows else {}

    latest_ts = _parse_iso_utc(latest.get("captured_at"))
    now = datetime.now(timezone.utc)
    interval_seconds = _env_int("SCAN_INTERVAL", 120)
    stale_after_seconds = interval_seconds * 2
    age_seconds = None

    # Prefer the last scrape timestamp (updated even when data is unchanged due
    # to deduplication) so the staleness indicator reflects actual scraper
    # activity rather than only data changes.
    last_scrape_raw = db.get_metadata("last_scrape_at")
    last_scrape_ts = _parse_iso_utc(last_scrape_raw) if last_scrape_raw else None
    freshest_ts = max(
        (ts for ts in (latest_ts, last_scrape_ts) if ts is not None),
        default=None,
    )

    if freshest_ts is not None:
        age_seconds = int((now - freshest_ts).total_seconds())
    stale_data = age_seconds is None or age_seconds > stale_after_seconds

    auth = scraper.get_auth_status(
        timeout_ms=_AUTH_CHECK_TIMEOUT_MS,
        cache_ttl_seconds=_AUTH_STATUS_CACHE_SECONDS,
    )
    auth_payload = {
        "authenticated": auth["authenticated"],
        "estimated_expiry_utc": (
            auth["estimated_expiry_utc"].isoformat() if auth["estimated_expiry_utc"] else None
        ),
        "remaining_seconds": auth["remaining_seconds"],
        "note": auth["estimate_note"],
    }

    payload = {
        "latest_used": latest.get("used"),
        "quota": latest.get("quota"),
        "latest_captured_at": latest.get("captured_at"),
        "scan_interval_seconds": interval_seconds,
        "stale_after_seconds": stale_after_seconds,
        "data_age_seconds": age_seconds,
        "stale_data": stale_data,
        "auth": auth_payload,
        "stats": stats,
    }

    if include_chart:
        payload["chart"] = [
            {"x": r["captured_at"], "y": r["used"], "quota": r["quota"]}
            for r in chart_rows
        ]

    return payload


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    from_ts = request.args.get("from")  # ISO date/datetime string, inclusive
    to_ts = request.args.get("to")      # ISO date/datetime string, inclusive
    filtered = bool(from_ts or to_ts)

    now_epoch = time.time()
    if not filtered:
        with _API_DATA_CACHE_LOCK:
            cached_at = _API_DATA_CACHE["cached_at_epoch"]
            cached_payload = _API_DATA_CACHE["payload"]
            if (
                _API_DATA_CACHE_SECONDS > 0
                and cached_at is not None
                and cached_payload is not None
                and (now_epoch - cached_at) < _API_DATA_CACHE_SECONDS
            ):
                return jsonify(cached_payload)

    # Stats always use the full dataset so burndown is never degraded by chart filters.
    all_rows = db.query_history(metric_name="ai_credits", limit=5000)
    all_rows = [r for r in all_rows if r["used"] is not None]
    all_rows.sort(key=lambda r: r["captured_at"])

    # Chart data is filtered by the requested date range (or full if no filter).
    if filtered:
        chart_rows = db.query_history(
            metric_name="ai_credits", limit=5000, from_ts=from_ts, to_ts=to_ts
        )
        chart_rows = [r for r in chart_rows if r["used"] is not None]
        chart_rows.sort(key=lambda r: r["captured_at"])
    else:
        chart_rows = all_rows
    payload = _build_usage_payload(all_rows, chart_rows, include_chart=True)

    if not filtered:
        with _API_DATA_CACHE_LOCK:
            _API_DATA_CACHE["cached_at_epoch"] = now_epoch
            _API_DATA_CACHE["payload"] = payload
    return jsonify(payload)


@app.route("/api/data/updates")
def api_data_updates():
    """Return incremental chart points plus fresh stats/session summary.

    Query params:
      since   ISO date/datetime string used as an exclusive lower bound
    """
    since_ts_raw = request.args.get("since")
    if not since_ts_raw:
        return jsonify({"error": "missing required query param: since"}), 400

    # Some clients send a literal '+' in timezone offsets without URL-encoding,
    # which Flask decodes as a space. Normalize this to keep the API forgiving.
    since_ts = since_ts_raw.strip().replace(" ", "+")
    if _parse_iso_utc(since_ts) is None:
        return jsonify({"error": "invalid since timestamp; expected ISO-8601"}), 400

    all_rows = db.query_history(metric_name="ai_credits", limit=5000)
    all_rows = [r for r in all_rows if r["used"] is not None]
    all_rows.sort(key=lambda r: r["captured_at"])

    new_rows = _rows_after_timestamp(all_rows, since_ts)
    payload = _build_usage_payload(all_rows, new_rows, include_chart=False)
    payload["chart_append"] = [
        {"x": r["captured_at"], "y": r["used"], "quota": r["quota"]}
        for r in new_rows
    ]
    payload["since"] = since_ts

    return jsonify(payload)


@app.route("/api/daily")
def api_daily():
    """Credits consumed per weekday for the last 30 weekdays with data."""
    # tz_offset_minutes mirrors JS Date.getTimezoneOffset(): UTC - local, in minutes.
    tz_offset_minutes = request.args.get("tz_offset_minutes", 0, type=int)
    local_tz = timezone(timedelta(minutes=-tz_offset_minutes))

    rows = db.query_history(metric_name="ai_credits", limit=5000)
    rows = [r for r in rows if r["used"] is not None]
    rows.sort(key=lambda r: r["captured_at"])

    # Group by local weekday date string (YYYY-MM-DD)
    from collections import defaultdict
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        dt = _parse_iso_utc(r["captured_at"])
        if dt is None:
            continue
        local_dt = dt.astimezone(local_tz)
        if local_dt.weekday() >= 5:
            continue
        day = local_dt.strftime("%Y-%m-%d")
        by_day[day].append((local_dt, r["used"]))

    # Credits used each day = max - min within that day
    # active_hours: cluster credit-increase timestamps; gaps > 2h between increases start a new
    # working window. Sum each window's span (first increase → last increase within it).
    result = []
    previous_day_max = None
    for day in sorted(by_day):
        entries = sorted(by_day[day], key=lambda e: e[0])
        vals = [v for _, v in entries]
        day_min = min(vals)
        day_max = max(vals)
        if previous_day_max is not None and day_min < previous_day_max:
            previous_day_max = day_max
            continue
        credits = day_max - day_min

        active_times = [
            entries[j][0]
            for j in range(1, len(entries))
            if entries[j][1] > entries[j - 1][1]
        ]

        active_seconds = 0.0
        if active_times:
            w_start = active_times[0]
            w_end   = active_times[0]
            for t in active_times[1:]:
                if (t - w_end).total_seconds() > 7200:
                    active_seconds += (w_end - w_start).total_seconds()
                    w_start = t
                w_end = t
            active_seconds += (w_end - w_start).total_seconds()

        active_hours = round(active_seconds / 3600, 1)
        credits_per_hour = round(credits / active_hours, 1) if active_hours > 0 else None
        result.append({
            "date": day,
            "credits": credits,
            "active_hours": active_hours,
            "credits_per_hour": credits_per_hour,
        })
        previous_day_max = day_max

    return jsonify(result[-30:])


@app.route("/api/export")
def api_export():
    """Download all snapshots as JSON or CSV.

    Query params:
      format  csv | json  (default: json)
      from    ISO date/datetime, inclusive lower bound
      to      ISO date/datetime, inclusive upper bound
    """
    fmt = request.args.get("format", "json").lower()
    from_ts = request.args.get("from")
    to_ts = request.args.get("to")

    if fmt not in ("csv", "json"):
        return jsonify({"error": "format must be 'csv' or 'json'"}), 400

    rows = db.query_history(
        metric_name="ai_credits",
        limit=1_000_000,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    rows = [r for r in rows if r["used"] is not None]
    rows.sort(key=lambda r: r["captured_at"])

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["captured_at", "metric_name", "used", "quota", "raw_text"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=copilot-usage.csv"},
        )

    return Response(
        __import__("json").dumps(rows, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=copilot-usage.json"},
    )


@app.route("/api/errors")
def api_errors():
    """Return recent scrape errors.

    Query params:
      limit   max rows to return (default: 50)
      from    ISO date/datetime, inclusive lower bound
      to      ISO date/datetime, inclusive upper bound
    """
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    from_ts = request.args.get("from")
    to_ts = request.args.get("to")

    rows = db.query_scrape_errors(limit=limit, from_ts=from_ts, to_ts=to_ts)
    return jsonify(rows)


if __name__ == "__main__":
    _bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    app.run(debug=False, host=_bind_host, port=int(os.environ.get("PORT", "5000")))
