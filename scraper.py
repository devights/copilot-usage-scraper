"""
Playwright-based scraper for https://github.com/settings/copilot/features.

Auth strategy: persistent browser context stored in ~/.config/gh-scraper/browser-state/.
Run with --login on first use; subsequent runs reuse the saved session.
"""

import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

COPILOT_URL = "https://github.com/settings/copilot/features"
STATE_DIR = Path(os.environ.get("BROWSER_STATE_DIR", Path.home() / ".config" / "gh-scraper" / "browser-state"))
DEBUG_HTML = Path(__file__).parent / "debug_page.html"

# Matches "6,841 / 12,000 AI credits" (quota present)
_CREDITS_RE = re.compile(r"([\d,]+)\s*/\s*([\d,]+)\s+AI credits", re.IGNORECASE)
# Matches "951 AI credits used" (no quota set)
_CREDITS_USED_RE = re.compile(r"([\d,]+)\s+AI credits used", re.IGNORECASE)
_AUTH_URL_MARKERS = ("login", "signin", "session")
_AUTH_COOKIE_PRIORITY = (
    "user_session",
    "_gh_sess",
    "logged_in",
    "dotcom_user",
)

_AUTH_CACHE_LOCK = threading.Lock()
_AUTH_CACHE: dict = {
    "checked_at_epoch": None,
    "payload": None,
}


