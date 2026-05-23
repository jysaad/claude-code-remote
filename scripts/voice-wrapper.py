#!/usr/bin/env python3
"""Voice dictation wrapper for ttyd terminal on iPhone.

Serves a page with the ttyd terminal in an iframe and a native text input
field at the bottom. Dictation works in the native input, then text is
injected into the tmux session via `tmux send-keys`.
"""

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
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
OVERRIDES_FILE = Path.home() / ".claude" / "session-overrides.json"
STATUSLINE_SH = str(Path.home() / ".claude" / "statusline.sh")

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
    payload = json.dumps({
        "session_id": "",
        "version": "phone",
        "model": {"display_name": "Phone"},
        "effort": {"level": ""},
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


def set_session_label(session_id: str, label: str):
    """Write {sessionId: {name: label}} into session-overrides.json."""
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text())
        if not isinstance(overrides, dict):
            overrides = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        overrides = {}
    overrides.setdefault(session_id, {})["name"] = label
    OVERRIDES_FILE.write_text(json.dumps(overrides, indent=2))


def label_new_session(before_pids: set, label: str):
    sid = find_new_session(before_pids)
    if sid:
        set_session_label(sid, label)


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
    resume: bool = False


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
            display: flex;
            flex-direction: column;
            height: 100vh;
            height: 100dvh;
        }}
        .terminal-wrap {{
            flex: 1;
            position: relative;
            min-height: 0;
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
            padding: 6px 8px;
            background: #1c1c1c;
            color: #d0d0d0;
            font-family: 'Menlo', monospace;
            font-size: 12px;
            line-height: 1.5;
            border-top: 1px solid #2a2a2a;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 120px;
            overflow-y: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .status-bar:empty {{ display: none; }}
        .quick-keys {{
            display: flex;
            gap: 4px;
            padding: 4px 6px;
            background: #252525;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}
        .quick-keys button {{
            padding: 8px 14px;
            font-size: 14px;
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
            padding: 6px;
            background: #2d2d2d;
            border-top: 1px solid #444;
        }}
        .input-bar textarea {{
            flex: 1;
            padding: 10px 12px;
            font-size: 16px;
            border: 1px solid #555;
            border-radius: 8px;
            background: #1a1a1a;
            color: #fff;
            outline: none;
            resize: none;
            overflow-y: hidden;
            min-height: 42px;
            max-height: 100px;
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
        .resume-session-btn {{
            margin: 0 16px 12px;
            padding: 10px;
            background: #333;
            color: #ddd;
            border: 1px solid #555;
            border-radius: 8px;
            font-weight: 500;
            font-size: 13px;
            cursor: pointer;
        }}
        .resume-session-btn:active {{ background: #444; }}
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
            display: flex;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid #2a2a2a;
            color: #bbb;
            cursor: pointer;
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
        .session-row .idx {{
            color: #666;
            font-size: 11px;
            font-family: Menlo, monospace;
            margin-right: 8px;
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
    </style>
</head>
<body>
    <div class="drawer-backdrop" id="drawerBackdrop" onclick="closeDrawer()"></div>
    <div class="drawer" id="drawer">
        <div class="drawer-header">
            <span>Sessions <span class="drawer-hint">hold to rename</span></span>
            <button class="close-x" onclick="closeDrawer()">&times;</button>
        </div>
        <button class="new-session-btn" onclick="promptNewSession(false)">+ New session</button>
        <button class="resume-session-btn" onclick="promptNewSession(true)">+ Resume past session</button>
        <div class="session-list" id="sessionList">
            <div class="empty">Loading…</div>
        </div>
    </div>
    <div class="container">
        <div class="terminal-wrap">
            <iframe class="terminal-frame" src="about:blank"></iframe>
            <div class="touch-scroll-overlay" id="scrollOverlay"></div>
            <div class="scroll-hint" id="scrollHint">scrolling</div>
        </div>
        <div class="status-bar" id="statusBar"></div>
        <div class="quick-keys">
            <button onclick="sendKey('/')">/</button>
            <button onclick="reconnectAll()" title="Reconnect terminal">&#8635;</button>
            <button onclick="sendKey('Escape')">Esc</button>
            <button onclick="sendKey('Up')">&#9650;</button>
            <button onclick="sendKey('Down')">&#9660;</button>
            <button onclick="sendKey('Tab')">Tab</button>
            <button onclick="sendKey('C-u')">Del</button>
            <button onclick="takeScreenshot(this)">&#128242;</button>
            <button onclick="document.getElementById('galleryInput').click()">&#128444;&#65039;</button>
            <input type="file" id="galleryInput" accept="image/*" multiple style="display:none"
                   onchange="uploadPhoto(this)">
        </div>
        <div class="input-bar">
            <button class="menu-btn" onclick="openDrawer()" aria-label="Menu">
                <span class="icon">&#9776;</span>
                <span class="active-name" id="activeName">…</span>
            </button>
            <textarea id="cmd" rows="1"
                      placeholder="Dictate or type here..."
                      autocomplete="off"
                      autocorrect="on"
                      enterkeyhint="send"></textarea>
            <button onclick="sendText()">Send</button>
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

        // Auto-resize textarea as content grows
        input.addEventListener('input', () => {{
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 100) + 'px';
        }});

        // Enter sends, Shift+Enter adds newline
        input.addEventListener('keydown', (e) => {{
            if (e.key === 'Enter' && !e.shiftKey) {{
                e.preventDefault();
                sendText();
            }}
        }});

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

            try {{
                await fetch('/send', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ text }})
                }});
                if (!override) {{
                    input.value = '';
                    input.style.height = 'auto';
                    input.focus();
                }}
            }} catch (err) {{
                console.error('Send failed:', err);
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
            await promptNewSession(false);
        }}

        async function resumeSession() {{
            // Spawn a new tmux window with claude-ephemeral --resume.
            await promptNewSession(true);
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
                if (!data.windows || !data.windows.length) {{
                    list.innerHTML = '<div class="empty">No sessions</div>';
                    activeNameEl.textContent = '…';
                    return;
                }}
                list.innerHTML = '';
                let activeLabel = '';
                for (const w of data.windows) {{
                    if (w.active) activeLabel = w.name;
                    const row = document.createElement('div');
                    row.className = 'session-row' + (w.active ? ' active' : '');
                    row.innerHTML = `
                        <span class="idx">${{w.index}}</span>
                        <span class="name">${{escapeHtml(w.name)}}</span>
                        <button class="row-close" data-idx="${{w.index}}" data-name="${{escapeHtml(w.name)}}">&times;</button>
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

        async function promptNewSession(resume) {{
            const defaultName = (resume ? 'resume-' : 'session-') + Date.now().toString().slice(-4);
            const name = prompt(resume ? 'Resume session — name?' : 'New session — name?', defaultName);
            if (!name) return;
            try {{
                await fetch('/tmux/new', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name, resume: !!resume }})
                }});
                // Give tmux a moment to create the window before refreshing
                setTimeout(refreshSessions, 400);
                closeDrawer();
            }} catch (err) {{
                console.error('new session failed:', err);
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
                    // iframe so tmux-attach.sh recreates a fresh empty `claude`.
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

        async function takeScreenshot(btn) {{
            const origPlaceholder = input.placeholder;
            const failMsg = (msg) => {{
                input.placeholder = msg;
                setTimeout(() => {{ input.placeholder = origPlaceholder; }}, 3000);
            }};
            if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {{
                failMsg('Screen capture unavailable (needs HTTPS).');
                return;
            }}
            let stream;
            try {{
                stream = await navigator.mediaDevices.getDisplayMedia({{ video: true, audio: false }});
            }} catch (err) {{
                if (err.name === 'NotAllowedError' || err.name === 'AbortError') return;
                console.error('Screen capture failed:', err);
                failMsg('Screenshot failed: ' + (err.message || err.name));
                return;
            }}
            try {{
                const video = document.createElement('video');
                video.srcObject = stream;
                video.muted = true;
                await video.play();
                await new Promise(r => requestAnimationFrame(r));
                const canvas = document.createElement('canvas');
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
                canvas.getContext('2d').drawImage(video, 0, 0);
                video.pause();
                video.srcObject = null;
                const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', 0.92));
                const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
                const file = new File([blob], 'screenshot-' + ts + '.jpg', {{ type: 'image/jpeg' }});
                await uploadFiles([file], btn);
            }} catch (err) {{
                console.error('Screenshot processing error:', err);
                failMsg('Screenshot processing failed');
            }} finally {{
                stream.getTracks().forEach(t => t.stop());
            }}
        }}

        // Sidecar statusbar: poll /status every 60s, render server-converted HTML.
        // Bypasses xterm.js col-truncation by rendering the bar as native HTML.
        async function refreshStatus() {{
            try {{
                const r = await fetch('/status', {{ cache: 'no-store' }});
                const data = await r.json();
                const bar = document.getElementById('statusBar');
                if (bar) bar.innerHTML = data.html || '';
            }} catch (e) {{ /* silent */ }}
        }}
        refreshStatus();
        setInterval(refreshStatus, 60000);

        // Auto-reconnect: reload iframe when tab becomes visible again.
        // ttyd closes WS with code 1000 when Chrome backgrounds the tab,
        // which triggers its "Press ⏎ to Reconnect" overlay; force-reload
        // the iframe to bypass that and reattach to the persistent tmux session.
        const terminal = document.querySelector('.terminal-frame');
        const TERMINAL_SRC = window.location.protocol === 'https:'
            ? `${{window.location.protocol}}//${{window.location.hostname}}:8443`
            : `http://{ip}:{TTYD_PORT}`;
        terminal.src = TERMINAL_SRC;
        function reloadTerminal() {{
            terminal.src = 'about:blank';
            setTimeout(() => {{ terminal.src = TERMINAL_SRC + '?t=' + Date.now(); }}, 50);
        }}
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
            __lastReconnectAt = now;
            reconnectAll();
        }}
        document.addEventListener('visibilitychange', () => {{
            if (document.visibilityState === 'visible') {{ reconnectDebounced(); }}
        }});
        window.addEventListener('pageshow', (e) => {{
            if (e.persisted) {{ reconnectDebounced(); }}
        }});
        // Mobile Chrome doesn't fire visibilitychange reliably across PWA/tab
        // boundaries; focus + online catch the cases visibility misses,
        // particularly when ttyd's one-shot auto-reconnect itself fails and
        // leaves "Press ⏎ to Reconnect" stuck without a visibility transition.
        window.addEventListener('focus', reconnectDebounced);
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
            const PIXELS_PER_LINE = 14;
            const THROTTLE_MS = 40;
            const TAP_THRESHOLD_PX = 8;
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
                if (movementMag < TAP_THRESHOLD_PX) {{
                    input.focus();
                }}
            }}, {{ passive: true }});

            overlay.addEventListener('touchcancel', () => {{
                isTouching = false;
            }}, {{ passive: true }});

            // Desktop / trackpad wheel: same overlay, translate wheel delta.
            overlay.addEventListener('wheel', (e) => {{
                e.preventDefault();
                const lines = Math.round(-e.deltaY / PIXELS_PER_LINE);
                if (lines !== 0) queue(lines);
            }}, {{ passive: false }});
        }})();

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
    """List tmux windows in the claude session. Each is a separate CC session."""
    result = subprocess.run(
        [TMUX, "list-windows", "-t", TMUX_SESSION,
         "-F", "#{window_index}|#{window_name}|#{window_active}"],
        capture_output=True, text=True, timeout=5,
    )
    windows = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) >= 3:
            windows.append({
                "index": int(parts[0]),
                "name": parts[1],
                "active": parts[2] == "1",
            })
    return {"windows": windows}


