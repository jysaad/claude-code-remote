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

# Default the first window to claude-ephemeral so a cold attach lands in CC,
# matching the "+ New session" button behavior. The command is only honored on
# session creation; subsequent attaches go to whatever's already running.
CLAUDE_EPHEMERAL="$HOME/.local/bin/claude-ephemeral"
exec "$TMUX_BIN" new-session -A -s claude -c "$HOME" "$CLAUDE_EPHEMERAL"
