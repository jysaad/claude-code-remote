#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"

echo "Stopping remote CLI services..."

# Stop the watchdog FIRST. Otherwise its EXIT trap will respawn ttyd and
# voice-wrapper the instant we kill them, defeating the stop.
if [ -f "$LOG_DIR/watchdog.pid" ]; then
    WATCHDOG_PID=$(cat "$LOG_DIR/watchdog.pid" 2>/dev/null || true)
    if [ -n "$WATCHDOG_PID" ] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
        echo "Stopping watchdog (PID $WATCHDOG_PID)..."
        kill -TERM "$WATCHDOG_PID" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$WATCHDOG_PID" 2>/dev/null || break
            sleep 0.5
        done
        kill -KILL "$WATCHDOG_PID" 2>/dev/null || true
    fi
    rm -f "$LOG_DIR/watchdog.pid"
fi
# Belt-and-suspenders: kill any orphan watchdog left over from a prior install
# that didn't write the watchdog.pid file.
pkill -f "start-remote-cli\.sh" 2>/dev/null || true

# Stop ttyd
if [ -f "$LOG_DIR/ttyd.pid" ]; then
    kill "$(cat "$LOG_DIR/ttyd.pid")" 2>/dev/null && echo "ttyd stopped" || echo "ttyd was not running"
    rm -f "$LOG_DIR/ttyd.pid"
else
    pkill -f "ttyd" 2>/dev/null && echo "ttyd stopped" || echo "ttyd was not running"
fi

# Stop voice wrapper
if [ -f "$LOG_DIR/voice-wrapper.pid" ]; then
    kill "$(cat "$LOG_DIR/voice-wrapper.pid")" 2>/dev/null && echo "voice wrapper stopped" || echo "voice wrapper was not running"
    rm -f "$LOG_DIR/voice-wrapper.pid"
else
    pkill -f "voice-wrapper" 2>/dev/null && echo "voice wrapper stopped" || echo "voice wrapper was not running"
fi

# Stop caffeinate
if [ -f "$LOG_DIR/caffeinate.pid" ]; then
    kill "$(cat "$LOG_DIR/caffeinate.pid")" 2>/dev/null && echo "caffeinate stopped" || echo "caffeinate was not running"
    rm -f "$LOG_DIR/caffeinate.pid"
else
    pkill -f "caffeinate" 2>/dev/null && echo "caffeinate stopped" || echo "caffeinate was not running"
fi

echo ""
echo "Services stopped. tmux session 'claude' is still alive."
echo "To kill it too: tmux kill-session -t claude"
