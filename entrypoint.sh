#!/bin/sh
set -e

case "$1" in
  login)
    VNC_PORT="${VNC_PORT:-5900}"
    echo ""
    echo "  ┌─────────────────────────────────────────────────────┐"
    echo "  │  gh-scraper login                                   │"
    echo "  │                                                     │"
    echo "  │  1. Open a VNC viewer and connect to:               │"
    echo "  │       localhost:${VNC_PORT}  (no password)                │"
    echo "  │                                                     │"
    echo "  │     macOS:   Finder ▸ Go ▸ Connect to Server       │"
    echo "  │              vnc://localhost:${VNC_PORT}                   │"
    echo "  │     Linux:   gvncviewer localhost::${VNC_PORT}            │"
    echo "  │     Windows: TightVNC / RealVNC → localhost:${VNC_PORT}    │"
    echo "  │                                                     │"
    echo "  │  2. A browser window will open — log in to GitHub.  │"
    echo "  │  3. Once logged in, close the browser and press     │"
    echo "  │     Ctrl+C here to stop.                            │"
    echo "  └─────────────────────────────────────────────────────┘"
    echo ""

    Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
    XVFB_PID=$!

    i=0; while [ $i -lt 20 ] && ! DISPLAY=:99 xdpyinfo >/dev/null 2>&1; do
      sleep 0.2; i=$((i+1))
    done

    x11vnc -display :99 -nopw -listen localhost -rfbport "${VNC_PORT}" \
           -forever -shared -quiet &
    VNC_PID=$!

    trap 'kill "${VNC_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true' EXIT

    DISPLAY=:99 python main.py login
    trap - EXIT

    kill $VNC_PID $XVFB_PID 2>/dev/null || true
    echo "Login complete. Browser state saved to ${BROWSER_STATE_DIR}."
    ;;
  serve)
    RELOAD=""
    [ "${FLASK_DEBUG:-0}" = "1" ] && RELOAD="--reload"
    exec flask --app app run --host 0.0.0.0 --port "${PORT:-5000}" $RELOAD
    ;;
  watch)
    exec python main.py watch --interval "${SCAN_INTERVAL:-3600}"
    ;;
  *)
    RELOAD=""
    [ "${FLASK_DEBUG:-0}" = "1" ] && RELOAD="--reload"
    python main.py watch --interval "${SCAN_INTERVAL:-3600}" &
    WATCHER_PID=$!
    flask --app app run --host 0.0.0.0 --port "${PORT:-5000}" $RELOAD &
    FLASK_PID=$!
    trap 'kill $WATCHER_PID $FLASK_PID 2>/dev/null; exit 0' INT TERM
    wait $WATCHER_PID $FLASK_PID
    ;;
esac
