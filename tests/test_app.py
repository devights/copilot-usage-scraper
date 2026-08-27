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


# ── Deduplication ──────────────────────────────────────────────────────────────

def test_init_db_creates_parent_directory_and_schema(tmp_path):
    import db

    test_db = tmp_path / "data" / "usage.db"

    db.init_db(test_db)

    assert test_db.is_file()
    with db.get_conn(test_db) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"usage_snapshots", "scrape_errors", "metadata"} <= tables


def test_save_snapshot_deduplication(tmp_path):
    """Second save with identical used/quota must not insert a new row."""
    import db

    test_db = tmp_path / "test.db"
    db.init_db(test_db)

    metric = {"metric_name": "ai_credits", "used": 5000, "quota": 12000, "raw_text": "5,000 / 12,000 AI credits"}
    inserted_first  = db.save_snapshot([metric], test_db)
    inserted_second = db.save_snapshot([metric], test_db)  # identical — should be skipped

    rows = db.query_history(metric_name="ai_credits", db_path=test_db)
    assert inserted_first == 1
    assert inserted_second == 0
    assert len(rows) == 1


def test_save_snapshot_inserts_when_value_changes(tmp_path):
    """A new snapshot with different 'used' must be stored."""
    import db

    test_db = tmp_path / "test.db"
    db.init_db(test_db)

    db.save_snapshot([{"metric_name": "ai_credits", "used": 5000, "quota": 12000, "raw_text": ""}], test_db)
    db.save_snapshot([{"metric_name": "ai_credits", "used": 5100, "quota": 12000, "raw_text": ""}], test_db)

    rows = db.query_history(metric_name="ai_credits", db_path=test_db)
    assert len(rows) == 2


# ── Date-range filter ──────────────────────────────────────────────────────────

def test_api_data_date_range_filter(monkeypatch):
    """?from and ?to should restrict the chart series returned."""
    rows = [
        {"captured_at": "2026-07-01T12:00:00+00:00", "used": 100, "quota": 12000, "raw_text": ""},
        {"captured_at": "2026-07-15T12:00:00+00:00", "used": 200, "quota": 12000, "raw_text": ""},
        {"captured_at": "2026-07-29T12:00:00+00:00", "used": 300, "quota": 12000, "raw_text": ""},
    ]

    def fake_query(metric_name, limit, from_ts=None, to_ts=None, db_path=None):
        result = rows
        if from_ts:
            result = [r for r in result if r["captured_at"] >= from_ts]
        if to_ts:
            result = [r for r in result if r["captured_at"] <= to_ts]
        return result

    monkeypatch.setattr(app.db, "query_history", fake_query)
    monkeypatch.setattr(app.scraper, "get_auth_status", lambda **_: {
        "authenticated": True, "estimated_expiry_utc": None,
        "remaining_seconds": 99999, "expiry_source_cookie": None, "estimate_note": "",
    })

    with app.app.test_client() as client:
        resp = client.get("/api/data?from=2026-07-10&to=2026-07-20")

    assert resp.status_code == 200
    payload = resp.get_json()
    chart_dates = [p["x"][:10] for p in payload["chart"]]
    assert "2026-07-01" not in chart_dates
    assert "2026-07-15" in chart_dates
    assert "2026-07-29" not in chart_dates


def test_api_data_updates_since_returns_only_new_rows(monkeypatch):
    rows = [
        {"captured_at": "2026-07-01T12:00:00+00:00", "used": 100, "quota": 12000, "raw_text": ""},
        {"captured_at": "2026-07-15T12:00:00+00:00", "used": 200, "quota": 12000, "raw_text": ""},
        {"captured_at": "2026-07-29T12:00:00+00:00", "used": 300, "quota": 12000, "raw_text": ""},
    ]

    monkeypatch.setattr(app.db, "query_history", lambda **_: rows)
    monkeypatch.setattr(app.scraper, "get_auth_status", lambda **_: {
        "authenticated": True,
        "estimated_expiry_utc": None,
        "remaining_seconds": 99999,
        "expiry_source_cookie": None,
        "estimate_note": "",
    })

    with app.app.test_client() as client:
        resp = client.get("/api/data/updates?since=2026-07-10T00:00:00+00:00")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert "chart" not in payload
    assert [p["x"] for p in payload["chart_append"]] == [
        "2026-07-15T12:00:00+00:00",
        "2026-07-29T12:00:00+00:00",
    ]
    assert payload["latest_captured_at"] == "2026-07-29T12:00:00+00:00"


def test_api_data_updates_requires_valid_since(monkeypatch):
    monkeypatch.setattr(app.db, "query_history", lambda **_: [])
    monkeypatch.setattr(app.scraper, "get_auth_status", lambda **_: {
        "authenticated": True,
        "estimated_expiry_utc": None,
        "remaining_seconds": 99999,
        "expiry_source_cookie": None,
        "estimate_note": "",
    })

    with app.app.test_client() as client:
        resp_missing = client.get("/api/data/updates")
        resp_invalid = client.get("/api/data/updates?since=not-a-date")

    assert resp_missing.status_code == 400
    assert resp_invalid.status_code == 400


def test_api_export_csv(monkeypatch):
    rows = [
        {"captured_at": "2026-07-01T00:00:00+00:00", "metric_name": "ai_credits",
         "used": 100, "quota": 12000, "raw_text": "100 / 12,000 AI credits"},
    ]
    monkeypatch.setattr(app.db, "query_history", lambda **_: rows)

    with app.app.test_client() as client:
        resp = client.get("/api/export?format=csv")

    assert resp.status_code == 200
    assert resp.content_type.startswith("text/csv")
    text = resp.data.decode()
    assert "captured_at" in text
    assert "2026-07-01" in text


def test_api_export_json(monkeypatch):
    rows = [
        {"captured_at": "2026-07-01T00:00:00+00:00", "metric_name": "ai_credits",
         "used": 100, "quota": 12000, "raw_text": ""},
    ]
    monkeypatch.setattr(app.db, "query_history", lambda **_: rows)

    with app.app.test_client() as client:
        resp = client.get("/api/export?format=json")

    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert data[0]["used"] == 100


def test_api_export_invalid_format():
    with app.app.test_client() as client:
        resp = client.get("/api/export?format=xml")
    assert resp.status_code == 400