def _parse_int(text: str) -> int | None:
    cleaned = text.replace(",", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


def _apply_quota_override(metrics: list[dict]) -> list[dict]:
    value = os.environ.get("QUOTA_OVERRIDE")
    if value is None or not value.strip():
        return metrics

    quota = _parse_int(value)
    if quota is None or quota <= 0:
        raise ValueError("QUOTA_OVERRIDE must be a positive integer")

    for metric in metrics:
        metric["quota"] = quota
    return metrics


def _extract_metrics_from_html(page) -> list[dict]:
    """
    Extract AI credit usage from the 'Usage this cycle' section.
    Handles two formats:
      - quota present:  "6,841 / 12,000 AI credits"
      - no quota:       "951 AI credits used" (quota stored as None)
    """
    metrics: list[dict] = []

    for el in page.query_selector_all("span.color-fg-muted"):
        text = el.inner_text().strip()

        # Quota present: "X / Y AI credits"
        m = _CREDITS_RE.search(text)
        if m:
            metrics.append({
                "metric_name": "ai_credits",
                "used": _parse_int(m.group(1)),
                "quota": _parse_int(m.group(2)),
                "raw_text": text,
            })
            break

        # No quota: "X AI credits used"
        m2 = _CREDITS_USED_RE.search(text)
        if m2:
            metrics.append({
                "metric_name": "ai_credits",
                "used": _parse_int(m2.group(1)),
                "quota": None,
                "raw_text": text,
            })
            break

    return metrics


def _is_login_redirect(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    return any(path == f"/{m}" or path.startswith(f"/{m}/") for m in _AUTH_URL_MARKERS)


def _pick_expiry_cookie(cookies: list[dict]) -> tuple[str | None, float | None]:
    exp_by_name: dict[str, float] = {}
    for c in cookies:
        name = c.get("name")
        expires = c.get("expires")
        if not name or expires is None:
            continue
        try:
            exp_val = float(expires)
        except (TypeError, ValueError):
            continue
        if exp_val <= 0:
            continue
        exp_by_name[name] = exp_val

    for name in _AUTH_COOKIE_PRIORITY:
        if name in exp_by_name:
            return name, exp_by_name[name]

    if not exp_by_name:
        return None, None

    # Fall back to the earliest known cookie expiry as a conservative estimate.
    fallback_name = min(exp_by_name, key=exp_by_name.get)
    return fallback_name, exp_by_name[fallback_name]


def _build_auth_payload(
    *,
    authenticated: bool,
    current_url: str,
    cookie_name: str | None,
    expires_at: float | None,
) -> dict:
    estimated_expiry_utc = None
    remaining_seconds = None
    if expires_at is not None:
        estimated_expiry_utc = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        remaining_seconds = int(expires_at - datetime.now(tz=timezone.utc).timestamp())

    if expires_at is None:
        estimate_note = "No persistent auth-cookie expiry found (session cookie or unavailable)."
    else:
        estimate_note = "Estimate only: GitHub can invalidate sessions before cookie expiry."

    return {
        "authenticated": authenticated,
        "current_url": current_url,
        "state_dir": str(STATE_DIR),
        "expiry_source_cookie": cookie_name,
        "estimated_expiry_utc": estimated_expiry_utc,
        "remaining_seconds": remaining_seconds,
        "estimate_note": estimate_note,
    }


def get_auth_status(
    *,
    timeout_ms: int = 2_500,
    cache_ttl_seconds: int = 300,
    force_refresh: bool = False,
) -> dict:
    """
    Return current auth status and a best-effort session expiry estimate.
    Uses a short-lived in-process cache so API calls do not repeatedly launch
    a browser context.
    """
    now_epoch = datetime.now(tz=timezone.utc).timestamp()
    with _AUTH_CACHE_LOCK:
        cached_checked_at = _AUTH_CACHE["checked_at_epoch"]
        cached_payload = _AUTH_CACHE["payload"]
        if (
            not force_refresh
            and cache_ttl_seconds > 0
            and cached_checked_at is not None
            and cached_payload is not None
            and (now_epoch - cached_checked_at) < cache_ttl_seconds
        ):
            return cached_payload

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            str(STATE_DIR),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page() if not browser.pages else browser.pages[0]

        try:
            # DOM content is enough to detect login redirect; networkidle can be slow.
            page.goto(COPILOT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except PWTimeout:
            pass

        current_url = page.url
        authenticated = not _is_login_redirect(current_url)

        cookies = browser.cookies(["https://github.com"])
        cookie_name, expires_at = _pick_expiry_cookie(cookies)
        browser.close()

    payload = _build_auth_payload(
        authenticated=authenticated,
        current_url=current_url,
        cookie_name=cookie_name,
        expires_at=expires_at,
    )
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE["checked_at_epoch"] = now_epoch
        _AUTH_CACHE["payload"] = payload
    return payload


def scrape(*, headless: bool = True, debug: bool = False) -> list[dict]:
    """
    Scrape Copilot usage stats and return a list of metric dicts.
    Raises SystemExit if the user is not authenticated.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            str(STATE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page() if not browser.pages else browser.pages[0]

        print(f"  Navigating to {COPILOT_URL} …")
        try:
            page.goto(COPILOT_URL, wait_until="networkidle", timeout=30_000)
        except PWTimeout:
            # Partial load — try to extract whatever loaded
            print("  [WARN] Page load timed out; attempting extraction from partial content.", file=sys.stderr)

        # Detect redirect to login page
        if _is_login_redirect(page.url):
            browser.close()
            raise RuntimeError(
                "Not authenticated. Run with --login to open a browser and log in first."
            )

        if debug:
            DEBUG_HTML.write_text(page.content(), encoding="utf-8")
            print(f"  Debug HTML saved to {DEBUG_HTML}")

        metrics = _apply_quota_override(_extract_metrics_from_html(page))
        browser.close()

    return metrics


def login() -> None:
    """Open a visible browser so the user can log into GitHub interactively."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    print("Opening browser — please log into GitHub, then close the browser window.")
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            str(STATE_DIR),
            headless=False,
        )
        page = browser.new_page() if not browser.pages else browser.pages[0]
        page.goto("https://github.com/login")
        # Wait until the user navigates away from the login page
        try:
            page.wait_for_url(
                re.compile(r"github\.com/(?!login|session|signup)"),
                timeout=120_000,
            )
            print("  Login detected — session saved.")
        except PWTimeout:
            print("  Timed out waiting for login. Session may not be saved.")
        finally:
            browser.close()
