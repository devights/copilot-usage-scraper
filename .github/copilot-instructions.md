# gh-scraper — Project Guidelines

## Overview

Self-hosted tool that scrapes GitHub Copilot AI credit usage from
`https://github.com/settings/copilot/features` using a persistent Playwright
browser session, stores snapshots in SQLite, and serves a burndown dashboard
via a local Flask web UI.

## Architecture

| File | Purpose |
|------|---------|
| `scraper.py` | Playwright scraper + auth-status check with in-process cache |
| `db.py` | SQLite layer — `init_db`, `save_snapshot` (dedup), `query_history` (date filter), `get_last_snapshot` |
| `app.py` | Flask API — `/api/data`, `/api/daily`, `/api/export` |
| `main.py` | CLI entrypoint — `login`, `scrape`, `watch`, `history`, `auth-status` |
| `templates/index.html` | Single-page dashboard — Chart.js, CSS custom-property theming, no build step |
| `tests/test_app.py` | Flask + db unit tests (pytest + monkeypatch) |
| `tests/test_scraper.py` | Scraper pure-function + Playwright-mocked tests |

## Build & Test

```bash
# Activate the venv first — always required
source .venv/bin/activate

# Run all tests
pytest tests/ -v

# One-shot scrape
python main.py scrape

# Continuous watch mode (used by Docker entrypoint)
python main.py watch --interval 3600

# Open browser for initial GitHub login
python main.py login

# Check auth / session expiry
python main.py auth-status
```

Docker workflow:
```bash
docker compose up -d          # start scraper + dashboard
docker compose logs -f        # tail logs
docker compose down           # stop
```

## Conventions

**Database**
- `save_snapshot` skips the INSERT when `used` and `quota` are identical to the last row (deduplication). It returns the number of rows actually inserted.
- `query_history` accepts `from_ts` / `to_ts` ISO strings for date-range filtering and always includes `raw_text` in the SELECT.
- Always call `db.init_db()` before any DB operation in CLI commands.

**API**
- `/api/data` — stats are always computed from the **full** dataset; only `chart` data is filtered by `?from` / `?to` params. The 10s in-process cache is bypassed when date params are present.
- `/api/export` — accepts `?format=csv|json` + `?from` / `?to`; responds with `Content-Disposition: attachment`.
- All Flask routes live in `app.py`; no blueprints.

**Frontend**
- No build step. Vanilla JS + Chart.js 4.4 from CDN.
- Theme is controlled by `data-theme` attribute on `<html>`; toggle persists to `localStorage`.
- Stat cards (This Cycle / Today / Forecast) always use unfiltered `/api/data`. The date picker only zooms the line chart via a separate filtered fetch.
- Use `cssVar('--name')` helper to read CSS custom properties for Chart.js color updates on theme switch.

**Scraper**
- Uses a persistent Chromium profile in `BROWSER_STATE_DIR` (default `~/.config/gh-scraper/browser-state`).
- `get_auth_status` has a 5-minute in-process cache; pass `force_refresh=True` to bust it.
- `_extract_metrics_from_html` matches `span.color-fg-muted` elements against `_CREDITS_RE`. Run `python main.py scrape --debug` to dump the raw page to `debug_page.html` when the selector breaks.

**Testing**
- Playwright is never invoked in tests; `scraper.sync_playwright` is monkeypatched with `unittest.mock.patch`.
- DB tests use `tmp_path` fixtures — never touch `usage.db`.
- Flask tests use `monkeypatch.setattr(app.db, "query_history", ...)` to stub the DB layer.
- The `_AUTH_CACHE` dict must be reset at the start of any `get_auth_status` test to prevent inter-test bleed.

## Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_PATH` | `./usage.db` | SQLite file path |
| `BROWSER_STATE_DIR` | `~/.config/gh-scraper/browser-state` | Playwright profile |
| `SCAN_INTERVAL` | `3600` | Seconds between scrapes in watch mode |
| `API_DATA_CACHE_SECONDS` | `10` | TTL for `/api/data` in-process cache |
| `AUTH_STATUS_CACHE_SECONDS` | `300` | TTL for auth-status in-process cache |
| `PORT` | `5000` | Flask listen port |
