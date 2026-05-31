#!/usr/bin/env python3
"""Voice dictation wrapper for ttyd terminal on iPhone.

Serves a page with the ttyd terminal in an iframe and a native text input
field at the bottom. Dictation works in the native input, then text is
injected into the tmux session via `tmux send-keys`.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
import subprocess
import shutil
import time

from pathlib import Path

from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
TAILSCALE = shutil.which("tailscale") or "/usr/local/bin/tailscale"
TTYD_PORT = 7681
WRAPPER_PORT = 8080
TMUX_SESSION = "claude"
CLAUDE_EPHEMERAL = str(Path.home() / ".local" / "bin" / "claude-ephemeral")
SESSION_DOING_SCRIPT = Path.home() / "Context" / "scripts" / "session-doing-summarize.sh"
AUTO_RENAME_INTERVAL_SEC = 60
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
OVERRIDES_FILE = Path.home() / ".claude" / "session-overrides.json"
STATUSLINE_SH = str(Path.home() / ".claude" / "statusline.sh")
SESSION_STATE_DIR = Path("/tmp/.remote-cli-session-state")
SESSION_STATE_DIR.mkdir(exist_ok=True)

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_BASE_COLORS = {
    30: "#000000", 31: "#cc0000", 32: "#4e9a06", 33: "#c4a000",
    34: "#3465a4", 35: "#75507b", 36: "#06989a", 37: "#d3d7cf",
    90: "#888a85", 91: "#ef2929", 92: "#8ae234", 93: "#fce94f",
    94: "#729fcf", 95: "#ad7fa8", 96: "#34e2e2", 97: "#eeeeec",
}


def ansi_to_html(text: str) -> str:
    """Convert ANSI escape sequences in `text` to HTML <span>s with inline styles."""
    out = []
    pos = 0
    style = {"color": None, "bold": False, "dim": False, "underline": False}

    def has_style() -> bool:
        return bool(style["color"] or style["bold"] or style["dim"] or style["underline"])

    def open_span() -> str:
        parts = []
        if style["color"]:
            parts.append(f"color:{style['color']}")
        if style["bold"]:
            parts.append("font-weight:600")
        if style["dim"]:
            parts.append("opacity:0.55")
        if style["underline"]:
            parts.append("text-decoration:underline")
        return f'<span style="{";".join(parts)}">' if parts else ""

    def emit(segment: str):
        if not segment:
            return
        escaped = html_lib.escape(segment).replace("\n", "<br>")
        if has_style():
            out.append(open_span())
            out.append(escaped)
            out.append("</span>")
        else:
            out.append(escaped)

    for m in ANSI_RE.finditer(text):
        emit(text[pos:m.start()])
        pos = m.end()

        codes_str = m.group(1)
        codes = [int(c) for c in codes_str.split(";") if c] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                style = {"color": None, "bold": False, "dim": False, "underline": False}
            elif c == 1:
                style["bold"] = True
            elif c == 2:
                style["dim"] = True
            elif c == 4:
                style["underline"] = True
            elif c == 22:
                style["bold"] = False
                style["dim"] = False
            elif c == 24:
                style["underline"] = False
            elif c == 39:
                style["color"] = None
            elif c in ANSI_BASE_COLORS:
                style["color"] = ANSI_BASE_COLORS[c]
            elif c == 38 and i + 1 < len(codes):
                kind = codes[i + 1]
                if kind == 2 and i + 4 < len(codes):
                    r, g, b = codes[i + 2], codes[i + 3], codes[i + 4]
                    style["color"] = f"rgb({r},{g},{b})"
                    i += 4
                elif kind == 5 and i + 2 < len(codes):
                    i += 2
            i += 1

    emit(text[pos:])
    return "".join(out)


def render_statusline_html() -> str:
    """Run ~/.claude/statusline.sh with synthesized stdin, return ANSI->HTML output."""
    # Omit session_id: the sidecar isn't a real CC session, and supplying a
    # synthetic one ("phone-sidecar") makes statusline.sh render a bogus
    # underlined "Anon" entry in the Sessions row. Empty self_id skips the
    # name_part composition cleanly.
    model_name, effort_level = active_model_and_effort()
    payload = json.dumps({
        "session_id": "",
        "version": "phone",
        "model": {"display_name": model_name or "Phone"},
        "effort": {"level": effort_level},
        "cwd": str(Path.home()),
    })
    try:
        # Drop CLAUDE_REMOTE_PHONE for this subprocess: statusline.sh early-exits
        # when that var is set (suppresses the in-terminal bar on phone sessions),
        # but the sidecar IS the phone bar — it must render fully.
        # Drop TMUX so statusline.sh doesn't think it's running inside the "claude"
        # tmux session and enter pane-width compact mode (which clips per-line at
        # tmux pane_width and ellipsizes the Sessions row mid-list).
        child_env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_REMOTE_PHONE", "TMUX")}
        # Always show the counts row on the phone bar — bypass statusline.sh's
        # calm-by-default time gate (the gate suits the Mac terminal, not the
        # dedicated phone sidecar where Sms/Email/Gtasks are the point).
        child_env["STATUSLINE_FORCE_COUNTS"] = "1"
        result = subprocess.run(
            ["/bin/bash", STATUSLINE_SH],
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
            env=child_env,
        )
        # Collapse runs of newlines (statusline.sh double-spaces rows) → single break
        # and strip trailing whitespace so the bar doesn't grow an empty last line.
        out = re.sub(r"\n+", "\n", result.stdout).strip()
        return ansi_to_html(out)
    except Exception as e:
        return f'<span style="color:#888">statusbar error: {html_lib.escape(str(e))}</span>'


def sanitize_window_name(name: str) -> str:
    name = re.sub(r"[^\w\s\-\.]", "", name or "").strip()
    return name[:32] or "session"


def find_new_session(before_pids: set, timeout: float = 8.0):
    """Poll ~/.claude/sessions/*.json for a new claude session pid; return its sessionId."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for fp in SESSIONS_DIR.glob("*.json"):
            if fp.stem in before_pids:
                continue
            try:
                data = json.loads(fp.read_text())
                if data.get("sessionId"):
                    return data["sessionId"]
            except (json.JSONDecodeError, FileNotFoundError, OSError):
                continue
        time.sleep(0.3)
    return None


def set_session_label(session_id: str, label: str, auto: bool = False):
    """Write {sessionId: {name: label, auto: bool}} into session-overrides.json.
    auto=True marks the label as replaceable by the doing-loop (a session got
    its initial name from `/tmux/new` and hasn't been manually renamed since).
    auto=False (default) means the label is sticky — user-set, leave it alone."""
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text())
        if not isinstance(overrides, dict):
            overrides = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        overrides = {}
    entry = overrides.setdefault(session_id, {})
    entry["name"] = label
    entry["auto"] = auto
    OVERRIDES_FILE.write_text(json.dumps(overrides, indent=2))


def label_new_session(before_pids: set, label: str):
    sid = find_new_session(before_pids)
    if sid:
        set_session_label(sid, label, auto=True)


def find_cc_session_for_window(window_name: str):
    """Return (sessionId, status) for the CC session whose override-name
    matches window_name. The override is set by label_new_session() right
    after the wrapper creates the tmux window, so any session started via
    the drawer has a match. Returns (None, None) if no match."""
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text())
        if not isinstance(overrides, dict):
            return None, None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None
    target_sid = None
    for sid, attrs in overrides.items():
        if isinstance(attrs, dict) and attrs.get("name") == window_name:
            target_sid = sid
            break
    if not target_sid:
        return None, None
    for fp in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text())
            if data.get("sessionId") == target_sid:
                return target_sid, data.get("status") or "idle"
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
    return target_sid, None


def check_session_ready(sid: str, curr_status: str) -> bool:
    """Track per-session busy↔idle transitions so the drawer can surface
    "this session just finished thinking and is waiting for you" with a
    red dot. State files in SESSION_STATE_DIR:
      • prev-<sid>  — last-seen status, updated on every observation.
      • ready-<sid> — touch file iff the most recent transition was
        busy→idle and no busy observation has happened since.
    curr=busy clears the ready flag. curr=idle with prev=busy sets it.
    Returns True if the ready flag is currently set."""
    if not sid:
        return False
    prev_file = SESSION_STATE_DIR / f"prev-{sid}"
    ready_file = SESSION_STATE_DIR / f"ready-{sid}"
    prev = ""
    try:
        if prev_file.exists():
            prev = prev_file.read_text().strip()
    except OSError:
        pass
    if curr_status == "busy":
        try:
            ready_file.unlink()
        except FileNotFoundError:
            pass
    elif curr_status == "idle" and prev == "busy":
        ready_file.touch()
    try:
        prev_file.write_text(curr_status)
    except OSError:
        pass
    return ready_file.exists()


def _pid_tty_map() -> dict:
    """Map pid -> controlling tty basename (e.g. 'ttys059') via one ps call.
    Lets us bind a tmux window to the CC session running in it by TTY, which
    is stable across window renames and 1:1 with the pane — unlike the
    name-based override lookup."""
    out = {}
    try:
        res = subprocess.run(
            ["/bin/ps", "-eo", "pid=,tty="],
            capture_output=True, text=True, timeout=5,
        )
        for ln in res.stdout.splitlines():
            parts = ln.split(None, 1)
            if len(parts) == 2:
                out[parts[0].strip()] = parts[1].strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return out


def find_cc_session_for_tty(pane_tty: str, pid_tty: dict | None = None):
    """Return (sessionId, status) for the CC session whose process runs on
    pane_tty. Resolves window->session by controlling TTY instead of the
    mutable, non-unique window name.

    Why this exists: the name-based find_cc_session_for_window() silently
    returns the wrong session in two common states, both of which default
    status to 'idle' -> GREEN dot while the session is actually thinking:
      1. Window renamed (auto-rename loop or manual) — the override 'name'
         drifts from the live tmux window name, so the lookup misses and
         returns (None, None).
      2. Two sessions ever shared a name (e.g. 'statusbar' reused across
         sessions) — first-match-wins in dict order picks the OLDEST entry,
         usually a dead session with no peer file -> status None -> idle.
    A pane's #{pane_tty} ('/dev/ttysNNN') and the claude process's tty
    ('ttysNNN', inherited through disclaim-exec) are a reliable 1:1 key.
    Returns (None, None) if no live CC session is on that tty."""
    if not pane_tty:
        return None, None
    want = pane_tty.rsplit("/", 1)[-1]  # /dev/ttys059 -> ttys059
    if pid_tty is None:
        pid_tty = _pid_tty_map()
    for fp in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text())
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        pid = str(data.get("pid") or "")
        if pid and pid_tty.get(pid) == want:
            return data.get("sessionId"), data.get("status") or "idle"
    return None, None


def find_transcript_path(sid: str):
    """Find the .jsonl transcript for a Claude Code session id by globbing
    ~/.claude/projects/*/<sid>.jsonl. Returns None if not found."""
    if not sid:
        return None
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return None
    matches = list(projects_root.glob(f"*/{sid}.jsonl"))
    return matches[0] if matches else None


def find_cwd_for_session(sid: str):
    """Return cwd for a CC session by sid, or None if not found."""
    if not sid:
        return None
    for fp in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text())
            if data.get("sessionId") == sid:
                return data.get("cwd")
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
    return None


# Map model IDs (as recorded in transcripts) → the compact display names
# statusline.sh renders. Fallback chain: exact id → prettified id → "" (caller
# substitutes "Phone").
MODEL_DISPLAY_NAMES = {
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-haiku-4-5": "Haiku 4.5",
}


# Effort-level display relabels for the phone sidebar (CC's .effort.level →
# the label John selected). "xhigh" is what CC reports when ultracode is on.
EFFORT_DISPLAY_NAMES = {
    "xhigh": "ultracode",
}


def _model_display_from_id(model_id: str) -> str:
    if not model_id:
        return ""
    if model_id in MODEL_DISPLAY_NAMES:
        return MODEL_DISPLAY_NAMES[model_id]
    m = re.match(r"claude-(opus|sonnet|haiku)-(\d+)-(\d+)", model_id)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}"
    return model_id


def _active_window_sid():
    """sessionId of the currently-active phone tmux window, or None."""
    try:
        r = subprocess.run(
            [TMUX, "list-windows", "-t", TMUX_SESSION,
             "-F", "#{window_name}|#{window_active}"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            nm, _, act = line.rpartition("|")
            if act == "1":
                sid, _ = find_cc_session_for_window(nm)
                return sid
    except Exception:
        pass
    return None


def _model_from_transcript(sid: str) -> str:
    """Fallback model display name from the transcript tail when the phone
    cache is absent. Effort is not recoverable this way (transcripts omit it)."""
    tpath = find_transcript_path(sid)
    if not tpath:
        return ""
    try:
        with open(tpath, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "ignore")
        ids = re.findall(r'"model":"(claude-[^"]+)"', tail)
        return _model_display_from_id(ids[-1]) if ids else ""
    except OSError:
        return ""


def active_model_and_effort():
    """(model_display, effort_level) for the active phone session.

    Primary source: /tmp/.statusline-phone-<sid>, written by statusline.sh on
    the REAL session's render — authoritative, exactly what CC reports (model
    already stripped of its " (1M context)" suffix; tab-separated model\\teffort).
    Falls back to the transcript for the model id when the cache is absent;
    effort has no fallback. Returns ("", "") when nothing resolves so the caller
    substitutes "Phone" for the model."""
    sid = _active_window_sid()
    if not sid:
        return "", ""
    model, effort = "", ""
    cache = Path(f"/tmp/.statusline-phone-{sid}")
    try:
        if cache.exists():
            parts = cache.read_text().split("\t")
            model = parts[0].strip() if parts else ""
            effort = parts[1].strip() if len(parts) > 1 else ""
    except OSError:
        pass
    if not model:
        model = _model_from_transcript(sid)
    # Display relabel: CC reports ultracode's reasoning effort as "xhigh"; show
    # the mode name John selected instead. Phone-display only — the cache keeps
    # the authoritative value.
    effort = EFFORT_DISPLAY_NAMES.get(effort, effort)
    return model, effort


def wrap_up_closed_session(transcript_path: Path, sid: str):
    """Phone close = explicit wrap-up. Pipe the transcript into
    ~/Context/scripts/session-end-journal.sh, which summarizes into today's
    vault journal and then deletes the .jsonl via its cleanup_on_exit trap.
    Also clean up our own per-session state files and the override entry.
    Silent on failure — never let cleanup break a close."""
    try:
        time.sleep(0.5)  # let claude flush the final bytes after SIGHUP
        script = Path.home() / "Context" / "scripts" / "session-end-journal.sh"
        if script.exists() and transcript_path.exists():
            payload = json.dumps({
                "transcript_path": str(transcript_path),
                "source": "phone-close",
            })
            subprocess.run(
                [str(script)],
                input=payload, text=True, timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (subprocess.SubprocessError, OSError):
        pass
    for fname in (f"prev-{sid}", f"ready-{sid}"):
        try:
            (SESSION_STATE_DIR / fname).unlink()
        except FileNotFoundError:
            pass
    try:
        if OVERRIDES_FILE.exists():
            data = json.loads(OVERRIDES_FILE.read_text())
            if isinstance(data, dict) and sid in data:
                data.pop(sid)
                OVERRIDES_FILE.write_text(json.dumps(data, indent=2))
    except (json.JSONDecodeError, OSError):
        pass


app = FastAPI()


def get_tailscale_ip():
    result = subprocess.run(
        [TAILSCALE, "ip", "-4"], capture_output=True, text=True
    )
    return result.stdout.strip()


class TextInput(BaseModel):
    text: str


class KeyInput(BaseModel):
    key: str


class NewWindow(BaseModel):
    name: str = "session"


class WindowIndex(BaseModel):
    index: int


class WindowRename(BaseModel):
    index: int
    name: str


@app.get("/", response_class=HTMLResponse)
async def index():
    ip = get_tailscale_ip()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">
    <title>Claude Code Remote</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            height: 100%;
            background: #1a1a1a;
            overflow: hidden;
            font-family: -apple-system, system-ui, sans-serif;
            touch-action: manipulation;
        }}
        .container {{
            position: relative;
            width: 100%;
            /* 100lvh = large viewport, does NOT shrink when keyboard opens.
               Critical for keeping the iframe size stable so xterm.js
               doesn't re-fit on every textarea tap. 100vh fallback for
               browsers without large-viewport-unit support (pre-2022). */
            height: 100vh;
            height: 100lvh;
        }}
        .terminal-wrap {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 20vh;
            min-height: 0;
            /* Iframe shifts up by --keyboard-height so its bottom rows stay
               visible above the keyboard. translate3d keeps it on the
               compositor; contain isolates paint. Iframe SIZE doesn't change
               — xterm.js never re-fits. setupViewport snaps --keyboard-height
               (no JS animation) so this transform jumps in one frame when
               the keyboard event fires — see .bottom-panel rule for the
               "why no transition" rationale (avoids bounce on multi-event
               Android slides). */
            transform: translate3d(0, calc(-1 * var(--keyboard-height, 0px)), 0);
            contain: layout paint;
        }}
        .bottom-panel {{
            position: fixed;
            left: 0;
            right: 0;
            bottom: var(--keyboard-height, 0px);
            background: #1a1a1a;
            z-index: 20;
            /* No CSS transition. setupViewport snaps --keyboard-height
               directly on visualViewport.resize (no interpolation), so any
               transition here would cause a "bounce" when a second event
               fires with a smaller height (e.g., Gboard suggestion bar
               settling). The panel jumps in one frame to whatever Android
               last reported. */
        }}
        .terminal-frame {{
            border: none;
            width: 100%;
            height: 100%;
            display: block;
        }}
        .touch-scroll-overlay {{
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: transparent;
            touch-action: none;
            z-index: 5;
        }}
        /* Reconnecting overlay — covers the iframe while it reloads so the
           user never sees ttyd's 'Press ⏎ to Reconnect' or a blank flash. */
        .iframe-reloading {{
            position: absolute;
            inset: 0;
            background: #1a1a1a;
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 7;
            color: #888;
            font: 14px Menlo, monospace;
        }}
        .iframe-reloading.visible {{ display: flex; }}
        .iframe-reloading::before {{
            content: 'reconnecting…';
            padding: 14px 22px;
            background: #252525;
            border-radius: 8px;
            border: 1px solid #333;
        }}
        .scroll-hint {{
            position: absolute;
            top: 8px;
            right: 8px;
            padding: 4px 8px;
            background: rgba(0, 122, 255, 0.85);
            color: #fff;
            font: 11px -apple-system, system-ui, sans-serif;
            border-radius: 4px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.15s;
            z-index: 6;
        }}
        .scroll-hint.visible {{ opacity: 1; }}
        .status-bar {{
            padding: 4px 8px;
            background: #1c1c1c;
            color: #d0d0d0;
            font-family: 'Menlo', monospace;
            font-size: 11px;
            line-height: 1.4;
            border-top: 1px solid #2a2a2a;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 64px;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .status-bar:empty {{ display: none; }}
        /* Status-bar display modes (Settings sheet: Full / 1-line / Off). The
           bar is one innerHTML blob with \n-separated rows under white-space:
           pre-wrap, so "1-line" is a single-visual-line clamp and "Off" hides
           it outright. Set via JS classList on #statusBar; persisted in
           localStorage. */
        .status-bar.compact {{
            display: -webkit-box;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 1;
            overflow: hidden;
            max-height: 1.6em;
        }}
        .status-bar.off {{ display: none; }}
        .quick-keys {{
            display: flex;
            gap: 4px;
            padding: 3px 6px;
            background: #252525;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .quick-keys button {{
            padding: 6px 12px;
            font-size: 13px;
            font-family: 'Menlo', monospace;
            border: 1px solid #555;
            border-radius: 4px;
            background: #333;
            color: #ccc;
            cursor: pointer;
            white-space: nowrap;
            flex-shrink: 0;
        }}
        .quick-keys button:active {{
            background: #555;
        }}
        .input-bar {{
            display: flex;
            gap: 6px;
            padding: 4px 6px;
            background: #2d2d2d;
            border-top: 1px solid #444;
        }}
        .input-bar textarea {{
            flex: 1;
            padding: 8px 10px;
            font-size: 16px;
            border: 1px solid #555;
            border-radius: 8px;
            background: #1a1a1a;
            color: #fff;
            outline: none;
            resize: none;
            overflow-y: hidden;
            min-height: 38px;
            max-height: 80px;
            line-height: 1.4;
            font-family: -apple-system, system-ui, sans-serif;
        }}
        .input-bar textarea:focus {{
            border-color: #007aff;
        }}
        .input-bar button {{
            padding: 10px 18px;
            font-size: 16px;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            background: #007aff;
            color: #fff;
            cursor: pointer;
            white-space: nowrap;
        }}
        .input-bar button:active {{
            background: #005bb5;
        }}
        .copy-overlay {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.85);
            z-index: 100;
            flex-direction: column;
            padding: 12px;
        }}
        .copy-overlay.active {{
            display: flex;
        }}
        .copy-overlay .copy-content {{
            flex: 1;
            background: #1a1a1a;
            color: #e0e0e0;
            border: 1px solid #555;
            border-radius: 8px;
            padding: 12px;
            font-family: Menlo, monospace;
            font-size: 14px;
            line-height: 1.4;
            margin: 0;
            overflow-y: auto;
            overflow-x: hidden;
            white-space: pre-wrap;
            word-break: break-word;
            -webkit-user-select: text;
            user-select: text;
            -webkit-overflow-scrolling: touch;
        }}
        .copy-hint {{
            color: #888;
            font-size: 13px;
            text-align: center;
            padding: 6px;
        }}
        .copy-overlay .close-btn {{
            margin-top: 8px;
            padding: 12px;
            font-size: 16px;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            background: #555;
            color: #fff;
            cursor: pointer;
        }}
        .input-bar .menu-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 44px;
            min-height: 42px;
            padding: 0;
            background: #333;
            color: #ddd;
            border: 1px solid #555;
            border-radius: 8px;
            font-weight: normal;
            cursor: pointer;
            flex-shrink: 0;
        }}
        .input-bar .menu-btn:active {{ background: #555; }}
        .input-bar .menu-btn .icon {{ font-size: 20px; line-height: 1; }}
        .input-bar .menu-btn .active-name {{ display: none; }}
        .drawer-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.5);
            z-index: 150;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.18s ease;
        }}
        .drawer-backdrop.active {{
            opacity: 1;
            pointer-events: auto;
        }}
        .drawer {{
            position: fixed;
            top: 0;
            left: 0;
            width: 82%;
            max-width: 320px;
            height: 100%;
            height: 100dvh;
            background: #1f1f1f;
            border-right: 1px solid #444;
            z-index: 200;
            transform: translateX(-100%);
            transition: transform 0.2s ease;
            display: flex;
            flex-direction: column;
        }}
        .drawer.open {{ transform: translateX(0); }}
        .drawer-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            border-bottom: 1px solid #333;
            color: #fff;
            font-weight: 600;
            font-size: 15px;
        }}
        .drawer-header .close-x {{
            background: none;
            border: none;
            color: #888;
            font-size: 24px;
            line-height: 1;
            cursor: pointer;
            padding: 0 4px;
        }}
        .drawer-hint {{
            font-weight: normal;
            font-size: 11px;
            color: #777;
            margin-left: 8px;
        }}
        .new-session-btn {{
            margin: 12px 16px 6px;
            padding: 12px;
            background: #007aff;
            color: #fff;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
        }}
        .new-session-btn:active {{ background: #005bb5; }}
        .new-terminal-btn {{
            margin: 6px 16px 12px;
            padding: 12px;
            background: #2d2d2d;
            color: #cfcfcf;
            border: 1px solid #444;
            border-radius: 8px;
            font-weight: 600;
            font-size: 14px;
            font-family: Menlo, monospace;
            cursor: pointer;
        }}
        .new-terminal-btn:active {{ background: #1a1a1a; }}
        .drawer-settings-btn {{
            margin: 0 16px 12px;
            padding: 12px;
            background: #2d2d2d;
            color: #cfcfcf;
            border: 1px solid #444;
            border-radius: 8px;
            font-weight: 600;
            font-size: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            cursor: pointer;
        }}
        .drawer-settings-btn:active {{ background: #1a1a1a; }}
        .drawer-settings-btn .gear {{ font-size: 15px; }}
        .session-list {{
            flex: 1;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .session-list .empty {{
            text-align: center;
            color: #666;
            padding: 24px;
            font-size: 13px;
        }}
        .session-row {{
            display: block;
            padding: 12px 16px;
            border-bottom: 1px solid #2a2a2a;
            color: #bbb;
            cursor: pointer;
        }}
        .session-row .row-main {{
            display: flex;
            align-items: center;
        }}
        .session-row.active {{
            background: #143a5e;
            color: #fff;
        }}
        .session-row .name {{
            flex: 1;
            font-size: 14px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            padding-right: 8px;
        }}
        .session-row .row-close {{
            background: none;
            border: none;
            color: #888;
            font-size: 18px;
            padding: 6px 10px;
            margin: -6px -8px -6px 0;
            cursor: pointer;
        }}
        .session-row .row-close:active {{ color: #f55; }}
        .session-row .ready-dot,
        .menu-btn .ready-dot {{
            color: #34c759;
            font-size: 12px;
            margin-right: 10px;
            line-height: 1;
        }}
        .session-row .thinking-dot,
        .menu-btn .thinking-dot {{
            color: #ffb84d;
            font-size: 12px;
            margin-right: 10px;
            line-height: 1;
            animation: dot-blink 1s ease-in-out infinite;
        }}
        .menu-btn .thinking-dot,
        .menu-btn .ready-dot {{
            margin-right: 6px;
            font-size: 11px;
        }}
        @keyframes dot-blink {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.2; }}
        }}
        /* --- Toast (send failures, etc.) --- */
        .toast {{
            position: fixed;
            bottom: calc(var(--bottom-panel-height, 110px) + var(--keyboard-height, 0px) + 12px);
            left: 50%;
            transform: translateX(-50%) translateY(20px);
            background: #c0392b;
            color: #fff;
            padding: 10px 16px;
            border-radius: 8px;
            font-size: 14px;
            opacity: 0;
            transition: opacity 0.2s, transform 0.2s, bottom 0.15s;
            pointer-events: none;
            z-index: 300;
            max-width: 90%;
            text-align: center;
        }}
        .toast.visible {{
            opacity: 1;
            transform: translateX(-50%) translateY(0);
            pointer-events: auto;
        }}
        /* --- Send button states --- */
        .input-bar button.sending {{
            opacity: 0.6;
            cursor: progress;
        }}
        /* --- Skills bottom sheet --- */
        .skills-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.5);
            z-index: 250;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.18s ease;
        }}
        .skills-backdrop.active {{
            opacity: 1;
            pointer-events: auto;
        }}
        .skills-sheet {{
            position: fixed;
            left: 0;
            right: 0;
            bottom: var(--keyboard-height, 0px);
            max-height: 70vh;
            background: #1f1f1f;
            border-top: 1px solid #444;
            border-radius: 12px 12px 0 0;
            z-index: 300;
            transform: translateY(110%);
            display: flex;
            flex-direction: column;
            pointer-events: none;
            /* visibility: hidden while closed = zero paint, no flash during
               the keyboard slide. The 0.22s linear delay on visibility means
               the sheet only goes hidden AFTER the slide-down animation
               finishes (on close). On open, the override on .open sets the
               delay to 0s so visibility flips visible immediately. The
               removed `transition: bottom` (and bound-to-keyboard-height
               translateY's interaction with the rAF loop on --keyboard-height)
               was painting a sub-pixel sliver of the sheet during the
               keyboard slide, brief but visible — John reported it flashing
               on textbox focus 2026-05-27. visibility: hidden is the robust
               fix because it removes the element from the paint pipeline
               entirely. */
            visibility: hidden;
            transition: transform 0.22s ease, visibility 0s linear 0.22s;
        }}
        .skills-sheet.open {{
            transform: translateY(0);
            pointer-events: auto;
            visibility: visible;
            transition: transform 0.22s ease, visibility 0s linear 0s;
        }}
        /* Settings sheet — same shape as skills-sheet, also bound to
           --keyboard-height with NO bottom transition (rAF-driven, see
           skills-sheet rule for the why). */
        .settings-backdrop {{
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.4);
            z-index: 290;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.18s ease;
        }}
        .settings-backdrop.active {{
            opacity: 1;
            pointer-events: auto;
        }}
        .settings-sheet {{
            position: fixed;
            left: 0;
            right: 0;
            bottom: var(--keyboard-height, 0px);
            max-height: 70vh;
            background: #1f1f1f;
            border-top: 1px solid #444;
            border-radius: 12px 12px 0 0;
            z-index: 300;
            transform: translateY(110%);
            display: flex;
            flex-direction: column;
            pointer-events: none;
            /* Same visibility trick as .skills-sheet — see comment there. */
            visibility: hidden;
            transition: transform 0.22s ease, visibility 0s linear 0.22s;
        }}
        .settings-sheet.open {{
            transform: translateY(0);
            pointer-events: auto;
            visibility: visible;
            transition: transform 0.22s ease, visibility 0s linear 0s;
        }}
        .settings-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            border-bottom: 1px solid #333;
            color: #fff;
            font-weight: 600;
            font-size: 15px;
        }}
        .settings-header .close-x {{
            background: none;
            border: none;
            color: #888;
            font-size: 24px;
            line-height: 1;
            cursor: pointer;
            padding: 0 4px;
        }}
        .settings-body {{
            padding: 18px 20px 28px;
            overflow-y: auto;
        }}
        .settings-row {{ margin-bottom: 24px; }}
        .settings-row:last-child {{ margin-bottom: 0; }}
        .settings-label {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 10px;
        }}
        .settings-label .label-text {{
            font-weight: 600;
            color: #fff;
            font-size: 14px;
        }}
        .settings-label .label-value {{
            color: #aaa;
            font-size: 12px;
            font-variant-numeric: tabular-nums;
        }}
        .settings-slider {{
            width: 100%;
            accent-color: #007aff;
            margin: 0;
            height: 28px;
        }}
        .settings-ends {{
            display: flex;
            justify-content: space-between;
            margin-top: 4px;
            color: #777;
            font-size: 11px;
        }}
        .settings-hint {{
            margin-top: 12px;
            color: #888;
            font-size: 12px;
            line-height: 1.4;
        }}
        .settings-seg {{
            display: flex;
            gap: 6px;
        }}
        .settings-seg button {{
            flex: 1;
            padding: 9px 0;
            font-size: 13px;
            font-family: -apple-system, system-ui, sans-serif;
            border: 1px solid #555;
            border-radius: 8px;
            background: #2a2a2a;
            color: #bbb;
            cursor: pointer;
        }}
        .settings-seg button.active {{
            background: #007aff;
            border-color: #007aff;
            color: #fff;
            font-weight: 600;
        }}
        .skills-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 16px;
            border-bottom: 1px solid #333;
            color: #fff;
            font-weight: 600;
            font-size: 15px;
        }}
        .skills-header .close-x {{
            background: none;
            border: none;
            color: #888;
            font-size: 24px;
            line-height: 1;
            cursor: pointer;
            padding: 0 4px;
        }}
        .skills-list {{
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
            padding: 4px 0;
        }}
        .skills-row {{
            padding: 12px 16px;
            color: #ccc;
            font-family: Menlo, monospace;
            font-size: 14px;
            cursor: pointer;
            border-bottom: 1px solid #2a2a2a;
        }}
        .skills-row:active {{ background: #2a2a2a; color: #fff; }}
        .skills-row:last-child {{ border-bottom: none; }}
        .skills-empty {{
            padding: 24px;
            color: #666;
            text-align: center;
            font-size: 13px;
        }}
        /* --- Pull-to-refresh indicator --- */
        .pull-indicator {{
            position: fixed;
            top: 8px;
            left: 50%;
            transform: translateX(-50%);
            padding: 6px 14px;
            background: rgba(0, 122, 255, 0.85);
            color: #fff;
            font: 12px -apple-system, system-ui, sans-serif;
            border-radius: 14px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.15s;
            z-index: 400;
        }}
        .pull-indicator.visible {{ opacity: 1; }}
    </style>
</head>
<body>
    <div class="drawer-backdrop" id="drawerBackdrop" onclick="closeDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <span>Sessions <span class="drawer-hint">hold to rename</span></span>
            <button class="close-x" onclick="closeDrawer()">&times;</button>
        </div>
        <button class="new-session-btn" onclick="promptNewSession()">+ New session</button>
        <div class="session-list" id="sessionList">
            <div class="empty">Loading…</div>
        </div>
        <button class="new-terminal-btn" onclick="promptNewTerminal()">&gt;_ New terminal</button>
        <button class="drawer-settings-btn" onclick="openSettingsSheet()">
            <span class="gear">&#9881;</span>
            <span>Settings</span>
        </button>
    </div>
    <div class="container">
        <div class="terminal-wrap">
            <iframe class="terminal-frame" src="about:blank" tabindex="-1"></iframe>
            <div class="iframe-reloading" id="iframeReloading"></div>
            <div class="touch-scroll-overlay" id="scrollOverlay"></div>
            <div class="scroll-hint" id="scrollHint">scrolling</div>
        </div>
        <div class="bottom-panel" id="bottomPanel">
            <div class="status-bar" id="statusBar"></div>
            <div class="quick-keys">
                <button onclick="sendKey('Escape')">Esc</button>
                <button id="scrollBtn" title="Unlock swipe-scroll (no jump; never interrupts Claude)">&#9195;</button>
                <button id="slashBtn" title="Tap: /  ·  Long-press: all skills">/</button>
                <button onclick="sendKey('Up')">&#9650;</button>
                <button onclick="sendKey('Down')">&#9660;</button>
                <button onclick="sendKey('Right')">&#9654;</button>
                <button onclick="sendKey('Tab')">Tab</button>
                <button onclick="sendKey('C-u')">Del</button>
                <button onclick="copyPane()" title="Copy pane text">&#128203;</button>
                <button onclick="document.getElementById('galleryInput').click()">&#128444;&#65039;</button>
                <input type="file" id="galleryInput" accept="image/*" multiple style="display:none"
                       onchange="uploadPhoto(this)">
            </div>
            <div class="input-bar">
                <button class="menu-btn" onclick="openDrawer()" aria-label="Menu">
                    <span class="icon">&#9776;</span>
                    <span class="thinking-dot" id="activeThinking" style="display:none">&#9679;</span>
                    <span class="ready-dot" id="activeReady" style="display:none">&#9679;</span>
                    <span class="active-name" id="activeName">…</span>
                </button>
                <textarea id="cmd" rows="1"
                          placeholder="Dictate or type here..."
                          autocomplete="off"
                          autocorrect="off"
                          autocapitalize="off"
                          enterkeyhint="send"></textarea>
                <button id="sendBtn" onclick="sendText()">Send</button>
            </div>
        </div>
    </div>
    <div class="toast" id="toast"></div>
    <div class="pull-indicator" id="pullIndicator">pull to refresh</div>
    <div class="skills-backdrop" id="skillsBackdrop" onclick="closeSkillsSheet()"></div>
    <div class="skills-sheet" id="skillsSheet">
        <div class="skills-header">
            <span>Skills</span>
            <button class="close-x" onclick="closeSkillsSheet()">&times;</button>
        </div>
        <div class="skills-list" id="skillsList">
            <div class="skills-empty">Loading…</div>
        </div>
    </div>
    <div class="settings-backdrop" id="settingsBackdrop" onclick="closeSettingsSheet()"></div>
    <div class="settings-sheet" id="settingsSheet">
        <div class="settings-header">
            <span>Settings</span>
            <button class="close-x" onclick="closeSettingsSheet()">&times;</button>
        </div>
        <div class="settings-body">
            <div class="settings-row">
                <div class="settings-label">
                    <span class="label-text">Status bar</span>
                </div>
                <div class="settings-seg" id="statusSeg">
                    <button data-mode="full">Full</button>
                    <button data-mode="compact">1-line</button>
                    <button data-mode="off">Off</button>
                </div>
                <div class="settings-hint">Full shows all rows. 1-line collapses to a single summary line. Off hides the bar entirely.</div>
            </div>
            <div class="settings-row">
                <div class="settings-label">
                    <span class="label-text">Scroll sensitivity</span>
                    <span class="label-value" id="sensValue">14 px/line</span>
                </div>
                <input type="range" id="sensSlider" class="settings-slider"
                       min="5" max="28" step="1" value="14">
                <div class="settings-ends">
                    <span>Fast</span>
                    <span>Precise</span>
                </div>
                <div class="settings-hint">Controls how much finger movement is one scroll-line. Lower = a small swipe moves many lines (fast). Higher = each line needs more swipe (precise).</div>
            </div>
        </div>
    </div>
    <div class="copy-overlay" id="copyOverlay">
        <div class="copy-hint">Scroll to read · long-press to select, then Copy</div>
        <pre id="copyText" class="copy-content"></pre>
        <button class="close-btn" onclick="closeCopy()">Close</button>
    </div>
    <script>
        const input = document.getElementById('cmd');
        const UPLOAD_DIR = '/tmp/claude-uploads/';

        // Scroll sensitivity (Settings sheet). Script-scope `let` so the slider
        // can mutate it live without reloading. Read by setupScrollOverlay
        // (both touch swipe and wheel paths) via closure. Persisted in
        // localStorage; loaded on init at the bottom of the script.
        let PIXELS_PER_LINE = 14;
        const SENS_KEY = 'phoneOS.scrollSensitivity';
        const SENS_MIN = 5;
        const SENS_MAX = 28;
        const SENS_DEFAULT = 14;

        // Status bar display mode (Settings sheet): 'full' | 'compact' | 'off'.
        // Persisted in localStorage and applied to #statusBar as a CSS class.
        // Defaults to 'compact' (single line) so the phone bar stays minimized.
        const STATUS_MODE_KEY = 'phoneOS.statusBarMode';
        const STATUS_MODES = ['full', 'compact', 'off'];
        const STATUS_MODE_DEFAULT = 'compact';
        let statusBarMode = STATUS_MODE_DEFAULT;

        // Track typing activity so reconnectDebounced can suppress iframe
        // reloads while the user is composing. Reload mid-typing steals focus
        // from the textarea (xterm.js inside the iframe grabs focus on boot)
        // and corrupts Gboard's IME state — symptoms: cursor jumps to the
        // terminal, flicker, phantom words after picking from the word chooser.
        let lastInputAt = 0;
        let isComposing = false;

        // Auto-resize textarea as content grows
        input.addEventListener('input', () => {{
            lastInputAt = Date.now();
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 100) + 'px';
        }});
        input.addEventListener('compositionstart', () => {{ isComposing = true; }});
        input.addEventListener('compositionend', () => {{
            isComposing = false;
            lastInputAt = Date.now();
        }});

        // Enter sends, Shift+Enter adds newline
        input.addEventListener('keydown', (e) => {{
            if (e.key === 'Enter' && !e.shiftKey) {{
                e.preventDefault();
                sendText();
            }}
        }});

        // Toast helper — used by sendText() on failure and anywhere else
        // we want a non-blocking error message. Stacks the message above the
        // bottom panel (and above the keyboard when up).
        let __toastTimer = null;
        function showToast(msg, durationMs) {{
            const t = document.getElementById('toast');
            if (!t) return;
            t.textContent = msg;
            t.classList.add('visible');
            clearTimeout(__toastTimer);
            __toastTimer = setTimeout(() => t.classList.remove('visible'), durationMs || 3000);
        }}

        async function sendText(override) {{
            let text = override || input.value.trim();
            if (!text) {{
                await sendKey('Enter');
                return;
            }}
            // Swap [filename] placeholders to real paths
            text = text.replace(/\\[([^\\]]+)\\]/g, (match, name) => {{
                if (name.match(/\\.(jpg|jpeg|png|gif|webp|heic)$/i)) {{
                    return UPLOAD_DIR + name;
                }}
                return match;
            }});

            const sendBtn = document.getElementById('sendBtn');
            const prevSendLabel = sendBtn ? sendBtn.textContent : 'Send';
            if (sendBtn) {{
                sendBtn.textContent = '…';
                sendBtn.classList.add('sending');
                sendBtn.disabled = true;
            }}
            try {{
                const resp = await fetch('/send', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ text }})
                }});
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                if (!override) {{
                    input.value = '';
                    input.style.height = 'auto';
                    input.focus();
                }}
            }} catch (err) {{
                console.error('Send failed:', err);
                showToast('send failed — text kept, tap Send to retry');
                // Deliberately do NOT clear input on failure so retry is one tap.
            }} finally {{
                if (sendBtn) {{
                    sendBtn.textContent = prevSendLabel;
                    sendBtn.classList.remove('sending');
                    sendBtn.disabled = false;
                }}
            }}
        }}

        async function sendKey(key) {{
            try {{
                await fetch('/key', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ key }})
                }});
            }} catch (err) {{
                console.error('Key send failed:', err);
            }}
        }}

        // Context-aware scroll button. State synced from /tmux/windows
        // (in_copy_mode). NEVER interrupts Claude under any state.
        //   not-in-copy-mode → ⏫  tap = enter copy mode ONLY (unlock swipe).
        //                          User then drags at their own pace.
        //   in-copy-mode     → ⏬  tap = exit copy mode (snap back to live)
        // The Esc button (separate, leftmost) keeps its own gated
        // behavior; this button is the no-mental-load scroll surface
        // for users who don't trust the Esc gating in the heat of work.
        let inCopyMode = false;
        function updateScrollBtn() {{
            const btn = document.getElementById('scrollBtn');
            if (!btn) return;
            if (inCopyMode) {{
                btn.innerHTML = '&#9196;';  // ⏬
                btn.title = 'Exit scroll mode (back to live, never interrupts Claude)';
            }} else {{
                btn.innerHTML = '&#9195;';  // ⏫
                btn.title = 'Unlock swipe-scroll (no jump; never interrupts Claude)';
            }}
        }}
        async function toggleScroll() {{
            // Optimistic: flip the local flag immediately so the emoji and
            // gesture gates update without waiting for the server round-trip.
            // refreshSessions (scheduled below) will reconcile if the server
            // disagrees for any reason.
            const wasEnabled = inCopyMode;
            inCopyMode = !wasEnabled;
            updateScrollBtn();
            if (wasEnabled) {{
                try {{ await fetch('/scroll/exit', {{ method: 'POST' }}); }}
                catch (err) {{ /* silent */ }}
            }} else {{
                // /scroll/enter just puts tmux in copy-mode (no page-up).
                // The swipe-scroll path at the overlay touchmove handler is
                // gated on inCopyMode and is now the only way to actually
                // move the buffer — finger control, no surprise jumps.
                try {{ await fetch('/scroll/enter', {{ method: 'POST' }}); }}
                catch (err) {{ /* silent */ }}
            }}
            // No input.focus() — the scroll button is a "look at terminal"
            // gesture, not typing. Don't pop the keyboard from a scroll tap.
            // Refresh state ASAP so any server-side correction lands fast.
            setTimeout(refreshSessions, 200);
        }}
        document.getElementById('scrollBtn')?.addEventListener('click', toggleScroll);

        async function copyPane() {{
            try {{
                const resp = await fetch('/copy');
                const data = await resp.json();
                const overlay = document.getElementById('copyOverlay');
                const content = document.getElementById('copyText');
                content.innerHTML = data.html;
                overlay.classList.add('active');
                // Scroll to bottom so most recent output is visible
                content.scrollTop = content.scrollHeight;
            }} catch (err) {{
                console.error('Copy failed:', err);
            }}
        }}

        async function newSession() {{
            // Spawn a new tmux window with claude-ephemeral. Old session keeps running.
            await promptNewSession();
        }}

        function escapeHtml(s) {{
            return String(s).replace(/[&<>"']/g, c => (
                {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]
            ));
        }}

        async function refreshSessions() {{
            try {{
                const resp = await fetch('/tmux/windows');
                const data = await resp.json();
                const list = document.getElementById('sessionList');
                const activeNameEl = document.getElementById('activeName');
                const activeThinkingEl = document.getElementById('activeThinking');
                const activeReadyEl = document.getElementById('activeReady');
                if (!data.windows || !data.windows.length) {{
                    list.innerHTML = '<div class="empty">No sessions</div>';
                    activeNameEl.textContent = '…';
                    if (activeThinkingEl) activeThinkingEl.style.display = 'none';
                    if (activeReadyEl) activeReadyEl.style.display = 'none';
                    return;
                }}
                list.innerHTML = '';
                let activeLabel = '';
                // Aggregate across ALL sessions for the menu-button badge:
                // busy beats ready beats nothing. Drawer-closed glance-spot.
                let anyBusy = false;
                let anyReady = false;
                for (const w of data.windows) {{
                    if (w.active) {{ activeLabel = w.name; }}
                    if (w.status === 'busy') anyBusy = true;
                    if (w.ready) anyReady = true;
                    const row = document.createElement('div');
                    row.className = 'session-row' + (w.active ? ' active' : '');
                    // Per spec: every session shows a dot when busy or ready,
                    // INCLUDING the active row. Red persists until the
                    // session goes busy again (cleared only by check_session_ready
                    // server-side, never by tap/select/view).
                    const dot = (w.status === 'busy')
                        ? '<span class="thinking-dot">●</span>'
                        : '<span class="ready-dot">●</span>';
                    row.innerHTML = `
                        <div class="row-main">
                            <span class="name">${{escapeHtml(w.name)}}</span>
                            ${{dot}}
                            <button class="row-close" data-idx="${{w.index}}" data-name="${{escapeHtml(w.name)}}">&times;</button>
                        </div>
                    `;
                    let pressTimer = null;
                    let longPressFired = false;
                    const startPress = () => {{
                        longPressFired = false;
                        pressTimer = setTimeout(() => {{
                            longPressFired = true;
                            pressTimer = null;
                            renameSession(w.index, w.name);
                        }}, 500);
                    }};
                    const cancelPress = () => {{
                        if (pressTimer) {{ clearTimeout(pressTimer); pressTimer = null; }}
                    }};
                    row.addEventListener('touchstart', startPress, {{ passive: true }});
                    row.addEventListener('touchend', cancelPress);
                    row.addEventListener('touchmove', cancelPress);
                    row.addEventListener('touchcancel', cancelPress);
                    row.addEventListener('mousedown', startPress);
                    row.addEventListener('mouseup', cancelPress);
                    row.addEventListener('mouseleave', cancelPress);
                    row.addEventListener('contextmenu', (e) => e.preventDefault());
                    row.addEventListener('click', (e) => {{
                        if (longPressFired) {{ longPressFired = false; return; }}
                        if (e.target.classList.contains('row-close')) return;
                        selectSession(w.index);
                    }});
                    row.querySelector('.row-close').addEventListener('click', (e) => {{
                        e.stopPropagation();
                        closeSession(w.index, w.name);
                    }});
                    list.appendChild(row);
                }}
                activeNameEl.textContent = activeLabel || '…';
                if (activeThinkingEl) activeThinkingEl.style.display = anyBusy ? '' : 'none';
                if (activeReadyEl) activeReadyEl.style.display = (!anyBusy && anyReady) ? '' : 'none';
                // Sync the scroll button's emoji to current pane mode.
                const nextInCopy = !!data.in_copy_mode;
                if (nextInCopy !== inCopyMode) {{
                    inCopyMode = nextInCopy;
                    updateScrollBtn();
                }}
            }} catch (err) {{
                console.error('refreshSessions failed:', err);
            }}
        }}

        function openDrawer() {{
            document.getElementById('drawer').classList.add('open');
            document.getElementById('drawerBackdrop').classList.add('active');
            refreshSessions();
        }}

        function closeDrawer() {{
            document.getElementById('drawer').classList.remove('open');
            document.getElementById('drawerBackdrop').classList.remove('active');
        }}

        async function promptNewSession() {{
            const defaultName = 'session-' + Date.now().toString().slice(-4);
            const name = prompt('New session — name?', defaultName);
            if (!name) return;
            try {{
                const resp = await fetch('/tmux/new', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name }})
                }});
                const data = await resp.json().catch(() => ({{}}));
                // If we just created the tmux session itself (came up from the
                // "No active sessions" empty state), the iframe is still showing
                // tmux-attach.sh's empty-state message — reload it so ttyd
                // re-runs tmux-attach.sh, which now finds the session and attaches.
                if (data.session_created) {{
                    reloadTerminal();
                }}
                // Give tmux a moment to create the window before refreshing
                setTimeout(refreshSessions, 400);
                closeDrawer();
            }} catch (err) {{
                console.error('new session failed:', err);
            }}
        }}

        async function promptNewTerminal() {{
            // Spawn a tmux window running a plain shell (NOT claude). Used
            // for sudo / admin tasks that don't fit a Claude session. Auto-
            // selects the new window so the user lands in it immediately.
            try {{
                const resp = await fetch('/tmux/new-terminal', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: '{{}}'
                }});
                const data = await resp.json().catch(() => ({{}}));
                if (data.session_created) {{
                    reloadTerminal();
                }}
                if (typeof data.index === 'number') {{
                    await fetch('/tmux/select', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ index: data.index }})
                    }});
                }}
                setTimeout(refreshSessions, 400);
                closeDrawer();
            }} catch (err) {{
                console.error('new terminal failed:', err);
            }}
        }}

        async function selectSession(idx) {{
            try {{
                await fetch('/tmux/select', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ index: idx }})
                }});
                closeDrawer();
                setTimeout(refreshSessions, 200);
            }} catch (err) {{
                console.error('select failed:', err);
            }}
        }}

        async function renameSession(idx, currentName) {{
            const name = prompt('Rename session', currentName);
            if (!name || name === currentName) return;
            try {{
                await fetch('/tmux/rename', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ index: idx, name }})
                }});
                setTimeout(refreshSessions, 200);
            }} catch (err) {{
                console.error('rename failed:', err);
            }}
        }}

        async function closeSession(idx, name) {{
            if (!confirm(`Close session "${{name}}"?`)) return;
            try {{
                const resp = await fetch('/tmux/close', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ index: idx }})
                }});
                const data = await resp.json();
                if (data.status === 'rejected') {{
                    alert(data.error || 'Cannot close this session');
                }} else if (data.destroyed) {{
                    // Closed the last window — tmux session is gone. Reload the
                    // iframe so tmux-attach.sh re-runs and shows the empty-state
                    // message (it's attach-only, so no ghost session spawns).
                    reloadTerminal();
                }}
                setTimeout(refreshSessions, 400);
            }} catch (err) {{
                console.error('close failed:', err);
            }}
        }}

        function closeCopy() {{
            document.getElementById('copyOverlay').classList.remove('active');
            input.focus();
        }}

        function compressImage(file, maxWidth, quality) {{
            return new Promise((resolve) => {{
                // Skip compression for non-image files
                if (!file.type.startsWith('image/')) {{
                    resolve(file);
                    return;
                }}
                const img = new Image();
                img.onload = () => {{
                    URL.revokeObjectURL(img.src);
                    let w = img.width, h = img.height;
                    if (w > maxWidth) {{
                        h = Math.round(h * maxWidth / w);
                        w = maxWidth;
                    }}
                    const canvas = document.createElement('canvas');
                    canvas.width = w;
                    canvas.height = h;
                    canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                    canvas.toBlob((blob) => {{
                        const name = file.name.replace(/\\.[^.]+$/, '.jpg');
                        resolve(new File([blob], name, {{ type: 'image/jpeg' }}));
                    }}, 'image/jpeg', quality);
                }};
                img.src = URL.createObjectURL(file);
            }});
        }}

        async function uploadPhoto(fileInput) {{
            const files = Array.from(fileInput.files);
            if (!files.length) return;
            const btn = fileInput.previousElementSibling;
            try {{
                await uploadFiles(files, btn);
            }} finally {{
                fileInput.value = '';
            }}
        }}

        async function uploadFiles(files, btn) {{
            if (!files.length) return;
            const origText = btn.textContent;
            const origPlaceholder = input.placeholder;

            // Show counter on button + status in textarea placeholder
            const total = files.length;
            let done = 0;
            btn.textContent = '0/' + total;
            btn.disabled = true;
            input.placeholder = 'Compressing ' + total + ' photo' + (total > 1 ? 's' : '') + '...';

            try {{
                // Compress all photos first (resize to 1568px, 85% JPEG quality)
                const compressed = await Promise.all(files.map(f => compressImage(f, 1568, 0.85)));
                input.placeholder = 'Uploading ' + total + ' photo' + (total > 1 ? 's' : '') + '...';

                // Upload all photos in parallel, updating counter as each finishes
                const uploads = compressed.map(file => {{
                    const form = new FormData();
                    form.append('file', file);
                    return fetch('/upload', {{ method: 'POST', body: form }})
                        .then(r => r.json())
                        .then(data => {{
                            done++;
                            btn.textContent = done + '/' + total;
                            input.placeholder = 'Uploaded ' + done + '/' + total + '...';
                            return data;
                        }});
                }});
                const results = await Promise.all(uploads);

                // Show friendly names in textarea
                const tags = results.filter(r => r.name).map(r => '[' + r.name + ']');
                if (tags.length) {{
                    const prefix = input.value.trim();
                    input.value = (prefix ? prefix + '\\n' : '') + tags.join('\\n') + '\\n';
                    input.style.height = 'auto';
                    input.style.height = Math.min(input.scrollHeight, 100) + 'px';
                    input.focus();
                }}
            }} catch (err) {{
                console.error('Upload failed:', err);
                input.placeholder = 'Upload failed. Try again.';
                setTimeout(() => {{ input.placeholder = origPlaceholder; }}, 3000);
            }} finally {{
                btn.textContent = origText;
                btn.disabled = false;
                input.placeholder = origPlaceholder;
            }}
        }}

        // Sidecar statusbar: poll /status every 60s, render server-converted HTML.
        // Bypasses xterm.js col-truncation by rendering the bar as native HTML.
        async function refreshStatus() {{
            // 'Off' mode: don't poll the server at all — clear and bail.
            if (statusBarMode === 'off') {{
                const bar = document.getElementById('statusBar');
                if (bar) bar.innerHTML = '';
                return;
            }}
            try {{
                const r = await fetch('/status', {{ cache: 'no-store' }});
                const data = await r.json();
                const bar = document.getElementById('statusBar');
                if (bar) bar.innerHTML = data.html || '';
            }} catch (e) {{ /* silent */ }}
        }}
        refreshStatus();
        setInterval(refreshStatus, 60000);

        // Sessions poll: 3s when tab visible so the thinking dot on the menu
        // button (and ready/thinking dots in the drawer) track Claude's state
        // in near-real-time. Pauses when document.hidden — the browser would
        // throttle anyway, no point hitting the server for nothing.
        let __sessionsPollTimer = null;
        function startSessionsPoll() {{
            if (__sessionsPollTimer) return;
            __sessionsPollTimer = setInterval(() => {{
                if (!document.hidden) refreshSessions();
            }}, 3000);
        }}
        function stopSessionsPoll() {{
            if (__sessionsPollTimer) {{ clearInterval(__sessionsPollTimer); __sessionsPollTimer = null; }}
        }}
        document.addEventListener('visibilitychange', () => {{
            if (document.hidden) stopSessionsPoll(); else startSessionsPoll();
        }});
        startSessionsPoll();

        // Auto-reconnect: reload iframe when tab becomes visible again.
        // ttyd closes WS with code 1000 when Chrome backgrounds the tab,
        // which triggers its "Press ⏎ to Reconnect" overlay; force-reload
        // the iframe to bypass that and reattach to the persistent tmux session.
        const terminal = document.querySelector('.terminal-frame');
        const TERMINAL_SRC = window.location.protocol === 'https:'
            ? `${{window.location.protocol}}//${{window.location.hostname}}:8443`
            : `http://{ip}:{TTYD_PORT}`;
        terminal.src = TERMINAL_SRC;
        // Reconnecting overlay state — visible during iframe reload, hidden
        // when the iframe finishes loading the REAL URL (not about:blank).
        // Bridges the visual gap that previously showed ttyd's "Press ⏎ to
        // Reconnect" prompt or a blank white flash to the user.
        const reloadingOverlay = document.getElementById('iframeReloading');
        let __pendingRealLoad = false;
        function reloadTerminal() {{
            if (reloadingOverlay) reloadingOverlay.classList.add('visible');
            __pendingRealLoad = true;
            terminal.src = 'about:blank';
            setTimeout(() => {{ terminal.src = TERMINAL_SRC + '?t=' + Date.now(); }}, 50);
        }}
        terminal.addEventListener('load', () => {{
            // about:blank load is the intermediate step; ignore it.
            // Only hide once the real ttyd URL has loaded. Small delay so
            // xterm.js has time to render its first frame.
            if (__pendingRealLoad && terminal.src && !terminal.src.endsWith('about:blank')) {{
                __pendingRealLoad = false;
                setTimeout(() => {{
                    if (reloadingOverlay) reloadingOverlay.classList.remove('visible');
                }}, 300);
            }}
        }});
        // Full "get me unstuck" action: re-attach the terminal iframe to the
        // persistent tmux session and refresh the session list + statusbar.
        // Wired to the ↻ quick-key (manual) and the auto-reconnect listeners
        // below (tab-foreground / BFCache restore). Manual button covers the
        // case auto-reconnect misses: ttyd's WS drops without the tab ever
        // backgrounding (network blip), leaving its "Press ⏎ to Reconnect"
        // overlay with no visibilitychange to clear it.
        function reconnectAll() {{
            reloadTerminal();
            refreshSessions();
            refreshStatus();
        }}
        // Debounce: visibilitychange + focus + pageshow all fire on tab return,
        // and reloading the iframe 3 times back-to-back leaves it blank.
        let __lastReconnectAt = 0;
        function reconnectDebounced() {{
            const now = Date.now();
            if (now - __lastReconnectAt < 1500) return;
            // Suppress while user is actively typing — see input handler comment.
            if (isComposing) return;
            if (document.activeElement === input) return;
            if (now - lastInputAt < 5000) return;
            __lastReconnectAt = now;
            reconnectAll();
        }}
        document.addEventListener('visibilitychange', () => {{
            if (document.visibilityState === 'visible') {{ reconnectDebounced(); }}
        }});
        window.addEventListener('pageshow', (e) => {{
            if (e.persisted) {{ reconnectDebounced(); }}
        }});
        // 'online' catches network-blip recoveries (ttyd's WS drops without a
        // visibility change). The 'focus' listener used to live here too but
        // was removed — Android Chrome fires window.focus too eagerly on taps,
        // URL-bar interactions, and visual-viewport shifts. Result was a flash
        // on every tap of the terminal area because reconnectDebounced ran
        // before input.focus() had time to update document.activeElement, so
        // the textarea-focus gate didn't catch it. The manual ↻ button covers
        // any case visibilitychange + pageshow + online miss.
        window.addEventListener('online', reconnectDebounced);

        // Populate session label + drawer on first paint
        refreshSessions();

        // Touch-swipe over the terminal pane → scroll tmux scrollback line-by-line.
        // The iframe is output-only on the phone (input flows through this page's
        // textarea), so capturing all pointer events on top of it is safe.
        // Tap with no movement = focus the textarea so the keyboard pops up.
        (function setupScrollOverlay() {{
            const overlay = document.getElementById('scrollOverlay');
            const hint = document.getElementById('scrollHint');
            if (!overlay) return;
            // PIXELS_PER_LINE is a script-scope `let` set at the top of this
            // script block so the Settings sheet's sensitivity slider can
            // mutate it live without reloading. Default 14.
            const THROTTLE_MS = 40;
            const TAP_THRESHOLD_PX = 16;
            let lastY = 0;
            let accumulated = 0;
            let movementMag = 0;
            let isTouching = false;
            let pendingLines = 0;
            let scrollTimer = null;
            let hintTimer = null;

            function showHint() {{
                hint.classList.add('visible');
                clearTimeout(hintTimer);
                hintTimer = setTimeout(() => hint.classList.remove('visible'), 600);
            }}

            async function flush() {{
                const lines = pendingLines;
                pendingLines = 0;
                scrollTimer = null;
                if (lines === 0) return;
                try {{
                    await fetch('/scroll', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            direction: lines > 0 ? 'up' : 'down',
                            lines: Math.abs(lines)
                        }})
                    }});
                }} catch (err) {{ /* silent */ }}
            }}

            function queue(lines) {{
                pendingLines += lines;
                if (!scrollTimer) scrollTimer = setTimeout(flush, THROTTLE_MS);
                showHint();
            }}

            overlay.addEventListener('touchstart', (e) => {{
                if (e.touches.length !== 1) return;
                lastY = e.touches[0].clientY;
                accumulated = 0;
                movementMag = 0;
                isTouching = true;
            }}, {{ passive: true }});

            overlay.addEventListener('touchmove', (e) => {{
                if (!isTouching || e.touches.length !== 1) return;
                // Swipe-to-scroll is OFF by default and only activates when
                // the user explicitly enables it via the ⏫ button. Lets
                // pull-to-refresh and edge-swipe own gestures from cold.
                if (!inCopyMode) return;
                e.preventDefault();
                const y = e.touches[0].clientY;
                // Natural mobile scrolling: drag finger DOWN reveals older
                // content above (like dragging a piece of paper). Positive dy
                // = finger moved down = scroll back into history.
                const dy = y - lastY;
                accumulated += dy;
                lastY = y;
                movementMag += Math.abs(dy);
                const lines = Math.trunc(accumulated / PIXELS_PER_LINE);
                if (lines !== 0) {{
                    accumulated -= lines * PIXELS_PER_LINE;
                    queue(lines);
                }}
            }}, {{ passive: false }});

            overlay.addEventListener('touchend', () => {{
                isTouching = false;
                // Deliberately NO input.focus() here. Per John: tapping the
                // terminal should never pop the keyboard. Only tapping the
                // textarea itself (next to Send) brings the keyboard up.
            }}, {{ passive: true }});

            overlay.addEventListener('touchcancel', () => {{
                isTouching = false;
            }}, {{ passive: true }});

            // Desktop / trackpad wheel: same overlay, translate wheel delta.
            // Also gated on inCopyMode (the explicit scroll toggle) so the
            // wheel doesn't randomly enter copy mode on desktop browsers.
            overlay.addEventListener('wheel', (e) => {{
                if (!inCopyMode) return;
                e.preventDefault();
                const lines = Math.round(-e.deltaY / PIXELS_PER_LINE);
                if (lines !== 0) queue(lines);
            }}, {{ passive: false }});

            // No click handler — per John, clicking/tapping the terminal
            // must NOT pop the keyboard. The textarea is the explicit
            // typing surface; tap it directly to focus.
        }})();

        // ============================================================
        // Skills bottom sheet — long-press the / quick-key to open.
        // Fetches /skills which lists ~/.claude/skills/ subdirectories
        // (alphabetized). Tap a row to fire `/<skillname>` to claude.
        // Tap-and-release on / sends a literal /; long-press (500ms)
        // opens the sheet without sending anything.
        // ============================================================
        let __skillsCache = null;
        async function openSkillsSheet() {{
            const sheet = document.getElementById('skillsSheet');
            const backdrop = document.getElementById('skillsBackdrop');
            const listEl = document.getElementById('skillsList');
            sheet.classList.add('open');
            backdrop.classList.add('active');
            if (!__skillsCache) {{
                try {{
                    const r = await fetch('/skills');
                    const data = await r.json();
                    __skillsCache = data.skills || [];
                }} catch (err) {{
                    listEl.innerHTML = '<div class="skills-empty">failed to load skills</div>';
                    return;
                }}
            }}
            if (!__skillsCache.length) {{
                listEl.innerHTML = '<div class="skills-empty">no skills found</div>';
                return;
            }}
            listEl.innerHTML = '';
            for (const name of __skillsCache) {{
                const row = document.createElement('div');
                row.className = 'skills-row';
                row.textContent = '/' + name;
                row.addEventListener('click', async () => {{
                    closeSkillsSheet();
                    await sendText('/' + name);
                }});
                listEl.appendChild(row);
            }}
        }}
        function closeSkillsSheet() {{
            document.getElementById('skillsSheet').classList.remove('open');
            document.getElementById('skillsBackdrop').classList.remove('active');
        }}
        (function setupSlashButton() {{
            const btn = document.getElementById('slashBtn');
            if (!btn) return;
            let pressTimer = null;
            let longPressFired = false;
            const startPress = () => {{
                longPressFired = false;
                pressTimer = setTimeout(() => {{
                    longPressFired = true;
                    pressTimer = null;
                    openSkillsSheet();
                }}, 500);
            }};
            const cancelPress = () => {{
                if (pressTimer) {{ clearTimeout(pressTimer); pressTimer = null; }}
            }};
            btn.addEventListener('touchstart', startPress, {{ passive: true }});
            btn.addEventListener('touchend', cancelPress);
            btn.addEventListener('touchmove', cancelPress);
            btn.addEventListener('touchcancel', cancelPress);
            btn.addEventListener('mousedown', startPress);
            btn.addEventListener('mouseup', cancelPress);
            btn.addEventListener('mouseleave', cancelPress);
            btn.addEventListener('contextmenu', (e) => e.preventDefault());
            btn.addEventListener('click', () => {{
                if (longPressFired) {{ longPressFired = false; return; }}
                sendKey('/');
            }});
        }})();

        // ============================================================
        // Visual Viewport keyboard handling — keep the iframe size
        // CONSTANT when the soft keyboard appears (so xterm.js doesn't
        // re-fit and repaint). The bottom-panel (status + quick-keys +
        // input-bar) is position: fixed and translates up by the
        // keyboard height via the --keyboard-height CSS var.
        // The terminal-wrap is position: absolute with bottom = the
        // measured --bottom-panel-height, so it stays constant size
        // regardless of keyboard state.
        // ============================================================
        (function setupViewport() {{
            const root = document.documentElement;
            const bottomPanel = document.getElementById('bottomPanel');

            function readViewportKh() {{
                const vv = window.visualViewport;
                if (!vv) return 0;
                // Keyboard height = layout viewport - visual viewport. Do NOT
                // subtract vv.offsetTop — Android Chrome scrolls the layout
                // viewport during/after keyboard transitions to keep the focus
                // target in view, and offsetTop changes on each scroll. The
                // subtraction conflated keyboard motion with scroll motion,
                // making readViewportKh return shifting values that triggered
                // extra snaps via the visualViewport.scroll listener.
                return Math.max(0, Math.round(window.innerHeight - vv.height));
            }}
            function updateBottomPanelVar() {{
                if (bottomPanel) {{
                    root.style.setProperty('--bottom-panel-height', bottomPanel.offsetHeight + 'px');
                }}
            }}

            // Snap-with-secondary-event-filter.
            //
            // Android Chrome fires visualViewport.resize at the START and
            // END of the keyboard slide (sometimes more — Gboard's
            // suggestion bar settling, keyboard chrome reflow). The two
            // events often carry slightly different target heights, so
            // snapping naively to every event produces:
            //   - First snap: panel jumps to event-1 position (most of
            //     the motion).
            //   - ~250 ms later: second snap by ~10–50 px (visible as a
            //     small "flash + extra move" at the end of each
            //     transition — what John reported as ruining the
            //     experience even after the rAF animation was removed).
            //
            // Fix: ignore secondary events with SMALL delta within a
            // SHORT window of the last snap. Treat them as Android's
            // settle-up noise. Large deltas (real dismiss, keyboard
            // re-open) always pass through so quick keyboard up→down
            // still works. Outside the window, any delta passes.
            //
            // Constants tuned empirically:
            //   FILTER_WINDOW_MS = 500 — covers the typical ~250 ms gap
            //     between Android's start and end events with margin.
            //   FILTER_DELTA_PX  = 80  — bigger than typical suggestion-
            //     bar height (40–60 px) but smaller than a typical
            //     keyboard slide (~250–350 px), so dismissals always
            //     pass and only the settle-noise gets filtered.
            const FILTER_WINDOW_MS = 500;
            const FILTER_DELTA_PX = 80;
            let lastSnapAt = 0;
            function applyViewport() {{
                const now = performance.now();
                const newKh = readViewportKh();
                const currentStr = getComputedStyle(root).getPropertyValue('--keyboard-height').trim();
                const currentKh = parseFloat(currentStr) || 0;
                const delta = Math.abs(newKh - currentKh);
                if (now - lastSnapAt < FILTER_WINDOW_MS && delta > 0 && delta < FILTER_DELTA_PX) {{
                    // Settle-noise: skip. Panel stays at the first-event
                    // position (off by < 80 px from the "true" final
                    // value, but imperceptible).
                    return;
                }}
                root.style.setProperty('--keyboard-height', newKh + 'px');
                updateBottomPanelVar();
                lastSnapAt = now;
            }}
            function onViewportChange() {{ applyViewport(); }}

            if (window.visualViewport) {{
                window.visualViewport.addEventListener('resize', onViewportChange);
                // No 'scroll' listener: visualViewport.scroll fires during
                // browser auto-scroll-to-keep-focus-visible, often AFTER the
                // keyboard slide completes. Each scroll fired applyViewport
                // again and (combined with the offsetTop subtraction that's
                // now also gone) drove the "small move at the end" John
                // reported. Resize is the only relevant signal for keyboard
                // appearance/dismissal.
            }}
            window.addEventListener('resize', onViewportChange);
            if (window.ResizeObserver && bottomPanel) {{
                const ro = new ResizeObserver(updateBottomPanelVar);
                ro.observe(bottomPanel);
            }}
            // Initial sync (no animation needed at page load)
            root.style.setProperty('--keyboard-height', readViewportKh() + 'px');
            updateBottomPanelVar();
            setTimeout(updateBottomPanelVar, 100);
        }})();

        // ============================================================
        // Pull-to-refresh + edge-swipe drawer (document-level capture).
        // Captures touch BEFORE the scroll overlay so its handlers
        // don't fire on these gestures. Visual indicator at top for
        // pull-to-refresh; drawer slides in on edge swipe.
        // ============================================================
        (function setupPageGestures() {{
            const PULL_THRESHOLD_PX = 80;
            const EDGE_SWIPE_START_PX = 24;
            const EDGE_SWIPE_THRESHOLD_PX = 60;
            const TOP_GRAB_PX = 30;
            const indicator = document.getElementById('pullIndicator');
            let startX = 0, startY = 0;
            let mode = null;  // null | 'pull-candidate' | 'pull' | 'edge-candidate' | 'edge'
            let lastDy = 0;

            function reset() {{
                mode = null;
                lastDy = 0;
                if (indicator) indicator.classList.remove('visible');
            }}

            document.addEventListener('touchstart', (e) => {{
                if (e.touches.length !== 1) {{ reset(); return; }}
                const t = e.touches[0];
                startX = t.clientX;
                startY = t.clientY;
                lastDy = 0;
                // Don't trigger gestures when drawer / sheet / copy overlay is up
                const drawer = document.getElementById('drawer');
                const sheet = document.getElementById('skillsSheet');
                const copy = document.getElementById('copyOverlay');
                if ((drawer && drawer.classList.contains('open')) ||
                    (sheet && sheet.classList.contains('open')) ||
                    (copy && copy.classList.contains('active'))) {{
                    mode = null;
                    return;
                }}
                if (startX < EDGE_SWIPE_START_PX) mode = 'edge-candidate';
                // Pull-to-refresh: ANYWHERE on the page (not just y<30) when
                // scroll mode is OFF. The previous y<30 threshold was nearly
                // impossible to hit reliably on a phone — the user's thumb
                // doesn't naturally start at the very top edge. Now any
                // downward drag of >80 px counts. When scroll mode is ON,
                // gestures belong to terminal scrolling, not refresh.
                else if (!inCopyMode) mode = 'pull-candidate';
                else mode = null;
            }}, {{ capture: true, passive: true }});

            document.addEventListener('touchmove', (e) => {{
                if (!mode) return;
                if (e.touches.length !== 1) {{ reset(); return; }}
                const t = e.touches[0];
                const dx = t.clientX - startX;
                const dy = t.clientY - startY;
                if (mode === 'edge-candidate') {{
                    if (dx > 20 && Math.abs(dy) < 40) {{
                        mode = 'edge';
                        e.stopPropagation();
                    }} else if (Math.abs(dy) > 30) {{
                        mode = null;
                    }}
                }} else if (mode === 'pull-candidate') {{
                    if (dy > 20 && Math.abs(dx) < 40) {{
                        mode = 'pull';
                        e.stopPropagation();
                    }} else if (Math.abs(dx) > 30 || dy < -10) {{
                        mode = null;
                    }}
                }}
                if (mode === 'pull') {{
                    lastDy = dy;
                    e.stopPropagation();
                    e.preventDefault();
                    if (indicator) {{
                        indicator.textContent = (dy >= PULL_THRESHOLD_PX)
                            ? '↻ release to refresh'
                            : 'pull to refresh';
                        indicator.classList.add('visible');
                    }}
                }} else if (mode === 'edge') {{
                    e.stopPropagation();
                }}
            }}, {{ capture: true, passive: false }});

            document.addEventListener('touchend', (e) => {{
                if (mode === 'edge') {{
                    e.stopPropagation();
                    openDrawer();
                }} else if (mode === 'pull') {{
                    e.stopPropagation();
                    if (lastDy >= PULL_THRESHOLD_PX) {{
                        reconnectAll();
                        showToast('refreshing…', 1500);
                    }}
                }}
                reset();
            }}, {{ capture: true, passive: true }});

            document.addEventListener('touchcancel', () => {{ reset(); }}, {{ capture: true }});
        }})();

        // ============================================================
        // Settings sheet — currently exposes Scroll sensitivity. Loaded
        // from localStorage on init; the slider mutates PIXELS_PER_LINE
        // live (script-scope let, used by setupScrollOverlay's swipe +
        // wheel paths) and writes back on every change.
        // ============================================================
        function applySensitivity(value) {{
            const v = Math.max(SENS_MIN, Math.min(SENS_MAX, parseInt(value) || SENS_DEFAULT));
            PIXELS_PER_LINE = v;
            const valueEl = document.getElementById('sensValue');
            if (valueEl) valueEl.textContent = v + ' px/line';
            try {{ localStorage.setItem(SENS_KEY, String(v)); }} catch (e) {{ /* private mode */ }}
        }}
        function loadSensitivity() {{
            let stored = SENS_DEFAULT;
            try {{
                const raw = localStorage.getItem(SENS_KEY);
                const parsed = parseInt(raw);
                if (!isNaN(parsed) && parsed >= SENS_MIN && parsed <= SENS_MAX) stored = parsed;
            }} catch (e) {{ /* private mode */ }}
            PIXELS_PER_LINE = stored;
            const slider = document.getElementById('sensSlider');
            if (slider) slider.value = stored;
            const valueEl = document.getElementById('sensValue');
            if (valueEl) valueEl.textContent = stored + ' px/line';
        }}
        // Status bar mode (Full / 1-line / Off). Toggles CSS classes on
        // #statusBar; classes survive refreshStatus() since that only rewrites
        // innerHTML, not classList.
        function applyStatusMode(mode) {{
            if (!STATUS_MODES.includes(mode)) mode = STATUS_MODE_DEFAULT;
            statusBarMode = mode;
            const bar = document.getElementById('statusBar');
            if (bar) {{
                bar.classList.remove('compact', 'off');
                if (mode === 'compact') bar.classList.add('compact');
                else if (mode === 'off') bar.classList.add('off');
            }}
            const seg = document.getElementById('statusSeg');
            if (seg) seg.querySelectorAll('button').forEach(b => {{
                b.classList.toggle('active', b.dataset.mode === mode);
            }});
            try {{ localStorage.setItem(STATUS_MODE_KEY, mode); }} catch (e) {{ /* private mode */ }}
            // Leaving 'off' should repaint immediately rather than waiting for
            // the next 60s tick; entering 'off' clears via refreshStatus's guard.
            refreshStatus();
        }}
        function loadStatusMode() {{
            let stored = STATUS_MODE_DEFAULT;
            try {{
                const raw = localStorage.getItem(STATUS_MODE_KEY);
                if (STATUS_MODES.includes(raw)) stored = raw;
            }} catch (e) {{ /* private mode */ }}
            applyStatusMode(stored);
        }}
        document.getElementById('statusSeg')?.addEventListener('click', (e) => {{
            const btn = e.target.closest('button[data-mode]');
            if (btn) applyStatusMode(btn.dataset.mode);
        }});
        function openSettingsSheet() {{
            // Sync slider to current value (in case storage changed externally
            // or this is the first open).
            const slider = document.getElementById('sensSlider');
            if (slider) slider.value = PIXELS_PER_LINE;
            const valueEl = document.getElementById('sensValue');
            if (valueEl) valueEl.textContent = PIXELS_PER_LINE + ' px/line';
            // Close the drawer if it's open — the settings sheet is a
            // foreground action, not a drawer subview.
            closeDrawer();
            document.getElementById('settingsBackdrop').classList.add('active');
            document.getElementById('settingsSheet').classList.add('open');
        }}
        function closeSettingsSheet() {{
            document.getElementById('settingsSheet').classList.remove('open');
            document.getElementById('settingsBackdrop').classList.remove('active');
        }}
        document.getElementById('sensSlider')?.addEventListener('input', (e) => {{
            applySensitivity(e.target.value);
        }});
        loadSensitivity();
        loadStatusMode();

        input.focus();
    </script>
</body>
</html>"""


