"""Tests for scraper.py — pure functions and Playwright-mocked integration paths."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import scraper


# ── _parse_int ────────────────────────────────────────────────────────────────

def test_parse_int_plain():
    assert scraper._parse_int("1234") == 1234


def test_parse_int_comma_separated():
    assert scraper._parse_int("12,345") == 12345


def test_parse_int_non_numeric():
    assert scraper._parse_int("abc") is None


def test_parse_int_empty():
    assert scraper._parse_int("") is None


def test_parse_int_whitespace_stripped():
    assert scraper._parse_int("  99  ") == 99


# ── _is_login_redirect ────────────────────────────────────────────────────────

def test_is_login_redirect_login():
    assert scraper._is_login_redirect("https://github.com/login") is True


def test_is_login_redirect_login_subpath():
    assert scraper._is_login_redirect("https://github.com/login/oauth") is True


def test_is_login_redirect_signin():
    assert scraper._is_login_redirect("https://github.com/signin") is True


def test_is_login_redirect_session():
    assert scraper._is_login_redirect("https://github.com/session") is True


def test_is_login_redirect_settings_not_redirect():
    assert scraper._is_login_redirect("https://github.com/settings/copilot/features") is False


def test_is_login_redirect_root_not_redirect():
    assert scraper._is_login_redirect("https://github.com/") is False


def test_is_login_redirect_empty_url():
    assert scraper._is_login_redirect("") is False


# ── _pick_expiry_cookie ───────────────────────────────────────────────────────

def test_pick_expiry_cookie_returns_priority_name():
    """user_session is preferred over dotcom_user."""
    cookies = [
        {"name": "dotcom_user",  "expires": 9_999_999_990.0},
        {"name": "user_session", "expires": 9_000_000_000.0},
    ]
    name, exp = scraper._pick_expiry_cookie(cookies)
    assert name == "user_session"
    assert exp == 9_000_000_000.0


def test_pick_expiry_cookie_no_positive_expires():
    cookies = [
        {"name": "some_cookie", "expires": -1},
        {"name": "other",       "expires": 0},
    ]
    name, exp = scraper._pick_expiry_cookie(cookies)
    assert name is None
    assert exp is None


def test_pick_expiry_cookie_empty_list():
    name, exp = scraper._pick_expiry_cookie([])
    assert name is None
    assert exp is None


def test_pick_expiry_cookie_fallback_when_no_priority_match():
    """Falls back to the cookie with the smallest positive expiry."""
    cookies = [
        {"name": "unknown_a", "expires": 2_000_000_000.0},
        {"name": "unknown_b", "expires": 1_000_000_000.0},
    ]
    name, exp = scraper._pick_expiry_cookie(cookies)
    assert name == "unknown_b"
    assert exp == 1_000_000_000.0


# ── _build_auth_payload ───────────────────────────────────────────────────────

def test_build_auth_payload_authenticated_with_expiry():
    future_ts = datetime.now(tz=timezone.utc).timestamp() + 86400  # 1 day from now
    payload = scraper._build_auth_payload(
        authenticated=True,
        current_url="https://github.com/settings/copilot/features",
        cookie_name="user_session",
        expires_at=future_ts,
    )
    assert payload["authenticated"] is True
    assert payload["expiry_source_cookie"] == "user_session"
    assert isinstance(payload["estimated_expiry_utc"], datetime)
    assert payload["remaining_seconds"] > 0
    assert "Estimate only" in payload["estimate_note"]


def test_build_auth_payload_unauthenticated_no_expiry():
    payload = scraper._build_auth_payload(
        authenticated=False,
        current_url="https://github.com/login",
        cookie_name=None,
        expires_at=None,
    )
    assert payload["authenticated"] is False
    assert payload["estimated_expiry_utc"] is None
    assert payload["remaining_seconds"] is None
    assert "No persistent" in payload["estimate_note"]


# ── _extract_metrics_from_html ────────────────────────────────────────────────

class _MockElement:
    """Minimal stand-in for a Playwright ElementHandle."""
    def __init__(self, text: str):
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _MockPage:
    def __init__(self, elements):
        self._elements = elements

    def query_selector_all(self, selector):
        return self._elements


def test_extract_metrics_found():
    page = _MockPage([_MockElement("6,841 / 12,000 AI credits")])
    metrics = scraper._extract_metrics_from_html(page)
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "ai_credits"
    assert metrics[0]["used"] == 6841
    assert metrics[0]["quota"] == 12000
    assert "6,841" in metrics[0]["raw_text"]


def test_extract_metrics_no_match():
    page = _MockPage([_MockElement("Some unrelated text")])
    assert scraper._extract_metrics_from_html(page) == []


def test_extract_metrics_empty_page():
    page = _MockPage([])
    assert scraper._extract_metrics_from_html(page) == []


def test_extract_metrics_stops_at_first_match():
    """Only the first matching element is returned."""
    page = _MockPage([
        _MockElement("100 / 1,000 AI credits"),
        _MockElement("200 / 2,000 AI credits"),
    ])
    metrics = scraper._extract_metrics_from_html(page)
    assert len(metrics) == 1
    assert metrics[0]["used"] == 100


# ── scrape() — Playwright mocked ─────────────────────────────────────────────

def _make_browser_mock(url="https://github.com/settings/copilot/features", elements=None):
    """Return a mock playwright context + page pair."""
    if elements is None:
        elements = [_MockElement("1,000 / 12,000 AI credits")]

    page = MagicMock()
    page.url = url
    page.query_selector_all.return_value = elements

    browser = MagicMock()
    browser.pages = []
    browser.new_page.return_value = page

    pw = MagicMock()
    pw.chromium.launch_persistent_context.return_value = browser
    return pw, browser, page


def test_scrape_success(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "STATE_DIR", tmp_path)
    pw, browser, page = _make_browser_mock()

    with patch("scraper.sync_playwright") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = pw
        mock_ctx.return_value.__exit__.return_value = False
        metrics = scraper.scrape()

    assert len(metrics) == 1
    assert metrics[0]["used"] == 1000
    assert metrics[0]["quota"] == 12000
    browser.close.assert_called_once()


def test_scrape_unauthenticated_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "STATE_DIR", tmp_path)
    pw, browser, page = _make_browser_mock(url="https://github.com/login")

    with patch("scraper.sync_playwright") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = pw
        mock_ctx.return_value.__exit__.return_value = False
        try:
            scraper.scrape()
            assert False, "Expected RuntimeError"
        except RuntimeError as exc:
            assert "Not authenticated" in str(exc)

    browser.close.assert_called_once()


# ── get_auth_status() — caching ───────────────────────────────────────────────

def test_get_auth_status_authenticated(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "STATE_DIR", tmp_path)

    # Reset the in-process cache so previous test runs don't bleed through.
    with scraper._AUTH_CACHE_LOCK:
        scraper._AUTH_CACHE["checked_at_epoch"] = None
        scraper._AUTH_CACHE["payload"] = None

    future_ts = datetime.now(tz=timezone.utc).timestamp() + 86400
    pw, browser, page = _make_browser_mock()
    browser.cookies.return_value = [{"name": "user_session", "expires": future_ts}]

    with patch("scraper.sync_playwright") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = pw
        mock_ctx.return_value.__exit__.return_value = False
        status = scraper.get_auth_status(cache_ttl_seconds=0)

    assert status["authenticated"] is True
    assert status["remaining_seconds"] > 0


def test_get_auth_status_uses_cache(tmp_path, monkeypatch):
    """A second call within the TTL must not launch the browser again."""
    monkeypatch.setattr(scraper, "STATE_DIR", tmp_path)

    with scraper._AUTH_CACHE_LOCK:
        scraper._AUTH_CACHE["checked_at_epoch"] = None
        scraper._AUTH_CACHE["payload"] = None

    pw, browser, page = _make_browser_mock()
    browser.cookies.return_value = []

    with patch("scraper.sync_playwright") as mock_ctx:
        mock_ctx.return_value.__enter__.return_value = pw
        mock_ctx.return_value.__exit__.return_value = False

        scraper.get_auth_status(cache_ttl_seconds=300)  # populates cache
        scraper.get_auth_status(cache_ttl_seconds=300)  # should hit cache

    # sync_playwright (i.e. the browser launch) should only have been invoked once.
    assert mock_ctx.call_count == 1
