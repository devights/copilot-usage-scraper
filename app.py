"""Simple Flask dashboard for Copilot AI credit usage."""

import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import db
import scraper

app = Flask(__name__)


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

    days_in_cycle_elapsed = (now - cycle_start).total_seconds() / 86400
    days_until_reset = (cycle_end - now).total_seconds() / 86400

    if not cycle_rows or days_in_cycle_elapsed < 1 / 1440:
        return {
            "daily_burn_rate": None,
            "days_remaining": round(days_until_reset, 1),
            "exhaustion_date": None,
            "cycle_reset_date": cycle_end.strftime("%Y-%m-%d"),
        }

    last = cycle_rows[-1]
    current_used = last["used"] or 0
    quota = last["quota"]

    # Burn rate = credits used since cycle start / elapsed days
    daily_burn = current_used / days_in_cycle_elapsed

    exhaustion_date = None
    days_to_exhaustion = None
    exhausts_before_reset = False
    if daily_burn > 0 and quota is not None:
        remaining_credits = quota - current_used
        days_to_exhaustion = remaining_credits / daily_burn
        exhaustion_dt = now + timedelta(days=days_to_exhaustion)
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
                exhaustion_dt_24h = now + timedelta(days=days_to_exhaustion_24h)
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
        budget_per_day = round(credits_remaining / days_until_reset, 1)

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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    now_epoch = time.time()
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

    rows = db.query_history(metric_name="ai_credits", limit=5000)
    rows = [r for r in rows if r["used"] is not None]
    rows.sort(key=lambda r: r["captured_at"])

    stats = _burndown_stats(rows)

    chart_data = [
        {"x": r["captured_at"], "y": r["used"], "quota": r["quota"]}
        for r in rows
    ]

    latest = rows[-1] if rows else {}

    latest_ts = _parse_iso_utc(latest.get("captured_at"))
    now = datetime.now(timezone.utc)
    interval_seconds = int(os.environ.get("SCAN_INTERVAL", 3600))
    stale_after_seconds = interval_seconds * 2
    age_seconds = None
    if latest_ts is not None:
        age_seconds = int((now - latest_ts).total_seconds())
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
        "expiry_source_cookie": auth["expiry_source_cookie"],
        "note": auth["estimate_note"],
    }

    payload = {
        "chart": chart_data,
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
    with _API_DATA_CACHE_LOCK:
        _API_DATA_CACHE["cached_at_epoch"] = now_epoch
        _API_DATA_CACHE["payload"] = payload
    return jsonify(payload)


@app.route("/api/daily")
def api_daily():
    """Credits consumed per calendar day for the last 30 days."""
    # tz_offset_minutes mirrors JS Date.getTimezoneOffset(): UTC - local, in minutes.
    tz_offset_minutes = request.args.get("tz_offset_minutes", 0, type=int)
    local_tz = timezone(timedelta(minutes=-tz_offset_minutes))

    rows = db.query_history(metric_name="ai_credits", limit=5000)
    rows = [r for r in rows if r["used"] is not None]
    rows.sort(key=lambda r: r["captured_at"])

    # Group by local date string (YYYY-MM-DD)
    from collections import defaultdict
    by_day: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        dt = datetime.fromisoformat(r["captured_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(local_tz).strftime("%Y-%m-%d")
        by_day[day].append(r["used"])

    # Credits used each day = max - min within that day
    # Ignore days where the counter reset (min > previous day's max)
    result = []
    for day in sorted(by_day)[-30:]:
        vals = by_day[day]
        delta = max(vals) - min(vals)
        result.append({"date": day, "credits": delta})

    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