@app.post("/tmux/new")
async def new_window(payload: NewWindow, background_tasks: BackgroundTasks):
    """Create a new tmux window running claude-ephemeral. Original session keeps running."""
    name = sanitize_window_name(payload.name)
    cmd = CLAUDE_EPHEMERAL + (" --resume" if payload.resume else "")
    # Snapshot live claude session pids so the background task can spot the new one
    before_pids = {fp.stem for fp in SESSIONS_DIR.glob("*.json")} if SESSIONS_DIR.exists() else set()
    subprocess.run(
        [TMUX, "new-window", "-t", f"{TMUX_SESSION}:", "-n", name, cmd],
        timeout=5,
    )
    # Write the friendly name to session-overrides.json once the claude sessionId is known,
    # so the statusbar's Sessions: segment uses the same label as the drawer.
    background_tasks.add_task(label_new_session, before_pids, name)
    return {"status": "created", "name": name}


@app.post("/tmux/select")
async def select_window(payload: WindowIndex):
    """Switch the active tmux window — iframe will show its content."""
    subprocess.run(
        [TMUX, "select-window", "-t", f"{TMUX_SESSION}:{payload.index}"],
        timeout=5,
    )
    return {"status": "selected", "index": payload.index}


@app.post("/tmux/close")
async def close_window(payload: WindowIndex):
    """Kill a tmux window. Closing the last window destroys the tmux session;
    the client should reload the iframe so tmux-attach.sh recreates it."""
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
    return {"status": "closed", "index": payload.index, "destroyed": was_last}


@app.post("/tmux/rename")
async def rename_window(payload: WindowRename):
    """Rename a tmux window. Does not touch session-overrides.json (that's set at creation)."""
    name = sanitize_window_name(payload.name)
    subprocess.run(
        [TMUX, "rename-window", "-t", f"{TMUX_SESSION}:{payload.index}", name],
        timeout=5,
    )
    return {"status": "renamed", "index": payload.index, "name": name}


if __name__ == "__main__":
    ip = get_tailscale_ip()
    print(f"Voice wrapper: http://{ip}:{WRAPPER_PORT}")
    print(f"Terminal (ttyd): http://{ip}:{TTYD_PORT}")
    uvicorn.run(app, host=ip, port=WRAPPER_PORT)
