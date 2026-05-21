#!/bin/bash
# claude-ephemeral.sh — launches Claude Code on the phone surface.
#
# The name is HISTORICAL (carried over from when phone sessions deleted their
# transcripts on exit). As of 2026-05-20 the core architectural principle is
# that Pixel + MBP + Studio are one OS: a session initiated from any surface
# gets the same treatment. This wrapper now does ONE thing — pass
# `--dangerously-skip-permissions` so the phone's textarea doesn't have to
# approve every tool call (surface-level UX adaptation, not a session-
# behavior difference). Per-session behavior (auto-journal, transcript
# persistence, /chats triage, /save wind-down) is identical to a Mac session.
#
# Kept the file name for stability; renaming touches voice-wrapper.py, the
# ~/.local/bin/claude-ephemeral symlink, and every doc reference.

# Marker for ~/.claude/statusline.sh to skip the in-terminal bar on phone
# sessions — the voice-wrapper renders a sidecar HTML bar instead.
export CLAUDE_REMOTE_PHONE=1

exec claude --dangerously-skip-permissions "$@"
