#!/usr/bin/env python3
"""
gh-scraper — capture GitHub Copilot usage stats into a local SQLite database.

Usage:
    python main.py login                    # Log into GitHub (run once)
    python main.py                          # Scrape and save a snapshot
    python main.py history                  # Print recent snapshots
    python main.py history --metric ai_credits
    python main.py auth-status              # Check auth and estimated session expiry
    python main.py scrape --debug           # Also save raw HTML for selector debugging
"""

import argparse
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import db
import scraper


def cmd_watch(args) -> None:
    db.init_db()
    interval = args.interval
    print(f"Watching — scraping every {interval}s. Press Ctrl+C to stop.\n")
    try:
        while True:
            try:
                metrics = scraper.scrape(headless=True, debug=False)
                if metrics:
                    db.save_snapshot(metrics)
                    for m in metrics:
                        quota_str = f" / {m['quota']}" if m["quota"] is not None else ""
                        print(f"  {m['metric_name']}: {m['used']}{quota_str}")
                else:
                    print("  [WARN] No metrics found this cycle.", file=sys.stderr)
            except Exception as exc:
                error_type = type(exc).__name__
                print(f"  [ERROR] {error_type}: {exc}", file=sys.stderr)
                try:
                    db.log_scrape_error(str(exc), error_type=error_type)
                except Exception:
                    pass
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_login(_args) -> None:
    scraper.login()


def cmd_scrape(args) -> None:
    db.init_db()
    print("Scraping Copilot usage…")
    try:
        metrics = scraper.scrape(headless=not args.visible, debug=args.debug)
    except Exception as exc:
        error_type = type(exc).__name__
        print(f"  [ERROR] {error_type}: {exc}", file=sys.stderr)
        db.log_scrape_error(str(exc), error_type=error_type)
        sys.exit(1)

    if not metrics:
        msg = "No usage metrics found — page layout may have changed."
        print(f"\n[WARN] {msg}\n  Run with --debug to save debug_page.html.", file=sys.stderr)
        db.log_scrape_error(msg, error_type="NoMetrics")
        sys.exit(2)

    db.save_snapshot(metrics)
    print(f"\nSaved {len(metrics)} metric(s):")
    for m in metrics:
        quota_str = f" / {m['quota']}" if m["quota"] is not None else ""
        print(f"  {m['metric_name']}: {m['used']}{quota_str}")


def cmd_history(args) -> None:
    db.init_db()
    rows = db.query_history(metric_name=args.metric, limit=args.limit)
    if not rows:
        print("No data yet. Run without --history to capture a snapshot.")
        return

    # Simple columnar output
    header = f"{'captured_at':>27}  {'metric_name':<35}  {'used':>8}  {'quota':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        quota_str = str(r["quota"]) if r["quota"] is not None else "—"
        used_str = str(r["used"]) if r["used"] is not None else "—"
        print(f"  {r['captured_at']:>25}  {r['metric_name']:<35}  {used_str:>8}  {quota_str:>8}")


def _format_remaining(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "expired"

    delta = timedelta(seconds=seconds)
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def cmd_auth_status(_args) -> None:
    status = scraper.get_auth_status()

    print("Auth status")
    print(f"  authenticated: {'yes' if status['authenticated'] else 'no'}")
    print(f"  current_url: {status['current_url']}")
    print(f"  browser_state_dir: {status['state_dir']}")
    print(
        "  expiry_source_cookie: "
        + (status["expiry_source_cookie"] if status["expiry_source_cookie"] else "unknown")
    )

    if status["estimated_expiry_utc"] is None:
        print("  estimated_expiry_utc: unknown")
    else:
        print(f"  estimated_expiry_utc: {status['estimated_expiry_utc'].isoformat()}")
    print(f"  time_remaining: {_format_remaining(status['remaining_seconds'])}")
    print(f"  note: {status['estimate_note']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape GitHub Copilot usage stats into a local SQLite DB."
    )
    sub = parser.add_subparsers(dest="cmd")

    # watch
    p_watch = sub.add_parser("watch", help="Scrape repeatedly on an interval (default 60s).")
    p_watch.add_argument("--interval", type=int, default=int(os.environ.get("SCAN_INTERVAL", 60)), metavar="SECONDS",
                         help="Seconds between scrapes (default: 60).")

    # login
    sub.add_parser("login", help="Open browser to log into GitHub (run once).")

    # scrape (default)
    p_scrape = sub.add_parser("scrape", help="Capture a usage snapshot (default).")
    p_scrape.add_argument("--visible", action="store_true", help="Show the browser window.")
    p_scrape.add_argument("--debug", action="store_true", help="Save raw HTML to debug_page.html.")

    # history
    p_hist = sub.add_parser("history", help="Print recent snapshots.")
    p_hist.add_argument("--metric", help="Filter by metric name.")
    p_hist.add_argument("--limit", type=int, default=50, help="Max rows to show (default 50).")

    # auth-status
    sub.add_parser("auth-status", help="Show auth status and estimated session expiry.")

    args = parser.parse_args()

    if args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "watch":
        cmd_watch(args)
    elif args.cmd == "auth-status":
        cmd_auth_status(args)
    elif args.cmd == "history":
        cmd_history(args)
    else:
        # Default: scrape (even if no subcommand given)
        if args.cmd is None:
            args.visible = False
            args.debug = False
        cmd_scrape(args)


if __name__ == "__main__":
    main()
