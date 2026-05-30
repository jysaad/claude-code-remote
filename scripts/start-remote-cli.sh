#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"

# Prefer local-bin venv (TCC-safe under launchd); fall back to repo venv
if [ -x "$HOME/.local/bin/remote-cli/.venv/bin/python3" ]; then
    VENV_PYTHON="$HOME/.local/bin/remote-cli/.venv/bin/python3"
else
    VENV_PYTHON="$HOME/Workspace/Products/JohnOS/PhoneWrapper/.venv/bin/python3"
fi

mkdir -p "$LOG_DIR"

# Single-instance: replace any prior watchdog. Two watchdogs racing on the
# same ports causes ttyd EADDRINUSE crash-loops, which surface to the phone
# as repeated "Press ⏎ to Reconnect" prompts.
WATCHDOG_PID_FILE="$LOG_DIR/watchdog.pid"
if [ -f "$WATCHDOG_PID_FILE" ]; then
    OLD_WATCHDOG_PID=$(cat "$WATCHDOG_PID_FILE" 2>/dev/null || true)
    if [ -n "$OLD_WATCHDOG_PID" ] && kill -0 "$OLD_WATCHDOG_PID" 2>/dev/null; then
        echo "Replacing existing watchdog (PID $OLD_WATCHDOG_PID)"
        kill -TERM "$OLD_WATCHDOG_PID" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$OLD_WATCHDOG_PID" 2>/dev/null || break
            sleep 0.5
        done
        kill -KILL "$OLD_WATCHDOG_PID" 2>/dev/null || true
    fi
fi
echo "$$" > "$WATCHDOG_PID_FILE"

# Block until a TCP port is free (sockets sit in TIME_WAIT after pkill).
wait_for_port_free() {
    local port=$1
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    return 1
}

# Get Tailscale IP
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null)
if [ -z "$TAILSCALE_IP" ]; then
    echo "ERROR: Tailscale not running or no IPv4 address" >&2
    exit 1
fi

echo "Tailscale IP: $TAILSCALE_IP"

# Auto-detect: bind loopback if Tailscale Serve is fronting ttyd here
# (Studio); otherwise bind tailnet IP so the Pixel can hit ttyd directly (MBP).
if tailscale serve status 2>/dev/null | grep -qE "127\.0\.0\.1:7681|localhost:7681"; then
    TTYD_BIND="127.0.0.1"
else
    TTYD_BIND="$TAILSCALE_IP"
fi
echo "ttyd bind: $TTYD_BIND"

# Kill any existing ttyd processes
pkill -f "ttyd" 2>/dev/null || true
wait_for_port_free 7681 || echo "WARN: port 7681 still busy after 10s, attempting bind anyway" >&2

# Keep Mac awake (kill any existing caffeinate first)
pkill -f "caffeinate" 2>/dev/null || true
caffeinate -d -i -s &
CAFFEINATE_PID=$!
echo "caffeinate running (PID: $CAFFEINATE_PID)"

# Start ttyd bound to Tailscale IP only
# Uses tmux-attach.sh wrapper for clean argument handling
ttyd \
    --port 7681 \
    --interface "$TTYD_BIND" \
    --writable \
    -t fontSize=12 \
    -t reconnect=3 \
    -t lineHeight=1.1 \
    -t cursorBlink=true \
    -t cursorStyle=block \
    -t scrollback=10000 \
    -t disableLeaveAlert=true \
    -t disableResizeOverlay=true \
    -t 'fontFamily="Menlo, Monaco, Consolas, monospace, Apple Color Emoji, Segoe UI Emoji"' \
    "$SCRIPT_DIR/tmux-attach.sh" \
    >> "$LOG_DIR/ttyd.log" 2>&1 &

TTYD_PID=$!
echo "ttyd running (PID: $TTYD_PID) on 127.0.0.1:7681 (public via Tailscale Serve)"

# Start voice dictation wrapper
pkill -f "voice-wrapper" 2>/dev/null || true
wait_for_port_free 8080 || echo "WARN: port 8080 still busy after 10s, attempting bind anyway" >&2
"$VENV_PYTHON" "$SCRIPT_DIR/voice-wrapper.py" >> "$LOG_DIR/voice-wrapper.log" 2>&1 &
WRAPPER_PID=$!
echo "voice wrapper running (PID: $WRAPPER_PID) on 127.0.0.1:8080 (public via Tailscale Serve)"

TS_HOST=$(tailscale status --json 2>/dev/null | sed -n 's/.*"DNSName": "\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//')
echo ""
echo "=== Remote CLI Ready ==="
echo "Voice UI:  https://$TS_HOST/"
echo "Terminal:  https://$TS_HOST:8443/"
echo ""
echo "Open the Voice UI URL in Chrome on your iPhone (Tailscale must be active)."
echo "To stop: $SCRIPT_DIR/stop-remote-cli.sh"

# Save PIDs for stop script
echo "$TTYD_PID" > "$LOG_DIR/ttyd.pid"
echo "$CAFFEINATE_PID" > "$LOG_DIR/caffeinate.pid"
echo "$WRAPPER_PID" > "$LOG_DIR/voice-wrapper.pid"

# Watchdog: restart ttyd or wrapper if either crashes, exit cleanly on SIGTERM.
# On exit we kill children AND clear the watchdog PID file so the next
# start-remote-cli.sh doesn't think a watchdog is still owning the port.
KEEP_RUNNING=true
trap 'KEEP_RUNNING=false; kill $TTYD_PID $WRAPPER_PID 2>/dev/null; rm -f "$WATCHDOG_PID_FILE"' TERM INT EXIT

while $KEEP_RUNNING; do
    sleep 5
    $KEEP_RUNNING || break

    if ! kill -0 $TTYD_PID 2>/dev/null; then
        echo "[$(date)] ttyd exited, restarting..." >> "$LOG_DIR/ttyd.log"
        wait_for_port_free 7681 || echo "[$(date)] WARN: port 7681 still busy on restart" >> "$LOG_DIR/ttyd.log"
        ttyd \
            --port 7681 \
            --interface "$TTYD_BIND" \
            --writable \
            -t fontSize=12 \
            -t reconnect=3 \
            -t lineHeight=1.1 \
            -t cursorBlink=true \
            -t cursorStyle=block \
            -t scrollback=10000 \
            -t disableLeaveAlert=true \
            -t 'fontFamily="Menlo, Monaco, Consolas, monospace, Apple Color Emoji, Segoe UI Emoji"' \
            "$SCRIPT_DIR/tmux-attach.sh" \
            >> "$LOG_DIR/ttyd.log" 2>&1 &
        TTYD_PID=$!
        echo "$TTYD_PID" > "$LOG_DIR/ttyd.pid"
        echo "[$(date)] ttyd restarted (PID: $TTYD_PID)" >> "$LOG_DIR/ttyd.log"
    fi

    if ! kill -0 $WRAPPER_PID 2>/dev/null; then
        echo "[$(date)] voice-wrapper exited, restarting..." >> "$LOG_DIR/voice-wrapper.log"
        wait_for_port_free 8080 || echo "[$(date)] WARN: port 8080 still busy on restart" >> "$LOG_DIR/voice-wrapper.log"
        "$VENV_PYTHON" "$SCRIPT_DIR/voice-wrapper.py" >> "$LOG_DIR/voice-wrapper.log" 2>&1 &
        WRAPPER_PID=$!
        echo "$WRAPPER_PID" > "$LOG_DIR/voice-wrapper.pid"
        echo "[$(date)] voice-wrapper restarted (PID: $WRAPPER_PID)" >> "$LOG_DIR/voice-wrapper.log"
    fi
done