def _exit_copy_mode():
    """Best-effort cancel of tmux copy/view mode. No-op (with swallowed
    stderr) when the pane isn't in a mode. Run this before any send-keys
    that injects user input so scrolled-back panes don't eat the keystrokes."""
    subprocess.run(
        [TMUX, "send-keys", "-t", TMUX_SESSION, "-X", "cancel"],
        capture_output=True,
        timeout=5,
    )


@app.post("/send")
async def send_text(payload: TextInput):
    """Send literal text to tmux, then press Enter."""
    _exit_copy_mode()
    subprocess.run(
        [TMUX, "send-keys", "-t", TMUX_SESSION, "-l", payload.text],
        timeout=5,
    )
    subprocess.run(
        [TMUX, "send-keys", "-t", TMUX_SESSION, "Enter"],
        timeout=5,
    )
    return {"status": "sent"}


class ScrollInput(BaseModel):
    direction: str
    lines: int | None = None


@app.post("/scroll")
async def scroll_pane(payload: ScrollInput):
    """Scroll the tmux pane's scrollback buffer.

    With `lines` set (touch-swipe path): scroll-up/scroll-down N lines. Up
    enters copy mode first; down is a no-op outside copy mode (which is what
    we want — finger flicks at the live bottom shouldn't do anything).

    Without `lines` (legacy ⇞/⇟ button path): page-up / page-down as before.
    """
    count = max(0, int(payload.lines or 0))
    if payload.direction == "up":
        subprocess.run(
            [TMUX, "copy-mode", "-t", TMUX_SESSION],
            capture_output=True,
            timeout=5,
        )
        if count > 0:
            subprocess.run(
                [TMUX, "send-keys", "-t", TMUX_SESSION, "-N", str(count), "-X", "scroll-up"],
                capture_output=True,
                timeout=5,
            )
        else:
            subprocess.run(
                [TMUX, "send-keys", "-t", TMUX_SESSION, "-X", "page-up"],
                capture_output=True,
                timeout=5,
            )
    elif payload.direction == "down":
        if count > 0:
            subprocess.run(
                [TMUX, "send-keys", "-t", TMUX_SESSION, "-N", str(count), "-X", "scroll-down"],
                capture_output=True,
                timeout=5,
            )
        else:
            subprocess.run(
                [TMUX, "send-keys", "-t", TMUX_SESSION, "-X", "page-down"],
                capture_output=True,
                timeout=5,
            )
    else:
        return {"status": "rejected", "error": "direction must be up or down"}
    return {"status": "sent"}


