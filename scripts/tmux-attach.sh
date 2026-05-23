#!/bin/bash
# Clear Claude Code env vars so a fresh session can launch inside tmux
unset CLAUDECODE
unset CLAUDE_CODE_ENTRYPOINT
unset CLAUDE_CODE_ENTRY_VERSION
unset CLAUDE_CODE_ENV_VERSION

# UTF-8 locale for Unicode/emoji rendering
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

# Apple Silicon: /opt/homebrew/bin/tmux | Intel Mac: /usr/local/bin/tmux
TMUX_BIN=$(which tmux 2>/dev/null || echo "/opt/homebrew/bin/tmux")

# Attach-only: the "+ New Session" sidebar button is the ONLY surface that
# creates sessions. If the tmux session doesn't exist, show a friendly empty
# state and block — closing the last window won't auto-respawn a ghost
# claude.exe, and ttyd reconnects (network blip, tab foreground) attach to
# the existing session if there is one, else just re-show the empty state.
if "$TMUX_BIN" has-session -t claude 2>/dev/null; then
    exec "$TMUX_BIN" attach-session -t claude
fi

clear
cat <<'EOF'

  No active sessions.

  Open the menu (☰) on the left and tap "+ New Session" to start one.

EOF
# Block so ttyd keeps the connection open. Creating a session via the
# sidebar reloads this iframe, which kills this script.
exec sleep 86400
