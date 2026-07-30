from datetime import datetime, timedelta, timezone

import app


def test_env_int_returns_default_for_invalid_value(monkeypatch):
    monkeypatch.setenv("BAD_INT", "not-an-int")
    assert app._env_int("BAD_INT", 123) == 123


def test_weekday_seconds_between_skips_weekends():
    start = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)  # Friday noon
    end = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)     # Monday noon

    seconds = app._weekday_seconds_between(start, end)

    # Friday noon->midnight (12h) + Monday midnight->noon (12h) = 24h
    assert seconds == 24 * 3600


def test_add_weekday_days_skips_weekend_time():
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)  # Friday noon

    # Add one weekday day (24h of weekday time):
    # Fri noon->midnight (12h), then Mon midnight->noon (12h)
    out = app._add_weekday_days(start, 1.0)

    assert out == datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def test_api_daily_skips_weekends_and_reset_days(monkeypatch):
    rows = [
        # Friday (kept): delta = 10
        {"captured_at": "2026-07-24T09:00:00+00:00", "used": 90},
        {"captured_at": "2026-07-24T18:00:00+00:00", "used": 100},

        # Saturday (ignored)
        {"captured_at": "2026-07-25T12:00:00+00:00", "used": 101},

        # Monday reset day (min 10 < previous Friday max 100) -> skipped
        {"captured_at": "2026-07-27T09:00:00+00:00", "used": 10},
        {"captured_at": "2026-07-27T18:00:00+00:00", "used": 30},

        # Tuesday (kept): delta = 20
        {"captured_at": "2026-07-28T09:00:00+00:00", "used": 30},
        {"captured_at": "2026-07-28T18:00:00+00:00", "used": 50},

        # Invalid timestamp (ignored)
        {"captured_at": "bad-date", "used": 999},
    ]

    monkeypatch.setattr(app.db, "query_history", lambda metric_name, limit: rows)

    with app.app.test_client() as client:
        resp = client.get("/api/daily?tz_offset_minutes=0")

    assert resp.status_code == 200
    assert resp.get_json() == [
        {"date": "2026-07-24", "credits": 10},
        {"date": "2026-07-28", "credits": 20},
    ]


def test_api_daily_limits_to_last_30_weekdays(monkeypatch):
    def weekday_dates(start_date, n):
        out = []
        d = start_date
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    days = weekday_dates(datetime(2026, 1, 1, tzinfo=timezone.utc), 35)
    rows = []
    for i, day in enumerate(days):
        used_start = i * 10
        used_end = used_start + 5
        rows.append({"captured_at": day.replace(hour=9).isoformat(), "used": used_start})
        rows.append({"captured_at": day.replace(hour=18).isoformat(), "used": used_end})

    monkeypatch.setattr(app.db, "query_history", lambda metric_name, limit: rows)

    with app.app.test_client() as client:
        resp = client.get("/api/daily?tz_offset_minutes=0")

    payload = resp.get_json()
    assert resp.status_code == 200
    assert len(payload) == 30

    expected_days = [d.strftime("%Y-%m-%d") for d in days][-30:]
    assert [r["date"] for r in payload] == expected_days
    assert all(r["credits"] == 5 for r in payload)