ALLOWED_KEYS = {
    "Up", "Down", "Left", "Right", "Tab", "BTab", "Escape", "Enter",
    "C-c", "C-l", "C-d", "C-z", "C-a", "C-e", "C-k", "C-u",
    "BSpace", "DC", "Home", "End", "PPage", "NPage",
    "/",
}



@app.post("/key")
async def send_key(payload: KeyInput):
    """Send a special key (Escape, C-c, Enter, etc.) to tmux.

    Special-case Escape: when the pane is in copy/view mode, only cancel the
    mode and DO NOT forward Escape — otherwise the Escape passes through to
    the underlying program (Claude Code interprets it as 'interrupt response'),
    which surprised John when he was just trying to exit copy-mode scrolling.
    """
    if payload.key not in ALLOWED_KEYS:
        return {"status": "rejected", "error": "key not allowed"}
    if payload.key == "Escape":
        in_mode = subprocess.run(
            [TMUX, "display", "-p", "-t", TMUX_SESSION, "#{pane_in_mode}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() == "1"
        if in_mode:
            subprocess.run(
                [TMUX, "send-keys", "-t", TMUX_SESSION, "-X", "cancel"],
                timeout=5,
            )
            return {"status": "sent", "exited_copy_mode": True}
    subprocess.run(
        [TMUX, "send-keys", "-t", TMUX_SESSION, payload.key],
        timeout=5,
    )
    return {"status": "sent"}


@app.post("/scroll/exit")
async def scroll_exit():
    """Exit tmux copy/view mode without forwarding any key to the underlying
    program. Cannot interrupt Claude under any circumstance — use this as the
    safe alternative to the gated /key Escape handler when the only intent is
    to stop scrolling. No-op when not in copy mode."""
    _exit_copy_mode()
    return {"status": "ok"}


@app.post("/scroll/enter")
async def scroll_enter():
    """Enter tmux copy mode without scrolling. Unlocks the swipe-scroll path
    (gated client-side on inCopyMode) so the user can drag at their own pace,
    instead of the previous behavior which jumped a full page on every ⏫ tap.
    Pairs with /scroll/exit. Idempotent — tmux copy-mode is a no-op if already
    in copy mode."""
    subprocess.run(
        [TMUX, "copy-mode", "-t", TMUX_SESSION],
        capture_output=True,
        timeout=5,
    )
    return {"status": "ok"}


@app.get("/status")
async def get_status():
    """Render Claude Code's statusbar as HTML for the sidecar display."""
    return {"html": render_statusline_html()}


@app.get("/state")
async def get_state():
    """Surface pane mode so the UI can show scroll indicators."""
    r = subprocess.run(
        [TMUX, "display-message", "-p", "-t", TMUX_SESSION, "#{pane_in_mode}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return {"in_copy_mode": r.stdout.strip() == "1"}


@app.get("/copy")
async def copy_pane():
    """Capture full tmux pane scrollback as ANSI-styled HTML so the panel
    looks like the live terminal (colors, bold, dim, underline). `-e` keeps
    escape sequences; `-J` joins wrapped lines so long output renders as one
    logical line. Plain-text copy still works in the browser: selecting text
    inside the rendered <span>s copies the visible characters only, not the
    surrounding HTML."""
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", TMUX_SESSION, "-p", "-S", "-", "-e", "-J"],
        capture_output=True, text=True, timeout=5,
    )
    return {"html": ansi_to_html(result.stdout)}


UPLOAD_DIR = Path("/tmp/claude-uploads")
MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 MB


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Save an uploaded file using its original name and return the path."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize filename: strip path components and special characters
    raw_name = file.filename or "photo.jpg"
    name = Path(raw_name).name
    name = re.sub(r'[^\w.\-]', '_', name)
    if not name or name.startswith('.'):
        name = "photo.jpg"
    dest = UPLOAD_DIR / name
    # Handle duplicate filenames with a counter suffix
    counter = 2
    while dest.exists():
        stem = Path(name).stem
        ext = Path(name).suffix
        dest = UPLOAD_DIR / f"{stem}-{counter}{ext}"
        counter += 1
    # Stream-read with size limit to avoid memory exhaustion
    chunks = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_SIZE:
            return {"error": "File too large (max 20MB)"}
        chunks.append(chunk)
    dest.write_bytes(b"".join(chunks))
    return {"name": dest.name, "path": str(dest)}


@app.get("/tmux/windows")
async def list_windows():
    """List tmux windows in the claude session. Each is a separate CC session.

    For windows that are red (ready=True), also captures the last non-blank
    line of the pane so the drawer can surface 'what's it waiting on?' inline.
    Only captures for ready windows to keep the polling cost bounded — busy
    sessions change every render anyway and a preview would be noise."""
    result = subprocess.run(
        [TMUX, "list-windows", "-t", TMUX_SESSION,
         "-F", "#{window_index}\t#{window_name}\t#{window_active}\t#{pane_tty}"],
        capture_output=True, text=True, timeout=5,
    )
    windows = []
    pid_tty = _pid_tty_map()  # one ps call shared across all windows this poll
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            name = parts[1]
            pane_tty = parts[3] if len(parts) >= 4 else ""
            # Resolve the session by TTY (robust to renames + name collisions);
            # fall back to the legacy name lookup only if the tty match misses.
            sid, status = find_cc_session_for_tty(pane_tty, pid_tty)
            if sid is None:
                sid, status = find_cc_session_for_window(name)
            ready = check_session_ready(sid, status or "idle") if sid else False
            entry = {
                "index": int(parts[0]),
                "name": name,
                "active": parts[2] == "1",
                "ready": ready,
                "status": status or "idle",
            }
            if ready:
                try:
                    cap = subprocess.run(
                        [TMUX, "capture-pane", "-p", "-t",
                         f"{TMUX_SESSION}:{entry['index']}"],
                        capture_output=True, text=True, timeout=2,
                    )
                    last = ""
                    for ln in reversed(cap.stdout.split("\n")):
                        s = ln.strip()
                        if s:
                            last = s
                            break
                    # Truncate from the right so the prompt tail is preserved
                    entry["last_line"] = last[-80:] if last else ""
                except (subprocess.SubprocessError, OSError):
                    entry["last_line"] = ""
            windows.append(entry)
    # Active-pane copy-mode flag — drives the scroll button's emoji
    # toggle (⏫ when live, ⏬ when scrolled-back into history).
    try:
        mode_res = subprocess.run(
            [TMUX, "display-message", "-p", "-t", TMUX_SESSION, "#{pane_in_mode}"],
            capture_output=True, text=True, timeout=2,
        )
        in_copy_mode = mode_res.stdout.strip() == "1"
    except (subprocess.SubprocessError, OSError):
        in_copy_mode = False
    return {"windows": windows, "in_copy_mode": in_copy_mode}


# Built-in Claude Code slash commands worth surfacing in the phone skills
# sheet even though they live in the CC binary, not ~/.claude/skills/.
# Tapping the row fires `/effort`, which opens CC's reasoning-effort slider —
# drivable from the phone with the Up/Down/Enter quick-keys on the bar.
NATIVE_SLASH_COMMANDS = ["effort"]


@app.get("/skills")
async def list_skills():
    """Enumerate ~/.claude/skills/ subdirectory names plus select built-in
    Claude Code commands (NATIVE_SLASH_COMMANDS), alphabetized.

    Each subdirectory is a user-defined skill (an SKILL.md plus optional
    helpers); the native commands are CC built-ins that have no skill dir.
    The phone wrapper's long-press-/ sheet displays this list so John can
    fire any slash command without typing it on the phone keyboard. Hidden
    dirs and files are excluded."""
    names = list(NATIVE_SLASH_COMMANDS)
    skills_dir = Path.home() / ".claude" / "skills"
    if skills_dir.exists() and skills_dir.is_dir():
        try:
            names += [
                p.name for p in skills_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ]
        except OSError:
            pass
    return {"skills": sorted(set(names))}


@app.post("/tmux/new")
async def new_window(payload: NewWindow, background_tasks: BackgroundTasks):
    """Create a new tmux window running claude-ephemeral. If the tmux session
    'claude' doesn't exist (all windows were closed earlier), create the session
    with this window as its first. tmux-attach.sh is attach-only, so this
    endpoint is the ONLY surface that can spawn a session — no ghost claude.exe."""
    name = sanitize_window_name(payload.name)
    cmd = CLAUDE_EPHEMERAL
    # Snapshot live claude session pids so the background task can spot the new one
    before_pids = {fp.stem for fp in SESSIONS_DIR.glob("*.json")} if SESSIONS_DIR.exists() else set()
    has_session = subprocess.run(
        [TMUX, "has-session", "-t", TMUX_SESSION],
        capture_output=True, timeout=5,
    ).returncode == 0
    if has_session:
        subprocess.run(
            [TMUX, "new-window", "-t", f"{TMUX_SESSION}:", "-n", name, cmd],
            timeout=5,
        )
    else:
        subprocess.run(
            [TMUX, "new-session", "-d", "-s", TMUX_SESSION, "-n", name, "-c", str(Path.home()), cmd],
            timeout=5,
        )
        # Hide tmux's default green status bar — it's noise inside the phone
        # iframe (window list + clock are already in the wrapper's drawer +
        # sidecar). Scoped to the claude session so Mac-terminal tmux unaffected.
        subprocess.run(
            [TMUX, "set-option", "-t", TMUX_SESSION, "status", "off"],
            capture_output=True, timeout=5,
        )
    # Write the friendly name to session-overrides.json once the claude sessionId is known,
    # so the statusbar's Sessions: segment uses the same label as the drawer.
    background_tasks.add_task(label_new_session, before_pids, name)
    return {"status": "created", "name": name, "session_created": not has_session}


@app.post("/tmux/new-terminal")
async def new_terminal_window():
    """Create a new tmux window running an interactive shell (NOT claude).
    Used for the "Terminal" button in the sidebar — gives a real TTY for
    sudo / admin tasks that don't fit a Claude session. Skips the claude
    session-overrides bookkeeping (that's claude-only). Returns the new
    window's index so the client can auto-select it."""
    name = sanitize_window_name(f"term-{int(time.time()) % 10000:04d}")
    has_session = subprocess.run(
        [TMUX, "has-session", "-t", TMUX_SESSION],
        capture_output=True, timeout=5,
    ).returncode == 0
    # No command arg => tmux uses default-shell (inherits $SHELL).
    if has_session:
        subprocess.run(
            [TMUX, "new-window", "-t", f"{TMUX_SESSION}:", "-n", name],
            timeout=5,
        )
    else:
        subprocess.run(
            [TMUX, "new-session", "-d", "-s", TMUX_SESSION, "-n", name, "-c", str(Path.home())],
            timeout=5,
        )
        # Hide tmux's default green status bar (same reason as /tmux/new above).
        subprocess.run(
            [TMUX, "set-option", "-t", TMUX_SESSION, "status", "off"],
            capture_output=True, timeout=5,
        )
    # Look up the new window's index so the client can switch to it.
    list_result = subprocess.run(
        [TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}\t#{window_name}"],
        capture_output=True, text=True, timeout=5,
    )
    new_index = None
    for line in list_result.stdout.strip().split("\n"):
        if "\t" in line:
            idx_str, w_name = line.split("\t", 1)
            if w_name == name:
                try:
                    new_index = int(idx_str)
                except ValueError:
                    pass
    return {
        "status": "created",
        "name": name,
        "session_created": not has_session,
        "index": new_index,
    }


@app.post("/tmux/select")
async def select_window(payload: WindowIndex):
    """Switch the active tmux window — iframe will show its content.

    Note: deliberately does NOT clear the session's ready flag. Per spec, a
    red 'waiting on you' dot persists until the session goes busy again
    (Claude starts a new turn). Tapping, viewing, or selecting a session
    is no longer an acknowledgment gesture — the loop closes when work
    resumes, not when you look at it."""
    subprocess.run(
        [TMUX, "select-window", "-t", f"{TMUX_SESSION}:{payload.index}"],
        timeout=5,
    )
    return {"status": "selected", "index": payload.index}


@app.post("/tmux/close")
async def close_window(payload: WindowIndex, background_tasks: BackgroundTasks):
    """Kill a tmux window. If the window was running a CC session, also fire
    session-end-journal.sh to summarize the conversation into today's vault
    journal and delete the transcript. Phone close = explicit wrap-up: the
    knowledge survives in the journal, the raw .jsonl does not. Closing the
    last window destroys the tmux session; the client should reload the
    iframe so tmux-attach.sh recreates it."""
    # Look up window name + sid + transcript BEFORE killing — these die after.
    name_result = subprocess.run(
        [TMUX, "display-message", "-p", "-t",
         f"{TMUX_SESSION}:{payload.index}", "#{window_name}"],
        capture_output=True, text=True, timeout=5,
    )
    window_name = name_result.stdout.strip() if name_result.returncode == 0 else ""
    sid = None
    transcript_path = None
    if window_name:
        sid, _ = find_cc_session_for_window(window_name)
        if sid:
            transcript_path = find_transcript_path(sid)
    list_result = subprocess.run(
        [TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}"],
        capture_output=True, text=True, timeout=5,
    )
    indices = [l for l in list_result.stdout.strip().split("\n") if l]
    was_last = len(indices) <= 1
    subprocess.run(
        [TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{payload.index}"],
        timeout=5,
    )
    if transcript_path and sid:
        background_tasks.add_task(wrap_up_closed_session, transcript_path, sid)
    return {"status": "closed", "index": payload.index, "destroyed": was_last}


@app.post("/tmux/rename")
async def rename_window(payload: WindowRename):
    """Rename a tmux window AND update its session-overrides entry so the
    statusbar's Sessions: segment matches the picker. Clears the override's
    auto flag — a manual rename pins the name, the doing-loop won't touch it."""
    name = sanitize_window_name(payload.name)
    # Resolve sid from the CURRENT window name before renaming, so the override
    # entry (keyed by sid) can be updated to the new name.
    old_name_result = subprocess.run(
        [TMUX, "display-message", "-p", "-t",
         f"{TMUX_SESSION}:{payload.index}", "#{window_name}"],
        capture_output=True, text=True, timeout=5,
    )
    old_name = old_name_result.stdout.strip() if old_name_result.returncode == 0 else ""
    sid, _ = find_cc_session_for_window(old_name) if old_name else (None, None)
    subprocess.run(
        [TMUX, "rename-window", "-t", f"{TMUX_SESSION}:{payload.index}", name],
        timeout=5,
    )
    if sid:
        set_session_label(sid, name, auto=False)
    return {"status": "renamed", "index": payload.index, "name": name}


async def auto_rename_loop():
    """Background coroutine: every AUTO_RENAME_INTERVAL_SEC, walk tmux windows
    in TMUX_SESSION and refresh the name of any session whose override is still
    auto=True. Calls ~/Context/scripts/session-doing-summarize.sh per session
    (the same Haiku-summarizer Mac sessions use). Skips sessions with auto=False
    (manually renamed via /tmux/rename or the /rename skill — those are sticky).
    Renames both the tmux window AND the override so the picker, statusbar's
    Sessions: segment, and remote peer files stay in sync.

    Why this loop exists at all: statusline.sh exits early when
    CLAUDE_REMOTE_PHONE=1 is set, so phone sessions never populate the
    /tmp/.statusline-doing-<sid> cache via the normal render path. This loop
    owns that responsibility for phone sessions."""
    while True:
        try:
            await asyncio.sleep(AUTO_RENAME_INTERVAL_SEC)
            if not SESSION_DOING_SCRIPT.exists():
                continue
            try:
                overrides = json.loads(OVERRIDES_FILE.read_text())
                if not isinstance(overrides, dict):
                    overrides = {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            list_result = subprocess.run(
                [TMUX, "list-windows", "-t", TMUX_SESSION,
                 "-F", "#{window_index}|#{window_name}"],
                capture_output=True, text=True, timeout=5,
            )
            if list_result.returncode != 0:
                continue
            for line in list_result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 1)
                if len(parts) < 2:
                    continue
                window_idx, window_name = parts[0], parts[1]
                sid, _ = find_cc_session_for_window(window_name)
                if not sid:
                    continue
                entry = overrides.get(sid)
                if not isinstance(entry, dict) or not entry.get("auto"):
                    continue
                cwd = find_cwd_for_session(sid)
                if not cwd:
                    continue
                try:
                    proc = await asyncio.create_subprocess_exec(
                        str(SESSION_DOING_SCRIPT), sid, cwd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        stdout, _ = await asyncio.wait_for(
                            proc.communicate(), timeout=30
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        continue
                except (OSError, ValueError):
                    continue
                if proc.returncode != 0:
                    continue
                new_name = stdout.decode("utf-8", errors="replace").strip()
                if not new_name or new_name == window_name:
                    continue
                new_name = sanitize_window_name(new_name)
                if new_name == window_name:
                    continue
                subprocess.run(
                    [TMUX, "rename-window",
                     "-t", f"{TMUX_SESSION}:{window_idx}", new_name],
                    timeout=5,
                )
                set_session_label(sid, new_name, auto=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a transient error kill the loop.
            pass


@app.on_event("startup")
async def _start_background_tasks():
    asyncio.create_task(auto_rename_loop())


def pick_bind_host(port: int) -> str:
    """If Tailscale Serve is fronting this port on the host, bind loopback
    (Serve proxies to 127.0.0.1; binding to the tailnet IP would also work
    on the wire, but Tailscale 1.96.5's Serve hangs proxying through the
    host's own tailnet interface). Otherwise bind the tailnet IP so the
    Pixel can reach the wrapper directly."""
    try:
        out = subprocess.run(
            ["tailscale", "serve", "status"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        if "127.0.0.1:%d" % port in out or "localhost:%d" % port in out:
            return "127.0.0.1"
    except Exception:
        pass
    return get_tailscale_ip()


if __name__ == "__main__":
    bind_host = pick_bind_host(WRAPPER_PORT)
    print(f"Voice wrapper: bound {bind_host}:{WRAPPER_PORT}")
    print(f"Terminal (ttyd): port {TTYD_PORT}")
    uvicorn.run(app, host=bind_host, port=WRAPPER_PORT)
