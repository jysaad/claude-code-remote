#!/bin/bash
# claude-ephemeral.sh — launches Claude Code on the phone surface.
#
# The name is HISTORICAL (carried over from when phone sessions deleted their
# transcripts on exit). As of 2026-05-20 the core architectural principle is
# that Pixel + MBP + Studio are one OS: a session initiated from any surface
# gets the same treatment. Per-session behavior (auto-journal, transcript
# persistence, /chats triage, /save wind-down) is identical to a Mac session.
#
# This wrapper does TWO things:
#  1. Pass --dangerously-skip-permissions so the phone's textarea doesn't have
#     to approve every tool call (surface-level UX adaptation).
#  2. Spawn claude through disclaim-exec.py, which calls
#     responsibility_spawnattrs_setdisclaim() so claude.exe becomes its own
#     TCC responsible parent. Breaks the python3.13 -> claude-code chain
#     that produced recurring "python3.13 would like to access data from other
#     apps" popups despite a granted TCC.db Allow row. See phone-access.md
#     decisions log 2026-05-26 (original theory) + 2026-05-27 (correction +
#     disclaim-exec fix).

# Marker for ~/.claude/statusline.sh to skip the in-terminal bar on phone
# sessions — the voice-wrapper renders a sidecar HTML bar instead.
export CLAUDE_REMOTE_PHONE=1

# Resolve through symlink (this file lives in remote-cli/ but is invoked via
# ~/.local/bin/claude-ephemeral). readlink -f follows the symlink chain.
SCRIPT_REAL="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_REAL")" && pwd)"
DISCLAIM_PY="$SCRIPT_DIR/disclaim-exec.py"
PSF_PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
[ -x "$PSF_PYTHON" ] || PSF_PYTHON="$(command -v python3)"

exec "$PSF_PYTHON" "$DISCLAIM_PY" claude --dangerously-skip-permissions "$@"
