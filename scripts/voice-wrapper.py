#!/usr/bin/env python3
"""Voice dictation wrapper for ttyd terminal on iPhone.

Serves a page with the ttyd terminal in an iframe and a native text input
field at the bottom. Dictation works in the native input, then text is
injected into the tmux session via `tmux send-keys`.
"""

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
        .terminal-frame {{
            flex: 1;
            border: none;
            width: 100%;
        }}
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
        .copy-overlay textarea {{
            flex: 1;
            background: #1a1a1a;
            color: #e0e0e0;
            border: 1px solid #555;
            border-radius: 8px;
            padding: 12px;
            font-family: Menlo, monospace;
            font-size: 14px;
            line-height: 1.4;
            resize: none;
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
        .input-bar .menu-btn.scrolling {{
            background: #8a6d00;
            border-color: #ffd700;
            color: #fff;
        }}
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
            <span>Sessions</span>
            <button class="close-x" onclick="closeDrawer()">&times;</button>
        </div>
        <button class="new-session-btn" onclick="promptNewSession(false)">+ New session</button>
        <button class="resume-session-btn" onclick="promptNewSession(true)">+ Resume past session</button>
        <div class="session-list" id="sessionList">
            <div class="empty">Loading…</div>
        </div>
    </div>
    <div class="container">
        <iframe class="terminal-frame" src="http://{ip}:{TTYD_PORT}"></iframe>
        <div class="quick-keys">
            <button onclick="sendKey('Escape')">Esc</button>
            <button onclick="scrollPane('up')">&#8670;</button>
            <button onclick="scrollPane('down')">&#8671;</button>
            <button onclick="sendKey('Up')">&#9650;</button>
            <button onclick="sendKey('Down')">&#9660;</button>
            <button onclick="sendKey('Tab')">Tab</button>
            <button onclick="sendKey('C-u')">Del</button>
            <button onclick="sendKey('/')">/</button>
            <button onclick="document.getElementById('cameraInput').click()">&#128247;</button>
            <input type="file" id="cameraInput" accept="image/*" capture="environment" style="display:none"
                   onchange="uploadPhoto(this)">
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
        <div class="copy-hint">Long-press to select, then Copy</div>
        <textarea id="copyText" readonly></textarea>
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
            text = text.replace(/\[([^\]]+)\]/g, (match, name) => {{
                if (name.match(/\.(jpg|jpeg|png|gif|webp|heic)$/i)) {{
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

        async function scrollPane(direction) {{
            try {{
                await fetch('/scroll', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ direction }})
                }});
            }} catch (err) {{
                console.error('Scroll failed:', err);
            }}
        }}

        async function copyPane() {{
            try {{
                const resp = await fetch('/copy');
                const data = await resp.json();
                const overlay = document.getElementById('copyOverlay');
                const textarea = document.getElementById('copyText');
                textarea.value = data.text;
                overlay.classList.add('active');
                // Scroll to bottom so most recent output is visible
                textarea.scrollTop = textarea.scrollHeight;
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
                    row.addEventListener('click', (e) => {{
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
                fileInput.value = '';
            }}
        }}

        // Auto-reconnect: reload iframe when tab becomes visible again.
        // ttyd closes WS with code 1000 when Chrome backgrounds the tab,
        // which triggers its "Press ⏎ to Reconnect" overlay; force-reload
        // the iframe to bypass that and reattach to the persistent tmux session.
        const terminal = document.querySelector('.terminal-frame');
        const TERMINAL_SRC = `http://{ip}:{TTYD_PORT}`;
        function reloadTerminal() {{
            terminal.src = 'about:blank';
            setTimeout(() => {{ terminal.src = TERMINAL_SRC + '?t=' + Date.now(); }}, 50);
        }}
        document.addEventListener('visibilitychange', () => {{
            if (document.visibilityState === 'visible') {{
                reloadTerminal();
                refreshSessions();
            }}
        }});
        window.addEventListener('pageshow', (e) => {{
            if (e.persisted) {{
                reloadTerminal();
                refreshSessions();
            }}
        }});

        // Populate session label + drawer on first paint
        refreshSessions();

        // Poll pane mode every 2s to surface the scroll indicator
        async function refreshState() {{
            try {{
                const resp = await fetch('/state');
                const data = await resp.json();
                const btn = document.querySelector('.menu-btn');
                const icon = btn.querySelector('.icon');
                if (data.in_copy_mode) {{
                    btn.classList.add('scrolling');
                    icon.innerHTML = '&#8670;';
                }} else {{
                    btn.classList.remove('scrolling');
                    icon.innerHTML = '&#9776;';
                }}
            }} catch (err) {{ /* silent */ }}
        }}
        refreshState();
        setInterval(refreshState, 2000);

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


@app.post("/scroll")
async def scroll_pane(payload: ScrollInput):
    """Scroll the tmux pane's scrollback buffer. Up enters copy mode then
    pages up; down pages down (auto-exits copy mode at the bottom)."""
    if payload.direction == "up":
        subprocess.run(
            [TMUX, "copy-mode", "-t", TMUX_SESSION],
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            [TMUX, "send-keys", "-t", TMUX_SESSION, "-X", "page-up"],
            capture_output=True,
            timeout=5,
        )
    elif payload.direction == "down":
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
    """Send a special key (Escape, C-c, Enter, etc.) to tmux."""
    if payload.key not in ALLOWED_KEYS:
        return {"status": "rejected", "error": "key not allowed"}
    if payload.key == "Escape":
        _exit_copy_mode()
    subprocess.run(
        [TMUX, "send-keys", "-t", TMUX_SESSION, payload.key],
        timeout=5,
    )
    return {"status": "sent"}


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
    """Capture full tmux pane scrollback for copying."""
    result = subprocess.run(
        [TMUX, "capture-pane", "-t", TMUX_SESSION, "-p", "-S", "-"],
        capture_output=True, text=True, timeout=5,
    )
    return {"text": result.stdout}


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
    """Kill a tmux window. Refuses to close the last remaining one (would kill the tmux session)."""
    list_result = subprocess.run(
        [TMUX, "list-windows", "-t", TMUX_SESSION, "-F", "#{window_index}"],
        capture_output=True, text=True, timeout=5,
    )
    indices = [l for l in list_result.stdout.strip().split("\n") if l]
    if len(indices) <= 1:
        return {"status": "rejected", "error": "cannot close last window"}
    subprocess.run(
        [TMUX, "kill-window", "-t", f"{TMUX_SESSION}:{payload.index}"],
        timeout=5,
    )
    return {"status": "closed", "index": payload.index}


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
